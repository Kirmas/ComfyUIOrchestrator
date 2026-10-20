"""Tests for Antigravity provider (effort, fast mode, quotas and limits)."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers


class AgyProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = providers.AgyProvider()
        self.chat = SimpleNamespace(
            id="chat-1",
            dir=Path("/tmp/chat-1"),
            meta={"provider": "agy", "requested_model": "gemini-3.8-flash-medium", "permission_mode": "edits"},
            turn_state={},
            emit=AsyncMock(),
        )

    def test_describe_knobs(self):
        d = self.provider.describe()
        self.assertEqual(d["efforts"], ["low", "medium", "high"])
        self.assertFalse(d["supports_fast"])
        self.assertEqual(d["fast_models"], [])
        self.assertTrue(d["supports_compact"])
        gpt = next((m for m in d["models"] if m["id"] == "gpt-oss-120b"), None)
        self.assertIsNotNone(gpt)
        self.assertEqual(gpt["efforts"], ["medium"])
        self.assertEqual(self.provider.context_window("gemini-3.8-flash"), 1_048_576)
        self.assertEqual(self.provider.context_window("claude-sonnet-5-5"), 200_000)
        self.assertEqual(self.provider.context_window("gpt-oss-120b"), 128_000)

    def test_launch_with_effort_strips_suffix(self):
        self.chat.meta["effort"] = "high"
        launch = self.provider.launch(self.chat, providers.TurnSpec("system"), "hello", [])
        self.assertIn("--model", launch.args)
        model_idx = launch.args.index("--model")
        self.assertEqual(launch.args[model_idx + 1], "gemini-3.8-flash")
        self.assertIn("--effort", launch.args)
        effort_idx = launch.args.index("--effort")
        self.assertEqual(launch.args[effort_idx + 1], "high")

    def test_launch_without_effort_normalizes_to_base_model(self):
        self.chat.meta["effort"] = None
        launch = self.provider.launch(self.chat, providers.TurnSpec("system"), "hello", [])
        self.assertIn("--model", launch.args)
        model_idx = launch.args.index("--model")
        self.assertEqual(launch.args[model_idx + 1], "gemini-3.8-flash")
        self.assertNotIn("--effort", launch.args)

    def test_parse_reset_seconds(self):
        self.assertEqual(self.provider._parse_reset_seconds("Resets in 98h37m1s"), 98 * 3600 + 37 * 60 + 1)
        self.assertEqual(self.provider._parse_reset_seconds("Resets in 51h15m33s."), 51 * 3600 + 15 * 60 + 33)
        self.assertEqual(self.provider._parse_reset_seconds("Resets in 4d 6h"), 4 * 86400 + 6 * 3600)
        self.assertIsNone(self.provider._parse_reset_seconds("Some other error"))

    def test_get_context_tokens_reads_transcript(self):
        import tempfile
        provider = providers.AgyProvider()
        with tempfile.TemporaryDirectory() as tmpdir:
            t_dir = Path(tmpdir) / ".gemini/antigravity-cli/brain/test-sess/.system_generated/logs"
            t_dir.mkdir(parents=True)
            t_file = t_dir / "transcript.jsonl"
            t_file.write_text(
                '{"step_index": 1, "input_tokens": 1000, "cache_read_tokens": 4000}\n'
                '{"step_index": 2, "input_tokens": 2000, "cache_read_tokens": 5000}\n'
            )
            chat = SimpleNamespace(meta={"session_id": "test-sess"})
            with patch("pathlib.Path.home", return_value=Path(tmpdir)):
                tokens = provider.get_context_tokens(chat)
                self.assertEqual(tokens, 7000)
        self.assertEqual(self.provider._parse_reset_seconds("Resets in 98h37m1s"), 98 * 3600 + 37 * 60 + 1)
        self.assertEqual(self.provider._parse_reset_seconds("Resets in 51h15m33s."), 51 * 3600 + 15 * 60 + 33)
        self.assertEqual(self.provider._parse_reset_seconds("Resets in 4d 6h"), 4 * 86400 + 6 * 3600)
        self.assertIsNone(self.provider._parse_reset_seconds("Some other error"))

    def test_record_quota_error(self):
        with patch.object(providers.limits, "update") as mock_update:
            err = "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 51h15m33s."
            recorded = self.provider.record_quota_error("claude-sonnet-5-5-medium", err)
            self.assertTrue(recorded)
            mock_update.assert_called_once()
            provider_id, status, windows = mock_update.call_args.args
            self.assertEqual(provider_id, "agy")
            self.assertEqual(status, "rejected")
            target_window = next((w for w in windows if w["id"] == "claude-sonnet-5-5-medium"), None)
            self.assertIsNotNone(target_window)
            self.assertEqual(target_window["utilization"], 1.0)
            self.assertIsNotNone(target_window["resets_at"])

    def test_record_limits_from_usage_payload(self):
        payload = {
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "remaining_fraction": 0.90,
                            "reset_time": "2026-10-24T23:26:05Z",
                        },
                        {
                            "id": "gemini-5h",
                            "remaining_fraction": 0.95,
                            "reset_time": "2026-10-20T20:20:09Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "remaining_fraction": 0.0,
                            "reset_time": "2026-10-22T20:25:17Z",
                        },
                        {
                            "id": "3p-5h",
                            "disabled": True,
                            "remaining_fraction": 1.0,
                        },
                    ],
                },
            ]
        }
        with patch.object(providers.limits, "update") as mock_update:
            ok = self.provider.record_limits(payload)
            self.assertTrue(ok)
            mock_update.assert_called_once()
            provider_id, status, windows = mock_update.call_args.args
            self.assertEqual(provider_id, "agy")
            self.assertEqual(status, "rejected")  # 3p-weekly has utilization 1.0
            by_id = {w["id"]: w for w in windows}
            self.assertIn("gemini-weekly", by_id)
            self.assertEqual(by_id["gemini-weekly"]["utilization"], 0.10)
            self.assertEqual(by_id["gemini-weekly"]["group"], "Gemini Models")
            self.assertIn("gemini-5h", by_id)
            self.assertEqual(by_id["gemini-5h"]["utilization"], 0.05)
            self.assertEqual(by_id["gemini-5h"]["group"], "Gemini Models")
            self.assertIn("3p-weekly", by_id)
            self.assertEqual(by_id["3p-weekly"]["utilization"], 1.0)
            self.assertEqual(by_id["3p-weekly"]["group"], "Claude and GPT models")
            self.assertIn("3p-5h", by_id)
            self.assertEqual(by_id["3p-5h"]["group"], "Claude and GPT models")
            self.assertTrue(by_id["3p-5h"]["disabled"])


class AgyOnEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_event_quota_error(self):
        provider = providers.AgyProvider()
        chat = SimpleNamespace(
            id="chat-2",
            meta={"provider": "agy", "requested_model": "claude-sonnet-5-5-medium", "permission_mode": "edits"},
            turn_state={},
            emit=AsyncMock(),
        )
        proc = SimpleNamespace(stdin=AsyncMock(), returncode=None)
        err = "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 51h15m33s."
        event = {
            "event": "result",
            "result": {
                "status": "ERROR",
                "error": err,
                "duration_seconds": 1.0,
                "num_turns": 1,
            },
        }
        with patch.object(provider, "record_quota_error") as mock_record:
            await provider.on_event(chat, proc, event, None)
            mock_record.assert_called_once_with("claude-sonnet-5-5", err)
            self.assertTrue(chat.turn_state.get("failed"))

    def test_permission_modes_and_interactive(self):
        provider = providers.AgyProvider()
        self.assertEqual(provider.permission_modes, ["edits", "auto", "full", "plan"])
        self.assertTrue(provider.interactive)
        self.assertTrue(provider.accepts_images)

    def test_launch_with_images_injects_prompt_hint(self):
        provider = providers.AgyProvider()
        chat = SimpleNamespace(
            id="chat-img",
            meta={"provider": "agy", "requested_model": "gemini-3.8-flash-low", "permission_mode": "edits"},
            turn_state={},
            emit=AsyncMock(),
        )
        launch = provider.launch(chat, providers.TurnSpec("sys"), "Look at this", [Path("/tmp/pic.png")])
        prompt_arg = next(a for a in launch.args if a.startswith("-p="))
        self.assertIn("/tmp/pic.png", prompt_arg)
        self.assertIn("read_file", prompt_arg)

    def test_launch_env_and_skip_permissions(self):
        provider = providers.AgyProvider()
        chat = SimpleNamespace(
            id="chat-123",
            meta={"provider": "agy", "requested_model": "gemini-3.8-flash-low", "permission_mode": "plan"},
            turn_state={},
            emit=AsyncMock(),
        )
        launch = provider.launch(chat, providers.TurnSpec("sys"), "test prompt", [])
        self.assertIn("--dangerously-skip-permissions", launch.args)
        self.assertEqual(launch.env["COMFY_CHAT_ID"], "chat-123")
        self.assertEqual(launch.env["COMFY_PERMISSION_MODE"], "plan")
        self.assertIn("COMFY_AGENT_RUNNER_IPC", launch.env)

    async def test_limits_scheduler_invokes_probe_limits(self):
        import runner
        with patch.object(runner, "LIMITS_INITIAL_DELAY_SECONDS", 0.005), \
             patch.object(runner, "LIMITS_POLL_INTERVAL_SECONDS", 0.01), \
             patch.object(providers.PROVIDERS["agy"], "probe_limits", new_callable=AsyncMock) as mock_probe:
            task = runner.asyncio.create_task(runner.limits_scheduler())
            await runner.asyncio.sleep(0.03)
            task.cancel()
            try:
                await task
            except runner.asyncio.CancelledError:
                pass
            mock_probe.assert_called()


