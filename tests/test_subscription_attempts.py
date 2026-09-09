import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from codex_subscription import SubscriptionUnavailable
from subscription_attempts import analyze_once


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "attempts.db")
        self.options = dict(attempt_id=str(uuid4()), ledger_path=self.path,
                            binary="/trusted/codex", model="explicit", effort="low", enabled=True)

    def test_completed_result_survives_connection_restart_without_prompt_storage(self):
        receipt = {"summary": "Approved fixture result", "usage": {"output_tokens": 7}}
        with patch("subscription_attempts.analyze", return_value=receipt) as execute:
            self.assertEqual(analyze_once("private fixture input", **self.options), receipt)
            self.assertEqual(analyze_once("private fixture input", **self.options), receipt)
            execute.assert_called_once()
        self.assertNotIn(b"private fixture input", Path(self.path).read_bytes())
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(SubscriptionUnavailable, "different"):
            analyze_once("changed input", **self.options)

    def test_concurrent_account_attempt_cannot_start_another_model(self):
        started, release = threading.Event(), threading.Event()
        def execute(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(5))
            return {"summary": "one invocation"}
        with patch("subscription_attempts.analyze", side_effect=execute) as run, ThreadPoolExecutor() as pool:
            future = pool.submit(analyze_once, "first", **self.options)
            self.assertTrue(started.wait(5))
            try:
                with self.assertRaisesRegex(SubscriptionUnavailable, "holds"):
                    analyze_once("second", **(self.options | {"attempt_id": str(uuid4())}))
                with self.assertRaisesRegex(SubscriptionUnavailable, "Previous attempt"):
                    analyze_once("first", **self.options)
            finally:
                release.set()
            self.assertEqual(future.result()["summary"], "one invocation")
            run.assert_called_once()

    def test_failure_is_durable_and_error_details_are_not_stored(self):
        with patch("subscription_attempts.analyze", side_effect=RuntimeError("private secret detail")) as run:
            with self.assertRaises(RuntimeError):
                analyze_once("fixture", **self.options)
            with self.assertRaises(SubscriptionUnavailable):
                analyze_once("fixture", **self.options)
            run.assert_called_once()
        self.assertNotIn(b"private secret detail", Path(self.path).read_bytes())

    def test_interrupted_attempt_remains_held_after_reopen(self):
        script = "import os,sys,json,subscription_attempts as a; a.analyze=lambda *args,**kwargs: os._exit(9); a.analyze_once('fixture',**json.loads(sys.argv[1]))"
        child = subprocess.run([sys.executable, "-c", script, json.dumps(self.options)], timeout=10, check=False)
        self.assertEqual(child.returncode, 9)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("select status from attempts").fetchone()[0], "running")
        with patch("subscription_attempts.analyze") as execute:
            with self.assertRaises(SubscriptionUnavailable):
                analyze_once("fixture", **self.options)
            execute.assert_not_called()
