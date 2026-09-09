import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.tyleros_launchd import agent_plist, main, read_config


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "worker.json"
        self.config = {"TYLEROS_URL": "https://example.vercel.app", "TYLEROS_RUNTIME_CREDENTIAL": "tylrt_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"}
        self.write()

    def write(self):
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)

    def test_private_config_and_secret_free_plist(self):
        self.assertEqual(read_config(self.path)["TYLEROS_POLL_SECONDS"], "30")
        plist = agent_plist(self.path)
        self.assertNotIn(self.config["TYLEROS_RUNTIME_CREDENTIAL"], str(plist))
        self.assertNotIn("EnvironmentVariables", plist)
        self.assertNotIn("--allow-ai", str(plist))

    def test_rejects_world_readable_and_symlink(self):
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_config(self.path)
        self.path.chmod(0o600)
        link = self.path.with_suffix(".link")
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            read_config(link)

    def test_rejects_provider_keys_insecure_urls_and_bad_poll(self):
        for key, value in [("OPENAI_API_KEY", "fixture"), ("TYLEROS_URL", "http://example.com"), ("TYLEROS_URL", "https://example.com/?token=secret"), ("TYLEROS_POLL_SECONDS", "nan"), ("TYLEROS_POLL_SECONDS", "0"), ("TYLEROS_VERCEL_PROTECTION_BYPASS", "a\nb")]:
            with self.subTest(key=key, value=value):
                original = self.config.copy()
                self.config[key] = value
                self.write()
                with self.assertRaises(ValueError):
                    read_config(self.path)
                self.config = original

    def test_runner_strips_inherited_provider_and_system_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture", "RUNTIME_TOKEN": "fixture"}), patch("sys.argv", ["runner", "run", str(self.path)]), patch("os.execve") as execute:
            self.assertEqual(main(), 0)
            args = execute.call_args.args
            self.assertEqual(len(args[1]), 2)
            self.assertNotIn("OPENAI_API_KEY", args[2])
            self.assertNotIn("RUNTIME_TOKEN", args[2])
            self.assertEqual(args[2]["TYLEROS_RUNTIME_CREDENTIAL"], self.config["TYLEROS_RUNTIME_CREDENTIAL"])


if __name__ == "__main__":
    unittest.main()
