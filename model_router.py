"""Configuration-backed, budget-aware model routing.

This module is deliberately deterministic: loading a catalog and choosing a model
never calls a provider API. Prices and capability claims are operator-maintained
snapshots used for routing estimates, not a promise of current provider pricing.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_CATALOG_FILE = Path(__file__).resolve().parent / "config" / "model-catalog.json"
MODEL_CATALOG_ENV = "MODEL_CATALOG_FILE"


class ModelCatalogError(ValueError):
    """Raised when the configured model catalog is missing or invalid."""


class CapabilityLevel(str, Enum):
    LIGHTWEIGHT = "lightweight"
    STANDARD = "standard"
    ADVANCED = "advanced"


_LEVEL_RANK = {
    CapabilityLevel.LIGHTWEIGHT: 1,
    CapabilityLevel.STANDARD: 2,
    CapabilityLevel.ADVANCED: 3,
}


def _normalise_name(value: str) -> str:
    return "_".join(str(value).strip().lower().replace("-", " ").split())


def _coerce_level(value: CapabilityLevel | str) -> CapabilityLevel:
    if isinstance(value, CapabilityLevel):
        return value
    try:
        return CapabilityLevel(_normalise_name(value))
    except ValueError as exc:
        valid = ", ".join(level.value for level in CapabilityLevel)
        raise ModelCatalogError(f"Unknown capability level {value!r}; expected {valid}.") from exc


def _higher_level(left: CapabilityLevel, right: CapabilityLevel) -> CapabilityLevel:
    return left if _LEVEL_RANK[left] >= _LEVEL_RANK[right] else right


def _next_level(level: CapabilityLevel) -> CapabilityLevel:
    if level is CapabilityLevel.LIGHTWEIGHT:
        return CapabilityLevel.STANDARD
    return CapabilityLevel.ADVANCED


def _non_negative_number(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelCatalogError(f"{field_name} must be a number.") from exc
    if not math.isfinite(number) or number < 0:
        raise ModelCatalogError(f"{field_name} must be a finite non-negative number.")
    return number


def _non_negative_tokens(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer.")
    try:
        tokens = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a non-negative integer.") from exc
    if tokens < 0 or tokens != value:
        raise ValueError(f"{field_name} must be a non-negative integer.")
    return tokens


@dataclass(frozen=True)
class ModelSpec:
    """One routable model and its configured capability/pricing snapshot."""

    model_id: str
    level: CapabilityLevel
    capabilities: frozenset[str]
    context_limit_tokens: int
    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float
    strength: int
    enabled: bool = True

    def __post_init__(self) -> None:
        model_id = str(self.model_id).strip()
        if not model_id:
            raise ModelCatalogError("A model id cannot be empty.")
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "level", _coerce_level(self.level))
        object.__setattr__(
            self,
            "capabilities",
            frozenset(_normalise_name(item) for item in self.capabilities if str(item).strip()),
        )

        context_limit = _non_negative_tokens(self.context_limit_tokens, "context_limit_tokens")
        if context_limit == 0:
            raise ModelCatalogError("context_limit_tokens must be greater than zero.")
        object.__setattr__(self, "context_limit_tokens", context_limit)

        for field_name in (
            "input_usd_per_million",
            "cached_input_usd_per_million",
            "output_usd_per_million",
        ):
            object.__setattr__(self, field_name, _non_negative_number(getattr(self, field_name), field_name))

        strength = _non_negative_tokens(self.strength, "strength")
        if strength == 0:
            raise ModelCatalogError("strength must be greater than zero.")
        object.__setattr__(self, "strength", strength)

    @property
    def id(self) -> str:
        """JSON-friendly alias for ``model_id``."""

        return self.model_id

    @property
    def context_window_tokens(self) -> int:
        return self.context_limit_tokens

    @property
    def input_price_per_million(self) -> float:
        return self.input_usd_per_million

    @property
    def cached_input_price_per_million(self) -> float:
        return self.cached_input_usd_per_million

    @property
    def output_price_per_million(self) -> float:
        return self.output_usd_per_million

    def estimate_cost(
        self,
        input_tokens: int,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> float:
        """Estimate USD cost, treating cached input as a subset of input tokens."""

        input_count = _non_negative_tokens(input_tokens, "input_tokens")
        cached_count = min(
            _non_negative_tokens(cached_input_tokens, "cached_input_tokens"),
            input_count,
        )
        output_count = _non_negative_tokens(output_tokens, "output_tokens")
        fresh_count = input_count - cached_count
        total = (
            fresh_count * self.input_usd_per_million
            + cached_count * self.cached_input_usd_per_million
            + output_count * self.output_usd_per_million
        ) / 1_000_000
        return round(total, 9)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ModelSpec":
        pricing = raw.get("pricing_usd_per_million", {})
        if not isinstance(pricing, Mapping):
            raise ModelCatalogError("pricing_usd_per_million must be an object.")

        def configured(*names: str, default: Any = None) -> Any:
            for name in names:
                if name in raw:
                    return raw[name]
                if name in pricing:
                    return pricing[name]
            return default

        model_id = configured("id", "model_id")
        if model_id is None or not str(model_id).strip():
            raise ModelCatalogError("Each catalog model must have a non-empty id.")
        level = _coerce_level(configured("level", default="standard"))
        capabilities = configured("capabilities", default=[])
        if not isinstance(capabilities, Sequence) or isinstance(capabilities, (str, bytes)):
            raise ModelCatalogError(f"Model {model_id!r} capabilities must be a list.")
        default_strength = _LEVEL_RANK[level] * 100
        return cls(
            model_id=model_id,
            level=level,
            capabilities=frozenset(str(item) for item in capabilities),
            context_limit_tokens=configured(
                "context_limit_tokens", "context_window_tokens", default=128_000
            ),
            input_usd_per_million=configured(
                "input_usd_per_million", "input", default=None
            ),
            cached_input_usd_per_million=configured(
                "cached_input_usd_per_million", "cached_input", default=None
            ),
            output_usd_per_million=configured(
                "output_usd_per_million", "output", default=None
            ),
            strength=configured("strength", default=default_strength),
            enabled=bool(configured("enabled", default=True)),
        )


@dataclass(frozen=True)
class RoutingRequest:
    """Facts the deterministic router uses to select or defer a model call."""

    task_type: str
    complexity: CapabilityLevel | str = "auto"
    risk: str = "low"
    required_capabilities: tuple[str, ...] = ()
    estimated_input_tokens: int = 4_000
    estimated_cached_input_tokens: int = 0
    estimated_output_tokens: int = 1_000
    remaining_budget_usd: float | None = None
    previous_failures: int = 0
    previous_models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        task_type = _normalise_name(self.task_type)
        if not task_type:
            raise ValueError("task_type cannot be empty.")
        object.__setattr__(self, "task_type", task_type)
        if isinstance(self.complexity, CapabilityLevel):
            object.__setattr__(self, "complexity", self.complexity.value)
        else:
            object.__setattr__(self, "complexity", _normalise_name(self.complexity) or "auto")
        object.__setattr__(self, "risk", _normalise_name(self.risk) or "low")
        object.__setattr__(
            self,
            "required_capabilities",
            tuple(
                dict.fromkeys(
                    _normalise_name(item)
                    for item in self.required_capabilities
                    if str(item).strip()
                )
            ),
        )
        object.__setattr__(
            self,
            "estimated_input_tokens",
            _non_negative_tokens(self.estimated_input_tokens, "estimated_input_tokens"),
        )
        object.__setattr__(
            self,
            "estimated_cached_input_tokens",
            _non_negative_tokens(
                self.estimated_cached_input_tokens, "estimated_cached_input_tokens"
            ),
        )
        object.__setattr__(
            self,
            "estimated_output_tokens",
            _non_negative_tokens(self.estimated_output_tokens, "estimated_output_tokens"),
        )
        if self.remaining_budget_usd is not None:
            object.__setattr__(
                self,
                "remaining_budget_usd",
                _non_negative_number(self.remaining_budget_usd, "remaining_budget_usd"),
            )
        failures = _non_negative_tokens(self.previous_failures, "previous_failures")
        object.__setattr__(self, "previous_failures", failures)
        object.__setattr__(
            self,
            "previous_models",
            tuple(str(model).strip() for model in self.previous_models if str(model).strip()),
        )

    @property
    def context_size_tokens(self) -> int:
        """Conservative context requirement including the expected response."""

        return self.estimated_input_tokens + self.estimated_output_tokens


@dataclass(frozen=True)
class RoutingDecision:
    """A selected model or an explicit, non-spending deferral."""

    model_id: str | None
    model_level: CapabilityLevel | None
    required_level: CapabilityLevel
    estimated_cost_usd: float
    reason: str
    deferred: bool = False
    deferral_reason: str | None = None

    @property
    def model(self) -> str | None:
        return self.model_id

    @property
    def level(self) -> CapabilityLevel | None:
        return self.model_level

    @property
    def status(self) -> str:
        return "deferred" if self.deferred else "selected"


@dataclass(frozen=True)
class ModelCatalog:
    models: tuple[ModelSpec, ...]
    source_path: Path | None = None
    version: int = 1
    pricing_basis: str = "usd_per_1m_tokens"
    snapshot_note: str = "Operator-maintained pricing snapshot."

    def __post_init__(self) -> None:
        models = tuple(self.models)
        if not models:
            raise ModelCatalogError("The model catalog must contain at least one model.")
        duplicate_ids = sorted(
            model_id
            for model_id in {model.model_id for model in models}
            if sum(model.model_id == model_id for model in models) > 1
        )
        if duplicate_ids:
            raise ModelCatalogError(f"Duplicate model ids: {', '.join(duplicate_ids)}")
        object.__setattr__(self, "models", models)

    def __iter__(self):
        return iter(self.models)

    def get(self, model_id: str) -> ModelSpec | None:
        return next((model for model in self.models if model.model_id == model_id), None)

    def require(self, model_id: str) -> ModelSpec:
        model = self.get(model_id)
        if model is None:
            raise KeyError(f"Model {model_id!r} is not present in the catalog.")
        return model

    @property
    def enabled_models(self) -> tuple[ModelSpec, ...]:
        return tuple(model for model in self.models if model.enabled)

    def estimate_cost(
        self,
        model_id: str,
        input_tokens: int,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> float:
        return self.require(model_id).estimate_cost(
            input_tokens, cached_input_tokens, output_tokens
        )


def _models_from_json(raw_models: Any) -> Iterable[ModelSpec]:
    if isinstance(raw_models, Mapping):
        for model_id, payload in raw_models.items():
            if not isinstance(payload, Mapping):
                raise ModelCatalogError(f"Catalog entry {model_id!r} must be an object.")
            merged = dict(payload)
            merged.setdefault("id", model_id)
            yield ModelSpec.from_dict(merged)
        return
    if not isinstance(raw_models, Sequence) or isinstance(raw_models, (str, bytes)):
        raise ModelCatalogError("Catalog 'models' must be a list or object.")
    for payload in raw_models:
        if not isinstance(payload, Mapping):
            raise ModelCatalogError("Each catalog model must be an object.")
        yield ModelSpec.from_dict(payload)


def load_model_catalog(path: str | os.PathLike[str] | None = None) -> ModelCatalog:
    """Load the explicit path, ``MODEL_CATALOG_FILE``, or the repository default."""

    configured_path = path
    if configured_path is None:
        configured_path = os.environ.get(MODEL_CATALOG_ENV) or DEFAULT_CATALOG_FILE
    source_path = Path(configured_path).expanduser().resolve()
    try:
        with source_path.open("r", encoding="utf-8") as catalog_file:
            raw = json.load(catalog_file)
    except FileNotFoundError as exc:
        raise ModelCatalogError(f"Model catalog not found: {source_path}") from exc
    except json.JSONDecodeError as exc:
        raise ModelCatalogError(f"Invalid JSON in model catalog {source_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ModelCatalogError("The model catalog root must be an object.")
    return ModelCatalog(
        models=tuple(_models_from_json(raw.get("models"))),
        source_path=source_path,
        version=int(raw.get("version", 1)),
        pricing_basis=str(raw.get("pricing_basis", "usd_per_1m_tokens")),
        snapshot_note=str(raw.get("snapshot_note", "Operator-maintained pricing snapshot.")),
    )


# Short alias used by the design document and convenient for callers.
load_catalog = load_model_catalog


_TASK_PROFILES: dict[str, tuple[CapabilityLevel, str | None]] = {
    "classification": (CapabilityLevel.LIGHTWEIGHT, "classification"),
    "formatting": (CapabilityLevel.LIGHTWEIGHT, "formatting"),
    "summarization": (CapabilityLevel.LIGHTWEIGHT, "summarization"),
    "summary": (CapabilityLevel.LIGHTWEIGHT, "summarization"),
    "extraction": (CapabilityLevel.LIGHTWEIGHT, "extraction"),
    "routing": (CapabilityLevel.LIGHTWEIGHT, "routing"),
    "status_update": (CapabilityLevel.LIGHTWEIGHT, "status_updates"),
    "escalation_message": (CapabilityLevel.LIGHTWEIGHT, "status_updates"),
    "planning": (CapabilityLevel.STANDARD, "planning"),
    "coding": (CapabilityLevel.STANDARD, "coding"),
    "debugging": (CapabilityLevel.STANDARD, "debugging"),
    "documentation": (CapabilityLevel.STANDARD, "documentation"),
    "research": (CapabilityLevel.STANDARD, "research"),
    "review": (CapabilityLevel.STANDARD, "review"),
    "creative_ideation": (CapabilityLevel.STANDARD, "ideation"),
    "architecture": (CapabilityLevel.ADVANCED, "architecture"),
    "architecture_decision": (CapabilityLevel.ADVANCED, "architecture"),
    "complex_debugging": (CapabilityLevel.ADVANCED, "debugging"),
    "cross_project_reasoning": (CapabilityLevel.ADVANCED, "cross_project_reasoning"),
    "security_review": (CapabilityLevel.ADVANCED, "security_review"),
    "important_review": (CapabilityLevel.ADVANCED, "review"),
}


def _task_profile(task_type: str) -> tuple[CapabilityLevel, str | None]:
    if task_type in _TASK_PROFILES:
        return _TASK_PROFILES[task_type]
    if "architect" in task_type or "cross_project" in task_type:
        return CapabilityLevel.ADVANCED, "architecture"
    if "classif" in task_type or "extract" in task_type or "format" in task_type:
        return CapabilityLevel.LIGHTWEIGHT, None
    if "summar" in task_type or "status" in task_type:
        return CapabilityLevel.LIGHTWEIGHT, None
    if "debug" in task_type:
        return CapabilityLevel.STANDARD, "debugging"
    if "code" in task_type or "engineer" in task_type:
        return CapabilityLevel.STANDARD, "coding"
    if "review" in task_type:
        return CapabilityLevel.STANDARD, "review"
    return CapabilityLevel.STANDARD, None


def _complexity_level(value: str) -> CapabilityLevel | None:
    if value in {"", "auto", "unspecified"}:
        return None
    if value in {"lightweight", "simple", "low", "trivial"}:
        return CapabilityLevel.LIGHTWEIGHT
    if value in {"standard", "normal", "moderate", "medium"}:
        return CapabilityLevel.STANDARD
    if value in {"advanced", "complex", "high", "difficult"}:
        return CapabilityLevel.ADVANCED
    # Unknown complexity should not accidentally select the cheapest tier.
    return CapabilityLevel.STANDARD


def _risk_level(value: str) -> CapabilityLevel:
    if value in {"low", "routine", "none", ""}:
        return CapabilityLevel.LIGHTWEIGHT
    if value in {"medium", "moderate", "normal"}:
        return CapabilityLevel.STANDARD
    # High, critical, important, or an unrecognised risk label is conservative.
    return CapabilityLevel.ADVANCED


def _usd(amount: float) -> str:
    return f"${amount:.6f}".rstrip("0").rstrip(".")


class ModelRouter:
    """Select the least expensive configured model likely to complete a task."""

    def __init__(self, catalog: ModelCatalog | None = None):
        self.catalog = catalog or load_model_catalog()

    def estimate_cost(
        self,
        model_id: str,
        input_tokens: int,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> float:
        return self.catalog.estimate_cost(
            model_id, input_tokens, cached_input_tokens, output_tokens
        )

    def route(self, request: RoutingRequest) -> RoutingDecision:
        task_level, task_capability = _task_profile(request.task_type)
        required_level = task_level
        reason_parts = [f"task type {request.task_type!r} maps to {task_level.value}"]

        complexity_level = _complexity_level(str(request.complexity))
        if complexity_level is not None:
            required_level = _higher_level(required_level, complexity_level)
            reason_parts.append(f"complexity={request.complexity}")

        risk_level = _risk_level(request.risk)
        required_level = _higher_level(required_level, risk_level)
        if risk_level is not CapabilityLevel.LIGHTWEIGHT:
            reason_parts.append(f"risk={request.risk}")

        required_capabilities = set(request.required_capabilities)
        if task_capability:
            required_capabilities.add(task_capability)
        if required_capabilities:
            reason_parts.append(
                "requires " + ", ".join(sorted(required_capabilities))
            )

        failed_models: tuple[str, ...] = ()
        minimum_strength = 0
        if request.previous_failures:
            if request.previous_models:
                failed_models = request.previous_models[-request.previous_failures :]
                failed_specs = [
                    self.catalog.get(model_id)
                    for model_id in failed_models
                    if self.catalog.get(model_id) is not None
                ]
                if failed_specs:
                    minimum_strength = max(model.strength for model in failed_specs if model)
                    reason_parts.append(
                        f"{request.previous_failures} failed attempt(s) require a model stronger "
                        f"than {', '.join(failed_models)}"
                    )
                else:
                    required_level = _next_level(required_level)
                    reason_parts.append(
                        f"{request.previous_failures} failed attempt(s) promoted the capability tier"
                    )
            else:
                required_level = _next_level(required_level)
                reason_parts.append(
                    f"{request.previous_failures} failed attempt(s) promoted the capability tier"
                )

        level_candidates = [
            model
            for model in self.catalog.enabled_models
            if _LEVEL_RANK[model.level] >= _LEVEL_RANK[required_level]
        ]
        if not level_candidates:
            return self._defer(
                required_level,
                "no_enabled_model",
                reason_parts,
                f"No enabled model meets the required {required_level.value} level.",
            )

        capability_candidates = [
            model
            for model in level_candidates
            if required_capabilities.issubset(model.capabilities)
        ]
        if not capability_candidates:
            missing = ", ".join(sorted(required_capabilities)) or "requested capabilities"
            return self._defer(
                required_level,
                "missing_capability",
                reason_parts,
                f"No enabled {required_level.value}-or-stronger model supports {missing}.",
            )

        context_candidates = [
            model
            for model in capability_candidates
            if request.context_size_tokens <= model.context_limit_tokens
        ]
        if not context_candidates:
            largest = max(model.context_limit_tokens for model in capability_candidates)
            return self._defer(
                required_level,
                "context_limit",
                reason_parts,
                f"The estimated {request.context_size_tokens:,}-token context exceeds the "
                f"largest capable configured limit ({largest:,}).",
            )

        stronger_candidates = [
            model
            for model in context_candidates
            if model.model_id not in failed_models and model.strength > minimum_strength
        ]
        if not stronger_candidates:
            return self._defer(
                required_level,
                "no_stronger_model",
                reason_parts,
                "No stronger untried configured model remains; retrying would risk a no-progress loop.",
            )

        priced_candidates = sorted(
            (
                model.estimate_cost(
                    request.estimated_input_tokens,
                    request.estimated_cached_input_tokens,
                    request.estimated_output_tokens,
                ),
                model.strength,
                model.model_id,
                model,
            )
            for model in stronger_candidates
        )
        estimated_cost, _strength, _model_id, selected = priced_candidates[0]

        if (
            request.remaining_budget_usd is not None
            and estimated_cost > request.remaining_budget_usd + 1e-12
        ):
            return self._defer(
                required_level,
                "insufficient_budget",
                reason_parts,
                f"Cheapest capable model {selected.model_id} is estimated at "
                f"{_usd(estimated_cost)}, above the remaining {_usd(request.remaining_budget_usd)}.",
                estimated_cost=estimated_cost,
            )

        reason_parts.append(
            f"selected {selected.model_id} ({selected.level.value}) as the lowest estimated-cost "
            f"capable model among {len(priced_candidates)} candidate(s)"
        )
        reason_parts.append(
            f"estimated {_usd(estimated_cost)} from the configured pricing snapshot"
        )
        if request.remaining_budget_usd is not None:
            reason_parts.append(f"within remaining {_usd(request.remaining_budget_usd)}")
        return RoutingDecision(
            model_id=selected.model_id,
            model_level=selected.level,
            required_level=required_level,
            estimated_cost_usd=estimated_cost,
            reason="; ".join(reason_parts) + ".",
        )

    @staticmethod
    def _defer(
        required_level: CapabilityLevel,
        deferral_reason: str,
        reason_parts: Sequence[str],
        detail: str,
        estimated_cost: float = 0.0,
    ) -> RoutingDecision:
        return RoutingDecision(
            model_id=None,
            model_level=None,
            required_level=required_level,
            estimated_cost_usd=estimated_cost,
            reason="; ".join((*reason_parts, f"deferred: {detail}")),
            deferred=True,
            deferral_reason=deferral_reason,
        )


def estimate_cost(
    model: ModelSpec | str,
    input_tokens: int,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    catalog: ModelCatalog | None = None,
) -> float:
    """Convenience estimator accepting a ``ModelSpec`` or a configured model id."""

    spec = model if isinstance(model, ModelSpec) else (catalog or load_model_catalog()).require(model)
    return spec.estimate_cost(input_tokens, cached_input_tokens, output_tokens)


def route_task(
    request: RoutingRequest,
    catalog: ModelCatalog | None = None,
) -> RoutingDecision:
    return ModelRouter(catalog).route(request)


__all__ = [
    "CapabilityLevel",
    "DEFAULT_CATALOG_FILE",
    "MODEL_CATALOG_ENV",
    "ModelCatalog",
    "ModelCatalogError",
    "ModelRouter",
    "ModelSpec",
    "RoutingDecision",
    "RoutingRequest",
    "estimate_cost",
    "load_catalog",
    "load_model_catalog",
    "route_task",
]
