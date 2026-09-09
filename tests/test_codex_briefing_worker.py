import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from codex_subscription import QuotaEvidence, SubscriptionUnavailable
from codex_briefing_worker import resume_run, process_once

class BriefingWorkerTests(unittest.TestCase):
    @patch("codex_briefing_worker.request_json")
    def test_disabled_never_claims(self, request):
        with self.assertRaises(SubscriptionUnavailable):
            process_once("http://localhost:3004", "fixture", enabled=False)
        request.assert_not_called()

    @patch("codex_briefing_worker.analyze_once")
    @patch("codex_briefing_worker.request_json", return_value={"status":"succeeded"})
    def test_empty_day_never_calls_model(self, request, analyze):
        resume_run("http://localhost:3004", "fixture", str(uuid4()), ledger_path="unused", binary="unused", quota=QuotaEvidence(time.time(),0), enabled=True)
        analyze.assert_not_called()

    def test_lost_completion_response_reuses_local_model_receipt(self):
        run_id = str(uuid4())
        prepared = {"status":"prepared", "request":{"runId":run_id,"model":"explicit","effort":"low","prompt":"frozen context"}}
        judgment = {"summary":"Review worksheet", "priorities":[], "needsTyler":[], "watch":[]}
        completed = []
        def transport(method, url, token, **kwargs):
            if url.endswith("/prepare"): return prepared
            completed.append(kwargs["payload"])
            if len(completed) == 1: raise RuntimeError("Temporary transport interruption")
            return {"jobStatus":"needs_approval"}
        with tempfile.TemporaryDirectory() as directory, patch("codex_briefing_worker.request_json", side_effect=transport), patch("subscription_attempts.analyze", return_value={"judgment":judgment,"usage":{"input_tokens":10,"output_tokens":5}}) as execute:
            options = dict(ledger_path=str(Path(directory)/"attempts.db"),binary="/trusted/codex",quota=QuotaEvidence(time.time(),0),enabled=True)
            with self.assertRaises(RuntimeError): resume_run("http://localhost:3004","fixture",run_id,**options)
            self.assertEqual(resume_run("http://localhost:3004","fixture",run_id,delivery_only=True,**options),{"jobStatus":"needs_approval"})
            execute.assert_called_once()
            self.assertEqual(completed[0],completed[1])
            self.assertEqual(completed[0]["usage"]["outputTokens"],5)

    def test_delivery_only_unknown_receipt_does_not_contact_server_or_model(self):
        with tempfile.TemporaryDirectory() as directory, patch("codex_briefing_worker.request_json") as request, patch("codex_briefing_worker.analyze_once") as analyze:
            from subscription_attempts import _connect
            ledger = str(Path(directory) / "attempts.db")
            _connect(ledger).close()
            with self.assertRaisesRegex(SubscriptionUnavailable, "completed local receipt"):
                resume_run("http://localhost:3004", "fixture", str(uuid4()), ledger_path=ledger, binary="unused", enabled=True, delivery_only=True)
            request.assert_not_called()
            analyze.assert_not_called()

    def test_inspect_cli_needs_no_runtime_credentials_or_binary(self):
        import contextlib
        import io
        from codex_briefing_worker import main
        with patch("sys.argv", ["worker", "--inspect-ledger", "--ledger", "/private/ledger.db"]), patch("codex_briefing_worker.inspect_attempts", return_value=[]) as inspect, patch("codex_briefing_worker.work_and_tick_tokens") as credentials, patch("codex_briefing_worker.read_quota") as quota, contextlib.redirect_stdout(io.StringIO()) as output:
            main()
            self.assertEqual(output.getvalue().strip(), "[]")
            inspect.assert_called_once_with("/private/ledger.db")
            credentials.assert_not_called()
            quota.assert_not_called()

    def test_response_lost_after_server_commit_is_acknowledged_without_second_delivery(self):
        run_id = str(uuid4())
        prepared = {"status": "prepared", "request": {"runId": run_id, "model": "explicit", "effort": "low", "prompt": "frozen context"}}
        committed = False
        deliveries = []
        def transport(method, url, token, **kwargs):
            nonlocal committed
            if url.endswith("/prepare"):
                return {"status": "succeeded", "jobStatus": "needs_approval"} if committed else prepared
            deliveries.append(kwargs["payload"])
            committed = True
            raise RuntimeError("Response lost after server commit")
        with tempfile.TemporaryDirectory() as directory, patch("codex_briefing_worker.request_json", side_effect=transport), patch("subscription_attempts.analyze", return_value={"judgment": {"summary": "fixture", "priorities": [], "needsTyler": [], "watch": []}}) as execute:
            options = dict(ledger_path=str(Path(directory)/"attempts.db"), binary="/trusted/codex", enabled=True)
            with self.assertRaises(RuntimeError):
                resume_run("http://localhost:3004", "fixture", run_id, **options)
            result = resume_run("http://localhost:3004", "fixture", run_id, delivery_only=True, **options)
            self.assertEqual(result, {"status": "succeeded", "jobStatus": "needs_approval"})
            execute.assert_called_once()
            self.assertEqual(len(deliveries), 1)

    def test_interrupted_ledger_leaves_next_job_unclaimed(self):
        import sqlite3
        from subscription_attempts import _connect
        with tempfile.TemporaryDirectory() as directory, patch("codex_briefing_worker.request_json") as request:
            ledger = str(Path(directory)/"attempts.db")
            db = _connect(ledger)
            db.execute("insert into attempts(id,request_hash,status,started_at) values(?,?,'running',?)", (str(uuid4()), 'fixture', time.time()))
            db.close()
            with self.assertRaisesRegex(SubscriptionUnavailable, "no new job claimed"):
                process_once("http://localhost:3004", "fixture", ledger_path=ledger, binary="/trusted/codex", enabled=True, quota=QuotaEvidence(time.time(),0))
            request.assert_not_called()
            with sqlite3.connect(ledger) as db:
                self.assertEqual(db.execute("select status from attempts").fetchone()[0], "running")

    def test_new_ledger_admits_empty_queue_without_inference(self):
        with tempfile.TemporaryDirectory() as directory, patch("codex_briefing_worker.request_json", return_value={"job": None}) as request, patch("codex_briefing_worker.analyze_once") as analyze:
            result = process_once("http://localhost:3004", "fixture", ledger_path=str(Path(directory)/"attempts.db"), binary="/trusted/codex", enabled=True, quota=QuotaEvidence(time.time(),0))
            self.assertIsNone(result)
            request.assert_called_once()
            analyze.assert_not_called()
