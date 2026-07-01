import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "set_telegram_profile_photos.py"
MANIFEST_PATH = ROOT / "assets" / "telegram_profiles" / "profile_manifest.json"


spec = importlib.util.spec_from_file_location("set_telegram_profile_photos", SCRIPT_PATH)
telegram_profiles = importlib.util.module_from_spec(spec)
spec.loader.exec_module(telegram_profiles)


class FakeResponse:
    ok = True
    text = ""

    def json(self):
        return {"ok": True, "result": True}


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, data, files, timeout):
        self.calls.append({"url": url, "data": data, "files": files, "timeout": timeout})
        return FakeResponse()


class TelegramProfileTests(unittest.TestCase):
    def test_manifest_has_expected_agent_profiles_and_files(self):
        entries = telegram_profiles.load_manifest(MANIFEST_PATH)
        keys = {entry["key"] for entry in entries}

        self.assertEqual(
            keys,
            {"manager", "code", "research", "news", "write", "task", "tasks", "weather", "calendar", "gmail"},
        )

        for entry in entries:
            image_path = telegram_profiles.image_path_for(entry, MANIFEST_PATH)
            self.assertTrue(image_path.exists(), image_path)
            self.assertEqual(image_path.suffix.lower(), ".jpg")

    def test_select_entries_rejects_unknown_agent(self):
        entries = telegram_profiles.load_manifest(MANIFEST_PATH)

        with self.assertRaises(ValueError):
            telegram_profiles.select_entries(entries, ["nobody"], include_all=False)

    def test_dry_run_does_not_require_token(self):
        entry = telegram_profiles.load_manifest(MANIFEST_PATH)[0]

        with patch.dict("os.environ", {}, clear=True):
            result = telegram_profiles.apply_profile(entry, MANIFEST_PATH, dry_run=True)

        self.assertIn("DRY RUN", result)
        self.assertIn(entry["env_var"], result)

    def test_set_profile_photo_posts_static_jpg_payload(self):
        session = FakeSession()
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "avatar.jpg"
            image_path.write_bytes(b"fake-jpg")
            result = telegram_profiles.set_profile_photo("123:secret", image_path, session=session)

        self.assertTrue(result["ok"])
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], "https://api.telegram.org/bot123:secret/setMyProfilePhoto")
        self.assertEqual(call["timeout"], 30)
        self.assertEqual(json.loads(call["data"]["photo"]), {"type": "static", "photo": "attach://profile_photo"})
        self.assertEqual(call["files"]["profile_photo"][2], "image/jpeg")


if __name__ == "__main__":
    unittest.main()
