"""Tests for the Claude provider's context meter, /compact and per-model efforts.

The event shapes are real ones, captured from CLI 2.1.304 (haiku, no tools)."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers


def run(coro):
    return asyncio.run(coro)


class ClaudeProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = providers.ClaudeProvider()
        self.chat = SimpleNamespace(
            id="chat-1",
            dir=Path("/tmp/chat-1"),
            meta={"provider": "claude", "requested_model": "haiku", "model": "claude-haiku-5-5", "session_id": "S"},
            turn_state={},
            emit=AsyncMock(),
            save_meta=MagicMock(),
        )
        self.proc = SimpleNamespace(stdin=MagicMock())

    def event(self, e):
        run(self.provider.on_event(self.chat, self.proc, e, lambda *_: None))

    def test_flags(self):
        self.assertTrue(self.provider.supports_compact)

    def test_context_is_the_last_calls_whole_input(self):
        # cached + fresh input all count: the model read all of it.
        self.event({"type": "assistant", "message": {"content": [], "usage": {
            "input_tokens": 2, "cache_creation_input_tokens": 2899, "cache_read_input_tokens": 649, "output_tokens": 4}}})
        self.assertEqual(self.chat.meta["context_tokens"], 3550)

    def test_subagent_calls_do_not_move_the_meter(self):
        self.chat.meta["context_tokens"] = 3550
        self.event({"type": "assistant", "parent_tool_use_id": "toolu_1", "message": {"content": [], "usage": {"input_tokens": 90000}}})
        self.assertEqual(self.chat.meta["context_tokens"], 3550)

    def test_result_reports_the_live_window_not_a_guess(self):
        self.assertEqual(self.provider.get_context_window(self.chat), 200_000)  # nothing heard yet
        self.chat.meta["context_tokens"] = 3550
        self.event({"type": "result", "subtype": "success", "modelUsage": {"claude-haiku-5-5": {"contextWindow": 1_000_000}}})
        result = self.chat.emit.call_args.args[0]
        self.assertEqual((result["type"], result["context_tokens"], result["context_window"]), ("result", 3550, 1_000_000))
        self.proc.stdin.close.assert_called_once()

    def test_window_belongs_to_the_model_that_ran(self):
        self.event({"type": "result", "modelUsage": {"claude-haiku-5-5": {"contextWindow": 1_000_000}}})
        self.chat.meta["requested_model"] = "sonnet"  # switched mid-chat: old window must not leak
        self.assertEqual(self.provider.get_context_window(self.chat), 200_000)

    def test_main_model_window_wins_over_a_subagents(self):
        self.event({"type": "result", "modelUsage": {
            "claude-sonnet-5-5": {"contextWindow": 200_000}, "claude-haiku-5-5": {"contextWindow": 1_000_000}}})
        self.assertEqual(self.chat.meta["context_window"], 1_000_000)

    def test_compact_boundary_resets_the_meter_to_the_summary_size(self):
        # the compact turn's own result carries zero usage, so the boundary is the only source
        self.chat.meta["context_tokens"] = 4331
        self.event({"type": "system", "subtype": "compact_boundary", "compact_metadata": {
            "trigger": "manual", "pre_tokens": 4331, "post_tokens": 682}})
        self.assertEqual(self.chat.meta["context_tokens"], 682)
        text = self.chat.emit.call_args.args[0]
        self.assertEqual(text["type"], "text")
        self.assertIn("4331", text["text"])
        self.assertIn("682", text["text"])

    def test_every_model_has_its_own_effort_list(self):
        for model in self.provider.models:
            self.assertEqual(self.provider.efforts_for(model["id"]), providers.CLAUDE_EFFORTS)
        self.chat.meta["effort"] = "max"
        self.assertEqual(self.provider.effort_for(self.chat.meta), "max")
        self.chat.meta["effort"] = "bogus"
        self.assertIsNone(self.provider.effort_for(self.chat.meta))

    def test_usage_payload_becomes_windows_and_credits(self):
        # shape of GET /api/oauth/usage (percent, ISO times, one codename block with dollars)
        usage = {
            "five_hour": {"utilization": 14.0, "resets_at": "2026-10-21T05:00:00.234140+00:00"},
            "seven_day": {"utilization": 59.0, "resets_at": "2026-10-23T09:00:00+00:00"},
            "seven_day_opus": None,
            "extra_usage": {"is_enabled": False, "limit_dollars": None},
            "iguana_necktie": {"utilization": 0.0, "resets_at": "2026-11-05T07:59:00+00:00", "limit_dollars": 100, "used_dollars": 0.0},
        }
        by_id = {w["id"]: w for w in self.provider.usage_windows(usage)}
        self.assertEqual(set(by_id), {"five_hour", "seven_day", "cloud_credits"})
        self.assertAlmostEqual(by_id["five_hour"]["utilization"], 0.14)
        self.assertEqual(by_id["five_hour"]["resets_at"], 1792558800)
        self.assertIn("$100", by_id["cloud_credits"]["label"])

    def test_a_turns_event_keeps_the_credit_reading(self):
        providers.limits.update("claude", None, [{"id": "cloud_credits", "utilization": 0.0, "resets_at": 1}])
        self.provider.record_limits({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 2}}})
        ids = {w["id"] for w in providers.limits.data["claude"]["windows"]}
        self.assertEqual(ids, {"five_hour", "cloud_credits"})

    def test_expired_login_is_renewed_by_the_cli_then_retried(self):
        from unittest.mock import AsyncMock, patch
        usage = {"five_hour": {"utilization": 10.0, "resets_at": "2026-10-21T05:00:00+00:00"}}
        calls = iter([providers.TokenExpired("expired"), usage])

        def read():
            item = next(calls)
            if isinstance(item, Exception):
                raise item
            return item

        with patch.object(self.provider, "_read_usage", side_effect=read), \
             patch.object(self.provider, "renew_login", AsyncMock()) as renew:
            run(self.provider.probe_limits())
        renew.assert_awaited_once()
        self.assertAlmostEqual(providers.limits.data["claude"]["windows"][0]["utilization"], 0.10)


if __name__ == "__main__":
    unittest.main()
