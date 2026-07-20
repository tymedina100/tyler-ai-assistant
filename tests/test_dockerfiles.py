"""Guards the Dockerfiles' explicit COPY lists against the deployed code.

Both images COPY a hand-maintained list of .py files. When a new module is added
and imported (even lazily, inside a function) without updating the list, the
container builds fine but crashes at startup or at tool time with
ModuleNotFoundError - exactly how office_metrics.py took down the Railway deploy.
These tests compute each entry point's local-import closure from the source and
fail if any module in it is missing from that Dockerfile's COPY lines.
"""
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module_imports(text):
    """Top-level module names imported anywhere in the file (any indentation, so
    lazy function-level imports count - they still need the file in the image)."""
    names = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("from "):
            names.add(stripped.split()[1].split(".")[0])
        elif stripped.startswith("import "):
            for part in stripped[len("import "):].split(","):
                names.add(part.strip().split(" as ")[0].split(".")[0])
    return names


def _import_closure(entry_module):
    """All repo-local modules reachable from entry_module via import statements."""
    local = {path.stem for path in ROOT.glob("*.py")}
    seen = set()
    queue = [entry_module]
    while queue:
        name = queue.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        text = (ROOT / f"{name}.py").read_text(encoding="utf-8")
        queue.extend(_module_imports(text) & local - seen)
    return seen


def _copied_modules(dockerfile_name):
    """Module names of the .py files a Dockerfile COPYs into the image."""
    copied = set()
    for line in (ROOT / dockerfile_name).read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts[:1] == ["COPY"]:
            copied.update(Path(p).stem for p in parts[1:-1] if p.endswith(".py"))
    return copied


class DockerfileCopyClosureTests(unittest.TestCase):
    def assert_closure_copied(self, dockerfile_name, entry_module):
        missing = _import_closure(entry_module) - _copied_modules(dockerfile_name)
        self.assertEqual(
            missing, set(),
            f"{dockerfile_name} doesn't COPY module(s) {sorted(missing)} needed by "
            f"{entry_module}.py - the container would crash with ModuleNotFoundError.",
        )

    def test_group_image_copies_every_imported_module(self):
        self.assert_closure_copied("Dockerfile.group", "group_bot")

    def test_single_bot_image_copies_every_imported_module(self):
        self.assert_closure_copied("Dockerfile", "bot")

    def test_closure_sees_lazy_imports(self):
        # main.py imports company_mode/gumroad_helpers only inside functions; the
        # closure must still include them, or images silently ship broken tools.
        closure = _import_closure("bot")
        self.assertIn("company_mode", closure)
        self.assertIn("gumroad_helpers", closure)

    def test_group_closure_includes_office_metrics(self):
        # Regression: office_api imports office_metrics; missing it from
        # Dockerfile.group is what crashed the Railway deploy.
        self.assertIn("office_metrics", _import_closure("group_bot"))


if __name__ == "__main__":
    unittest.main()
