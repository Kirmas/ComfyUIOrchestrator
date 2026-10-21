"""Tests for Antigravity provider (effort, fast mode, quotas and limits)."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    def setUp(self):
        self.provider = providers.AgyProvider()
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

    def test_project_chat_launches_in_project_mode(self):
        provider = providers.AgyProvider()
        chat = SimpleNamespace(id="chat-p", meta={"provider": "agy", "requested_model": "gemini-3.8-flash"},
                               turn_state={}, emit=AsyncMock())
        spec = providers.TurnSpec("sys", isolated=True)
        with patch.object(provider, "_check_project_setup"):
            launch = provider.launch(chat, spec, "hi", [])
        self.assertEqual(launch.env["COMFY_PERMISSION_MODE"], "project")

    def test_bridge_refuses_commands_in_project_mode(self):
        import subprocess
        def decide(tool):
            env = {"COMFY_AGENT_RUNNER_IPC": "http://127.0.0.1:1/none", "COMFY_CHAT_ID": "c",
                   "COMFY_PERMISSION_MODE": "project", "PATH": "/usr/bin:/bin"}
            out = subprocess.run([sys.executable, str(providers.BRIDGE_SCRIPT)], env=env, text=True,
                                 input=json.dumps({"toolCall": {"name": tool, "args": {}}}),
                                 capture_output=True, timeout=10).stdout
            return json.loads(out)["decision"]
        self.assertEqual(decide("run_command"), "deny")
        self.assertEqual(decide("notebook_execution"), "deny")
        self.assertEqual(decide("write_to_file"), "deny")
        self.assertEqual(decide("replace_file_content"), "deny")
        self.assertEqual(decide("read_file"), "allow")
        self.assertEqual(decide("generate_image"), "allow")
        self.assertEqual(decide("call_mcp_tool"), "allow")

    async def test_limits_scheduler_invokes_probe_limits(self):
        import runner
        with patch.object(runner, "LIMITS_INITIAL_DELAY_SECONDS", 0.005), \
             patch.object(runner, "LIMITS_POLL_INTERVAL_SECONDS", 0.01), \
             patch.object(providers.PROVIDERS["codex"], "probe_limits", new_callable=AsyncMock), \
             patch.object(providers.PROVIDERS["agy"], "probe_limits", new_callable=AsyncMock) as mock_probe:
            task = runner.asyncio.create_task(runner.limits_scheduler())
            await runner.asyncio.sleep(0.03)
            task.cancel()
            try:
                await task
            except runner.asyncio.CancelledError:
                pass
            mock_probe.assert_called()

    IMAGE_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"

    async def test_recover_brain_images_and_skip_system_and_symlinks(self):
        import tempfile
        session = "11111111-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            brain = home / ".gemini/antigravity-cli/brain" / session
            brain.mkdir(parents=True)
            (brain / "pic.png").write_bytes(self.IMAGE_PNG)
            (brain / "symlink.png").symlink_to("/etc/passwd")

            scratch = brain / "scratch"
            scratch.mkdir()
            (scratch / "drawing.png").write_bytes(self.IMAGE_PNG)

            # Hidden dirs that must be ignored:
            sys_media = brain / ".system_generated/logs/media"
            sys_media.mkdir(parents=True)
            (sys_media / "step_1_media_0.png").write_bytes(self.IMAGE_PNG)

            user_up = brain / ".user_uploaded"
            user_up.mkdir()
            (user_up / "upload.png").write_bytes(self.IMAGE_PNG)

            chat = SimpleNamespace(
                id="chat-agy-img",
                dir=home / "chat",
                attachments_dir=home / "chat/attachments",
                meta={"session_id": session, "generated_images": []},
                turn_state={},
                emit=AsyncMock(),
                save_meta=Mock(),
            )

            with patch("pathlib.Path.home", return_value=home):
                await self.provider.recover_outputs(chat)
                # Second run shouldn't duplicate
                await self.provider.recover_outputs(chat)

            # Exactly pic.png (sha matches drawing.png since identical content)
            self.assertEqual(len(chat.meta["generated_images"]), 1)
            self.assertEqual(sum(c.args[0]["type"] == "image" for c in chat.emit.call_args_list), 1)
            name = chat.meta["generated_images"][0]
            self.assertTrue((chat.attachments_dir / name).is_file())

    async def test_recover_subagent_brain_images(self):
        import tempfile
        parent_session = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        sub_session = "12345678-1234-5678-1234-567812345678"
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            parent_brain = home / ".gemini/antigravity-cli/brain" / parent_session
            sub_brain = home / ".gemini/antigravity-cli/brain" / sub_session
            parent_brain.mkdir(parents=True)
            sub_brain.mkdir(parents=True)

            # Log subagent spawn in transcript
            t_file = parent_brain / ".system_generated/logs/transcript.jsonl"
            t_file.parent.mkdir(parents=True)
            t_file.write_text(f'{{"content": "Created subagent: \\"conversationId\\": \\"{sub_session}\\""}}\n')

            # Image created inside subagent brain
            (sub_brain / "sub_pic.png").write_bytes(self.IMAGE_PNG)

            chat = SimpleNamespace(
                id="chat-sub",
                dir=home / "chat",
                attachments_dir=home / "chat/attachments",
                meta={"session_id": parent_session, "generated_images": []},
                turn_state={},
                emit=AsyncMock(),
                save_meta=Mock(),
            )

            with patch("pathlib.Path.home", return_value=home):
                await self.provider.recover_outputs(chat)

            self.assertEqual(len(chat.meta["generated_images"]), 1)
            self.assertEqual(sum(c.args[0]["type"] == "image" for c in chat.emit.call_args_list), 1)

    async def test_on_event_tool_done_scans_and_publishes_image(self):
        import tempfile
        session = "99999999-8888-7777-6666-555555555555"
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            brain = home / ".gemini/antigravity-cli/brain" / session
            brain.mkdir(parents=True)

            chat = SimpleNamespace(
                id="chat-live",
                dir=home / "chat",
                attachments_dir=home / "chat/attachments",
                meta={"session_id": session, "generated_images": []},
                turn_state={},
                emit=AsyncMock(),
                save_meta=Mock(),
            )

            # Image is written during tool execution
            (brain / "generated.png").write_bytes(self.IMAGE_PNG)

            proc = SimpleNamespace()
            event = {
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "step_index": 5,
                    "state": "DONE",
                    "tool_name": "invoke_subagent",
                    "tool_info": {"name": "invoke_subagent"},
                },
            }

            with patch("pathlib.Path.home", return_value=home):
                await self.provider.on_event(chat, proc, event, Mock())

            self.assertEqual(len(chat.meta["generated_images"]), 1)
            self.assertTrue(any(c.args[0]["type"] == "image" for c in chat.emit.call_args_list))

    def test_prompt_for_injects_media_context(self):
        spec = providers.TurnSpec("system_prompt", isolated=True)
        # Resumed turn
        chat = SimpleNamespace(
            id="chat12345678",
            meta={"provider": "agy", "session_started": True, "session_id": "4a5ae7bf-b508-457c-9097-6417070f64fd"},
        )
        p1 = self.provider.prompt_for(chat, spec, "hello")
        self.assertIn("chat12345678", p1)
        self.assertIn("show_in_chat", p1)
        self.assertIn("4a5ae7bf-b508-457c-9097-6417070f64fd", p1)

        # Initial turn
        chat.meta["session_started"] = False
        p2 = self.provider.prompt_for(chat, spec, "hello")
        self.assertIn("system_prompt", p2)
        self.assertIn("chat12345678", p2)
        self.assertIn("show_in_chat", p2)

    async def test_get_chat_resolves_session_uuid(self):
        import runner
        from starlette.requests import Request
        session = "4a5ae7bf-b508-457c-9097-6417070f64fd"
        chat = SimpleNamespace(id="bda98e35c758", meta={"provider": "agy", "session_id": session})
        with patch.dict(runner.chats, {chat.id: chat}):
            # Resolve by 12-char chat_id
            req1 = Request({"type": "http", "path_params": {"chat_id": chat.id}})
            self.assertEqual(runner.get_chat(req1), chat)

            # Resolve by 36-char session UUID
            req2 = Request({"type": "http", "path_params": {"chat_id": session}})
            self.assertEqual(runner.get_chat(req2), chat)

            # Case insensitivity for UUID
            req2_upper = Request({"type": "http", "path_params": {"chat_id": session.upper()}})
            self.assertEqual(runner.get_chat(req2_upper), chat)

            # Unknown UUID returns None
            req3 = Request({"type": "http", "path_params": {"chat_id": "00000000-0000-0000-0000-000000000000"}})
            self.assertIsNone(runner.get_chat(req3))

            # Malformed string returns None
            req4 = Request({"type": "http", "path_params": {"chat_id": "random_junk"}})
            self.assertIsNone(runner.get_chat(req4))

    async def test_attach_media_with_session_uuid(self):
        import runner
        from starlette.requests import Request
        session = "4a5ae7bf-b508-457c-9097-6417070f64fd"
        chat = SimpleNamespace(
            id="bda98e35c758",
            meta={"provider": "agy", "session_id": session},
            emit=AsyncMock(),
            save_meta=Mock(),
        )
        with patch.dict(runner.chats, {chat.id: chat}):
            req = Request({
                "type": "http",
                "method": "POST",
                "path_params": {"chat_id": session},
            })
            async def mock_json():
                return {
                    "asset_id": "e2db9acf-6cdf-484b-9bd8-fc609bef3422",
                    "mime_type": "image/png",
                    "name": "dress.png",
                    "caption": "Test",
                }
            req.json = mock_json
            resp = await runner.attach_media(req)
            self.assertEqual(resp.status_code, 200)
            chat.emit.assert_called_once_with({
                "type": "media",
                "asset_id": "e2db9acf-6cdf-484b-9bd8-fc609bef3422",
                "mime_type": "image/png",
                "name": "dress.png",
                "caption": "Test",
            })



