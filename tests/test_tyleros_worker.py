import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tyleros_worker import (
    format_briefing_date,
    format_today_briefing,
    identity_headers,
    main,
    process_once,
    today_has_material,
    work_and_tick_tokens,
)


EMPTY = {
    "today": "2026-09-04",
    "overdue": [],
    "dueToday": [],
    "upcoming": [],
    "needsTriage": [],
    "expiringSoon": [],
}


class FormatTodayBriefingTests(unittest.TestCase):
    def test_formats_overdue_and_food_without_bodies(self):
        title, body = format_today_briefing(
            {
                "today": "2026-09-04",
                "overdue": [{"id": "1", "title": "Pay rent", "dueOn": "2026-09-01", "body": "secret"}],
                "dueToday": [],
                "upcoming": [{"id": "2", "title": "Call plumber", "dueOn": "2026-09-06"}],
                "needsTriage": [{"id": "3", "title": "Inbox capture"}],
                "expiringSoon": [
                    {
                        "id": "4",
                        "name": "Milk",
                        "location": "fridge",
                        "expiresOn": "2026-09-05",
                        "notes": "do not leak",
                    }
                ],
            }
        )

        self.assertEqual(title, "Today briefing — 4 Sep 2026")
        self.assertIn("## Overdue", body)
        self.assertIn("- Pay rent (due 2026-09-01)", body)
        self.assertIn("## Needs triage", body)
        self.assertIn("- Inbox capture", body)
        self.assertIn("## Next 7 days", body)
        self.assertIn("## Use soon", body)
        self.assertIn("- Milk (fridge, by 2026-09-05)", body)
        self.assertNotIn("secret", body)
        self.assertNotIn("do not leak", body)

    def test_empty_today_is_not_material(self):
        self.assertFalse(today_has_material(EMPTY))
        title, body = format_today_briefing(EMPTY)
        self.assertEqual(title, "Today briefing — 4 Sep 2026")
        self.assertNotIn("Nothing needs you right now.", body)

    def test_titles_make_today_material(self):
        self.assertTrue(today_has_material({**EMPTY, "overdue": [{"title": "Pay rent"}]}))
        self.assertTrue(today_has_material({**EMPTY, "expiringSoon": [{"name": "Milk"}]}))
        self.assertFalse(today_has_material({**EMPTY, "overdue": [{"title": "  "}]}))

    def test_briefing_date_drops_leading_zero(self):
        self.assertEqual(format_briefing_date("2026-09-04"), "4 Sep 2026")


class CredentialIdentityTests(unittest.TestCase):
    def test_instance_credential_does_not_send_kind_and_cannot_tick(self):
        env = {"TYLEROS_RUNTIME_CREDENTIAL": "tylrt_" + "a" * 40}
        headers = identity_headers(env)
        self.assertEqual(headers["X-TylerOS-Role"], "miles")
        self.assertNotIn("X-TylerOS-Runtime-Kind", headers)
        work, tick = work_and_tick_tokens(env)
        self.assertEqual(work, env["TYLEROS_RUNTIME_CREDENTIAL"])
        self.assertEqual(tick, "")

    def test_system_token_still_sends_kind_and_ticks(self):
        env = {"RUNTIME_TOKEN": "system-token-value-that-is-long-enough"}
        headers = identity_headers(env)
        self.assertEqual(headers["X-TylerOS-Runtime-Kind"], "python")
        work, tick = work_and_tick_tokens(env)
        self.assertEqual(work, tick)
        self.assertEqual(work, env["RUNTIME_TOKEN"])

    def test_instance_credential_wins_work_token_over_system(self):
        env = {
            "TYLEROS_RUNTIME_CREDENTIAL": "tylrt_instance",
            "RUNTIME_TOKEN": "system-token-value-that-is-long-enough",
        }
        work, tick = work_and_tick_tokens(env)
        self.assertEqual(work, "tylrt_instance")
        self.assertEqual(tick, "system-token-value-that-is-long-enough")


class ProcessOnceRoutingTests(unittest.TestCase):
    def test_deterministic_job_with_material_still_proposes_locally(self):
        calls: list[tuple[str, str]] = []

        def fake_request(method, url, token, payload=None, identity=True, timeout=30):
            calls.append((method, url))
            if url.split("?", 1)[0].endswith("/jobs/next"):
                return {
                    "job": {"id": "job-1", "kind": "today_briefing"},
                    "run": {"id": "run-1"},
                }
            if url.endswith("/context/today"):
                return {**EMPTY, "dueToday": [{"title": "Review TylerOS runtime PR"}]}
            return {"ok": True}

        with patch("tyleros_worker.request_json", side_effect=fake_request):
            self.assertTrue(process_once("http://localhost:3000", "token"))

        self.assertTrue(any(url.endswith("/complete") for _, url in calls))
        self.assertFalse(any("/brief" in url for _, url in calls))

    def test_ai_job_with_material_posts_brief_not_complete(self):
        calls: list[tuple[str, str]] = []

        def fake_request(method, url, token, payload=None, identity=True, timeout=30):
            calls.append((method, url))
            if url.split("?", 1)[0].endswith("/jobs/next"):
                return {
                    "job": {"id": "job-ai", "kind": "today_briefing_ai"},
                    "run": {"id": "run-ai"},
                }
            if url.endswith("/context/today"):
                return {**EMPTY, "dueToday": [{"title": "Review TylerOS runtime PR"}]}
            if url.endswith("/brief"):
                self.assertEqual(timeout, 60)
                return {"ok": True, "status": "needs_approval"}
            raise AssertionError(f"unexpected {method} {url}")

        with patch("tyleros_worker.request_json", side_effect=fake_request):
            self.assertTrue(process_once("http://localhost:3000", "token", allow_ai=True))

        self.assertTrue(any(url.endswith("/brief") for _, url in calls))
        self.assertFalse(any(url.endswith("/complete") for _, url in calls))

    def test_ai_job_empty_today_completes_without_brief(self):
        calls: list[tuple[str, str]] = []

        def fake_request(method, url, token, payload=None, identity=True, timeout=30):
            calls.append((method, url))
            if url.split("?", 1)[0].endswith("/jobs/next"):
                return {
                    "job": {"id": "job-ai", "kind": "today_briefing_ai"},
                    "run": {"id": "run-ai"},
                }
            if url.endswith("/context/today"):
                return EMPTY
            if url.endswith("/complete"):
                self.assertEqual(payload["usage"]["provider"], "none")
                self.assertEqual(payload["usage"]["model"], "deterministic")
                self.assertNotIn("proposal", payload)
                return {"ok": True}
            raise AssertionError(f"unexpected {method} {url}")

        with patch("tyleros_worker.request_json", side_effect=fake_request):
            self.assertTrue(process_once("http://localhost:3000", "token", allow_ai=True))

        self.assertFalse(any("/brief" in url for _, url in calls))


class NoCostSafetyTests(unittest.TestCase):
    def test_default_refuses_ai_and_unknown_before_reading_context(self):
        for kind in ("today_briefing_ai", "send_email", None):
            with self.subTest(kind=kind):
                calls = []
                def fake_request(method, url, token, **kwargs):
                    calls.append((method, url, kwargs.get("payload")))
                    if url.split("?", 1)[0].endswith("/jobs/next"):
                        return {"job": {"id": "job-safe", "kind": kind}, "run": {"id": "run-safe"}}
                    self.assertTrue(url.endswith("/complete"))
                    self.assertEqual(kwargs["payload"]["status"], "failed")
                    self.assertNotIn("proposal", kwargs["payload"])
                    self.assertEqual(kwargs["payload"]["usage"]["provider"], "none")
                    return {"ok": True}
                with patch("tyleros_worker.request_json", side_effect=fake_request):
                    self.assertTrue(process_once("http://localhost:3000", "token"))
                self.assertEqual(len(calls), 2)

    def test_claim_advertises_only_explicitly_enabled_job_kinds(self):
        for allow_ai in (False, True):
            with self.subTest(allow_ai=allow_ai), patch("tyleros_worker.request_json", return_value={"job": None}) as request:
                self.assertFalse(process_once("http://localhost:3000", "token", allow_ai=allow_ai))
                url = request.call_args.args[1]
                self.assertIn("?kind=today_briefing", url)
                self.assertEqual("&kind=today_briefing_ai" in url, allow_ai)

    def test_unknown_kind_refused_even_with_ai_opt_in(self):
        with patch("tyleros_worker.request_json", side_effect=[
            {"job": {"id": "j", "kind": "arbitrary_request"}, "run": {"id": "r"}}, {"ok": True},
        ]) as request:
            self.assertTrue(process_once("http://localhost:3000", "token", allow_ai=True))
        self.assertEqual(request.call_args.kwargs["payload"]["status"], "failed")

    def test_once_does_not_tick_or_enable_ai_just_because_token_exists(self):
        with patch.dict("os.environ", {"RUNTIME_TOKEN": "x" * 40}, clear=True), \
             patch("tyleros_worker.tick_once") as tick, \
             patch("tyleros_worker.process_once") as process:
            self.assertEqual(main(["--once"]), 0)
        tick.assert_not_called()
        process.assert_called_once_with("http://localhost:3000", "x" * 40, allow_ai=False)

    def test_explicit_scheduler_tick_requires_system_token(self):
        with patch.dict("os.environ", {"TYLEROS_RUNTIME_CREDENTIAL": "x" * 40}, clear=True), \
             patch("tyleros_worker.request_json") as request:
            self.assertEqual(main(["--once", "--tick-schedules"]), 1)
        request.assert_not_called()

    def test_scheduler_tick_only_after_explicit_flag(self):
        with patch.dict("os.environ", {"RUNTIME_TOKEN": "x" * 40}, clear=True), \
             patch("tyleros_worker.tick_once") as tick, \
             patch("tyleros_worker.process_once") as process:
            self.assertEqual(main(["--once", "--tick-schedules"]), 0)
        tick.assert_called_once_with("http://localhost:3000", "x" * 40)
        self.assertFalse(process.call_args.kwargs["allow_ai"])


if __name__ == "__main__":
    unittest.main()
