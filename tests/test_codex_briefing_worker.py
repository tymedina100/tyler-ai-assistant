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
            self.assertEqual(resume_run("http://localhost:3004","fixture",run_id,**options),{"jobStatus":"needs_approval"})
            execute.assert_called_once()
            self.assertEqual(completed[0],completed[1])
            self.assertEqual(completed[0]["usage"]["outputTokens"],5)
