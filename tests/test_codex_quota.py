import unittest
from unittest.mock import patch
from codex_quota import quota_from_response
from codex_subscription import SubscriptionUnavailable, QuotaEvidence
from codex_briefing_worker import process_once

class QuotaTests(unittest.TestCase):
    def response(self, primary=20, secondary=75):
        return {"rateLimitsByLimitId":{"codex":{"limitId":"codex","primary":{"usedPercent":primary,"resetsAt":200},"secondary":{"usedPercent":secondary,"resetsAt":300}}}}
    def test_uses_worst_window_not_most_generous(self):
        self.assertEqual(quota_from_response(self.response(),now=100).used_percent,75)
    def test_unknown_bucket_missing_window_and_invalid_numbers_fail_closed(self):
        for response in ({}, {"rateLimitsByLimitId":{}}, self.response(True), self.response(float('nan')),self.response(101)):
            with self.assertRaises(SubscriptionUnavailable): quota_from_response(response,now=100)
        with self.assertRaises(SubscriptionUnavailable): quota_from_response(self.response(),now=201)
    def test_legacy_bucket_and_exhaustion(self):
        record=self.response()["rateLimitsByLimitId"]["codex"]
        record["rateLimitReachedType"]="weekly"
        self.assertTrue(quota_from_response({"rateLimits":record},now=100).exhausted)
    @patch('codex_briefing_worker.request_json')
    @patch('codex_briefing_worker.read_quota',side_effect=SubscriptionUnavailable('offline'))
    def test_unavailable_quota_leaves_jobs_unclaimed(self, quota, request):
        with self.assertRaises(SubscriptionUnavailable): process_once('https://example.test','fixture',enabled=True,binary='/trusted/codex')
        request.assert_not_called()
    @patch('codex_briefing_worker.request_json')
    def test_exhausted_and_stale_manual_evidence_leaves_jobs_unclaimed(self, request):
        for quota in (QuotaEvidence(0,0),QuotaEvidence(100,99)):
            with self.assertRaises(SubscriptionUnavailable): process_once('https://example.test','fixture',enabled=True,binary='/trusted/codex',quota=quota)
        request.assert_not_called()
