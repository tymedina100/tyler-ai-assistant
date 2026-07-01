"""Apply the generated Telegram profile pictures for the multi-bot team.

This is intentionally a setup script, not startup behavior. Run it when you want
to update BotFather-facing bot profile photos, and use --dry-run first.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = ROOT / "assets" / "telegram_profiles" / "profile_manifest.json"


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8") as f:
        entries = json.load(f)

    required = {"key", "name", "role", "env_var", "file"}
    for entry in entries:
        missing = required - set(entry)
        if missing:
            raise ValueError(f"Profile manifest entry is missing: {', '.join(sorted(missing))}")
    return entries


def image_path_for(entry: dict[str, str], manifest_path: Path = DEFAULT_MANIFEST_PATH) -> Path:
    return manifest_path.parent / entry["file"]


def select_entries(entries: list[dict[str, str]], agent_keys: list[str] | None, include_all: bool) -> list[dict[str, str]]:
    if include_all:
        return entries

    if not agent_keys:
        raise ValueError("Choose --all or at least one --agent key.")

    by_key = {entry["key"]: entry for entry in entries}
    unknown = [key for key in agent_keys if key not in by_key]
    if unknown:
        raise ValueError(f"Unknown agent key(s): {', '.join(unknown)}")

    return [by_key[key] for key in agent_keys]


def build_profile_photo_payload(attachment_name: str = "profile_photo") -> dict[str, str]:
    return {
        "type": "static",
        "photo": f"attach://{attachment_name}",
    }


def set_profile_photo(token: str, image_path: Path, session=requests) -> dict:
    if image_path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise ValueError(f"Telegram static profile photos must be JPG files: {image_path}")

    url = f"https://api.telegram.org/bot{token}/setMyProfilePhoto"
    attachment_name = "profile_photo"
    payload = build_profile_photo_payload(attachment_name)

    with image_path.open("rb") as image_file:
        response = session.post(
            url,
            data={"photo": json.dumps(payload)},
            files={attachment_name: (image_path.name, image_file, "image/jpeg")},
            timeout=30,
        )

    try:
        body = response.json()
    except ValueError:
        body = {"ok": False, "description": response.text}

    if not getattr(response, "ok", False) or not body.get("ok"):
        description = body.get("description", "Telegram rejected the profile photo update.")
        raise RuntimeError(description)

    return body


def apply_profile(entry: dict[str, str], manifest_path: Path, dry_run: bool, session=requests) -> str:
    image_path = image_path_for(entry, manifest_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Missing image for {entry['key']}: {image_path}")

    if dry_run:
        return f"DRY RUN: {entry['name']} ({entry['key']}) -> {image_path} via {entry['env_var']}"

    token = os.environ.get(entry["env_var"])
    if not token:
        raise RuntimeError(f"Missing {entry['env_var']} for {entry['name']} ({entry['key']}).")

    set_profile_photo(token, image_path, session=session)
    return f"Updated {entry['name']} ({entry['key']}) from {image_path.name}."


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Set generated Telegram bot profile photos.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--agent", action="append", help="Agent key to update, e.g. manager. Can be repeated.")
    parser.add_argument("--all", action="store_true", help="Update every profile in the manifest.")
    parser.add_argument("--dry-run", action="store_true", help="Validate files and print what would happen.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv or sys.argv[1:])

    try:
        entries = load_manifest(args.manifest)
        selected = select_entries(entries, args.agent, args.all)
        for entry in selected:
            print(apply_profile(entry, args.manifest, args.dry_run))
    except Exception as exc:
        print(f"Profile photo update failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
