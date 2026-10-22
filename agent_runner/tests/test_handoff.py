"""Tests for agent handoff (Stage 1).

Covers:
- chain ban by identity across providers
- deny rule
- re-ask blocked after a no
- timeout = soft no without config change
- frozen chat 409
- field caps
- first-message assembly (verbatim user messages, last N)
"""
import asyncio
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import handoff
import providers
import runner
from handoff import HandoffConfig, get_handoff_targets, limit_state, target_label, identity_label, build_first_message


def run(coro):
    return asyncio.run(coro)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.config_path = Path(self.tmp_dir) / "handoff.json"
        self.config = HandoffConfig(self.config_path)

        # Mock providers to be available and support project
        self.prov_claude = SimpleNamespace(
            id="claude",
            label="Claude Code",
            available=True,
            supports_project=True,
            models=[{"id": "opus", "label": "Opus 4.6"}, {"id": "sonnet", "label": "Sonnet 4.5"}],
            model_ids={"opus", "sonnet"},
            efforts_for=lambda m: ["low", "high"],
        )
        self.prov_agy = SimpleNamespace(
            id="agy",
            label="Antigravity",
            available=True,
            supports_project=True,
            models=[{"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash"}, {"id": "claude-opus-5-5", "label": "Claude Opus 5.5"}],
            model_ids={"gemini-3.8-flash", "claude-opus-5-5"},
            efforts_for=lambda m: ["low", "medium", "high"],
        )
        self.prov_codex = SimpleNamespace(
            id="codex",
            label="Codex",
            available=True,
            supports_project=True,
            models=[{"id": "gpt-6-astra", "label": "Astra"}],
            model_ids={"gpt-6-astra"},
            efforts_for=lambda m: ["low", "high"],
        )
        self.mock_providers = {
            "claude": self.prov_claude,
            "agy": self.prov_agy,
            "codex": self.prov_codex,
        }

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_chain_ban_by_identity_across_providers(self):
        """Denying/banning an identity excludes all targets mapping to that identity, across providers."""
        self.config.identities = {
            "claude:opus": "claude-opus",
            "agy:claude-opus-5-5": "claude-opus",
        }

        # Chat 2 is on agy:gemini-3.8-flash, but handed off from a chat with identity claude-opus
        chat2 = SimpleNamespace(
            id="chat-2",
            meta={
                "kind": "project",
                "provider": "agy",
                "requested_model": "gemini-3.8-flash",
                "handoff_from": {
                    "chat_id": "chat-1",
                    "target": "agy:gemini-3.8-flash",
                    "reason": "drawing",
                    "chain": ["claude-opus", "gemini-3.8-flash"],
                },
            },
        )

        with patch.dict(providers.PROVIDERS, self.mock_providers, clear=True), \
             patch.dict(handoff.PROVIDERS, self.mock_providers, clear=True):
            targets = get_handoff_targets(chat2, self.config)
            target_ids = {t["target"] for t in targets}

            # agy:claude-opus-5-5 must be banned because identity 'claude-opus' is in the chain
            self.assertNotIn("agy:claude-opus-5-5", target_ids)
            # claude:opus must also be banned (same identity)
            self.assertNotIn("claude:opus", target_ids)
            # codex:gpt-6-astra and claude:sonnet are not in the chain, so they should be present
            self.assertIn("codex:gpt-6-astra", target_ids)
            self.assertIn("claude:sonnet", target_ids)

    def test_deny_rule(self):
        """A persistent rule '<from> -> <to>': 'deny' excludes the target."""
        self.config.rules = {
            "agy:gemini-3.8-flash -> codex:gpt-6-astra": "deny",
        }

        chat = SimpleNamespace(
            id="chat-agy",
            meta={
                "kind": "project",
                "provider": "agy",
                "requested_model": "gemini-3.8-flash",
            },
        )

        with patch.dict(providers.PROVIDERS, self.mock_providers, clear=True), \
             patch.dict(handoff.PROVIDERS, self.mock_providers, clear=True):
            targets = get_handoff_targets(chat, self.config)
            target_ids = {t["target"] for t in targets}
            self.assertNotIn("codex:gpt-6-astra", target_ids)

    def test_re_ask_blocked_after_a_no(self):
        """A target already refused (meta['handoff_refused']) is excluded from handoff targets."""
        chat = SimpleNamespace(
            id="chat-refused",
            meta={
                "kind": "project",
                "provider": "agy",
                "requested_model": "gemini-3.8-flash",
                "handoff_refused": ["codex:gpt-6-astra"],
            },
        )

        with patch.dict(providers.PROVIDERS, self.mock_providers, clear=True), \
             patch.dict(handoff.PROVIDERS, self.mock_providers, clear=True):
            targets = get_handoff_targets(chat, self.config)
            target_ids = {t["target"] for t in targets}
            self.assertNotIn("codex:gpt-6-astra", target_ids)

    def test_timeout_is_soft_no_without_config_change(self):
        """Timeout marks the request declined with timeout=True, adds target to refused, leaves config untouched."""
        self.config.rules = {}
        self.config.save()

        chat = SimpleNamespace(
            id="chat-timeout",
            dir=Path(self.tmp_dir) / "chat-timeout",
            meta={
                "kind": "project",
                "provider": "claude",
                "requested_model": "sonnet",
                "pending_handoff": {
                    "request_id": "req-123",
                    "target": "codex:gpt-6-astra",
                    "target_label": "Codex (Astra)",
                    "task": "Do task",
                    "reason": "Test",
                    "done_so_far": "",
                    "focus": [],
                    "expires_at": 1000.0,
                },
                "handoff_refused": [],
            },
            proc=None,
            emit=AsyncMock(),
            set_status=AsyncMock(),
            save_meta=MagicMock(),
        )

        with patch.dict(runner.chats, {chat.id: chat}), \
             patch("runner.run_turn", AsyncMock()), \
             patch.object(runner, "handoff_config", self.config):
            run(runner.handle_handoff_decline(chat.id, "req-123", is_timeout=True))

            # Target added to refused list
            self.assertIn("codex:gpt-6-astra", chat.meta["handoff_refused"])
            # Pending handoff cleared
            self.assertNotIn("pending_handoff", chat.meta)
            # Config rules untouched
            self.assertEqual(self.config.rules, {})

            # Event emitted with timeout=True
            emitted_types = [call.args[0]["type"] for call in chat.emit.call_args_list]
            self.assertIn("handoff_declined", emitted_types)
            declined_ev = next(call.args[0] for call in chat.emit.call_args_list if call.args[0]["type"] == "handoff_declined")
            self.assertTrue(declined_ev["timeout"])

            # System message emitted
            user_ev = next(call.args[0] for call in chat.emit.call_args_list if call.args[0]["type"] == "user")
            self.assertEqual(user_ev["origin"], "system")
            self.assertIn("(no answer in time)", user_ev["text"])

    def test_frozen_chat_409(self):
        """A handed off chat is frozen: post_message returns 409, handoff targets return empty."""
        chat = SimpleNamespace(
            id="chat-frozen",
            meta={
                "kind": "project",
                "provider": "claude",
                "requested_model": "sonnet",
                "handed_off_to": {
                    "chat_id": "chat-new",
                    "target": "codex:gpt-6-astra",
                    "reason": "hand off",
                    "at": 12345.0,
                },
            },
        )

        # Handoff targets on frozen chat is empty
        targets = get_handoff_targets(chat, self.config)
        self.assertEqual(targets, [])

        # Calling post_message on frozen chat returns 409
        req = SimpleNamespace(
            path_params={"chat_id": chat.id},
            json=AsyncMock(return_value={"text": "hello"}),
        )
        with patch.dict(runner.chats, {chat.id: chat}), \
             patch.dict(handoff.PROVIDERS, self.mock_providers, clear=True):
            resp = run(runner.post_message(req))
            self.assertEqual(resp.status_code, 409)
            body = json.loads(resp.body)
            self.assertIn("This chat was handed off to", body["detail"])

    def test_field_caps(self):
        """Field length caps: task <= 4000, reason <= 300, done_so_far <= 2000, focus <= 20 ids."""
        chat = SimpleNamespace(
            id="chat-caps",
            meta={
                "kind": "project",
                "provider": "claude",
                "requested_model": "sonnet",
            },
        )

        with patch.dict(runner.chats, {chat.id: chat}), \
             patch.dict(providers.PROVIDERS, self.mock_providers, clear=True), \
             patch.dict(handoff.PROVIDERS, self.mock_providers, clear=True), \
             patch.object(runner, "handoff_config", self.config):

            # 1. task too long
            req = SimpleNamespace(
                path_params={"chat_id": chat.id},
                json=AsyncMock(return_value={
                    "target": "codex:gpt-6-astra",
                    "task": "x" * 4001,
                    "reason": "valid reason",
                }),
            )
            resp = run(runner.request_handoff_route(req))
            self.assertEqual(resp.status_code, 400)
            self.assertIn("task exceeds 4000", json.loads(resp.body)["detail"])

            # 2. reason too long
            req = SimpleNamespace(
                path_params={"chat_id": chat.id},
                json=AsyncMock(return_value={
                    "target": "codex:gpt-6-astra",
                    "task": "valid task",
                    "reason": "y" * 301,
                }),
            )
            resp = run(runner.request_handoff_route(req))
            self.assertEqual(resp.status_code, 400)
            self.assertIn("reason exceeds 300", json.loads(resp.body)["detail"])

            # 3. done_so_far too long
            req = SimpleNamespace(
                path_params={"chat_id": chat.id},
                json=AsyncMock(return_value={
                    "target": "codex:gpt-6-astra",
                    "task": "valid task",
                    "reason": "valid reason",
                    "done_so_far": "z" * 2001,
                }),
            )
            resp = run(runner.request_handoff_route(req))
            self.assertEqual(resp.status_code, 400)
            self.assertIn("done_so_far exceeds 2000", json.loads(resp.body)["detail"])

            # 4. focus too long
            req = SimpleNamespace(
                path_params={"chat_id": chat.id},
                json=AsyncMock(return_value={
                    "target": "codex:gpt-6-astra",
                    "task": "valid task",
                    "reason": "valid reason",
                    "focus": ["id"] * 21,
                }),
            )
            resp = run(runner.request_handoff_route(req))
            self.assertEqual(resp.status_code, 400)
            self.assertIn("focus exceeds 20 ids", json.loads(resp.body)["detail"])

    def test_first_message_assembly_verbatim_user_messages_last_n(self):
        """Assembles first message template: verbatim user messages (excluding handoff), last N, oldest first."""
        chat_dir = Path(self.tmp_dir) / "source-chat"
        chat_dir.mkdir(parents=True, exist_ok=True)
        att_dir = chat_dir / "attachments"
        att_dir.mkdir(parents=True, exist_ok=True)
        (att_dir / "test_image.png").write_bytes(b"png")

        # 25 user messages + 1 handoff message
        events = []
        for i in range(25):
            events.append({"type": "user", "text": f"User message #{i}", "seq": i})
        events.append({"type": "user", "text": "Handoff message", "origin": "handoff", "seq": 26})

        chat = SimpleNamespace(
            id="source-chat-id",
            dir=chat_dir,
            attachments_dir=att_dir,
            meta={
                "project_id": "proj-123",
                "project_name": "My Project",
                "provider": "claude",
                "requested_model": "opus",
                "handoff_from": {
                    "chain": ["claude-opus"],
                },
            },
            events_after=lambda after: events,
        )

        self.config.user_messages_max = 5
        self.config.identities = {"claude:opus": "claude-opus", "codex:gpt-6-astra": "codex-astra"}

        with patch.dict(handoff.PROVIDERS, self.mock_providers, clear=True), \
             patch("handoff.validate_focus_ids", return_value="- node-1\n- node-2"):
            msg = run(build_first_message(
                source_chat=chat,
                target="codex:gpt-6-astra",
                task="Draw a cool background",
                reason="Codex draws better",
                done_so_far="Set up initial layout",
                focus=["node-1", "node-2"],
                config=self.config,
            ))

            # Header
            self.assertIn("[Handoff] This task was handed to you by Claude Code (Opus 4.6).", msg)
            self.assertIn("Chain: Claude Opus → you.", msg)
            self.assertIn("You may not hand it back to: Claude Opus.", msg)
            self.assertIn("Project: My Project (proj-123). Previous chat: source-chat-id.", msg)

            # Sections
            self.assertIn("## Task\nDraw a cool background", msg)
            self.assertIn("## Why you\nCodex draws better", msg)
            self.assertIn("## Already done\nSet up initial layout", msg)
            self.assertIn("## Where to work\n- node-1\n- node-2", msg)

            # Verbatim user messages: last 5 of the 25 user messages (i.e. #20 to #24), oldest first
            self.assertNotIn("User message #19", msg)
            self.assertIn("User message #20", msg)
            self.assertIn("User message #24", msg)
            # Origin=handoff must be excluded
            self.assertNotIn("Handoff message", msg)

            # Attachments
            self.assertIn('- test_image.png (chat_id="source-chat-id")', msg)
            self.assertIn("If you are not sure what the person wants, ask one short question instead of guessing.", msg)

    def test_handoff_routes_and_rules(self):
        """Test HTTP routes for handoff, decisions, and rules settings."""
        chat = SimpleNamespace(
            id="chat-http",
            dir=Path(self.tmp_dir) / "chat-http",
            meta={
                "kind": "project",
                "provider": "claude",
                "requested_model": "opus",
                "project_id": "p1",
                "project_name": "Project 1",
            },
            emit=AsyncMock(),
            set_status=AsyncMock(),
            save_meta=MagicMock(),
            proc=None,
        )

        with patch.dict(runner.chats, {chat.id: chat}), \
             patch.dict(providers.PROVIDERS, self.mock_providers, clear=True), \
             patch.dict(handoff.PROVIDERS, self.mock_providers, clear=True), \
             patch.object(runner, "handoff_config", self.config):

            # 1. GET handoff targets
            req = SimpleNamespace(path_params={"chat_id": chat.id})
            resp = run(runner.list_handoff_targets_route(req))
            self.assertEqual(resp.status_code, 200)
            targets = json.loads(resp.body)
            self.assertTrue(len(targets) > 0)
            target_ids = {t["target"] for t in targets}
            self.assertNotIn("claude:opus", target_ids)  # Own target excluded

            # 2. POST handoff (mode == 'ask' since no rule yet)
            req = SimpleNamespace(
                path_params={"chat_id": chat.id},
                json=AsyncMock(return_value={
                    "target": "codex:gpt-6-astra",
                    "task": "Test task",
                    "reason": "Test reason",
                }),
            )
            with patch("runner.stop_chat", AsyncMock()):
                resp = run(runner.request_handoff_route(req))
                self.assertEqual(resp.status_code, 200)
                body = json.loads(resp.body)
                self.assertEqual(body["status"], "awaiting_person")
                self.assertIn("pending_handoff", chat.meta)
                req_id = chat.meta["pending_handoff"]["request_id"]

            # 3. Decision allow_always writes rule and creates new chat
            decide_req = SimpleNamespace(
                path_params={"chat_id": chat.id, "request_id": req_id},
                json=AsyncMock(return_value={"decision": "allow_always"}),
            )
            with patch("runner.perform_handoff", AsyncMock(return_value=SimpleNamespace(id="new-chat-id"))):
                resp = run(runner.decide_handoff_route(decide_req))
                self.assertEqual(resp.status_code, 200)
                body = json.loads(resp.body)
                self.assertEqual(body["status"], "started")
                self.assertEqual(body["chat_id"], "new-chat-id")
                # Rule written to config
                self.assertEqual(self.config.rule_for("claude:opus", "codex:gpt-6-astra"), "allow")

            # 4. GET & PUT handoff rules routes
            resp = run(runner.get_handoff_rules_route(SimpleNamespace()))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(json.loads(resp.body)["rules"]["claude:opus -> codex:gpt-6-astra"], "allow")

            put_req = SimpleNamespace(json=AsyncMock(return_value={"rules": {}}))
            resp = run(runner.update_handoff_rules_route(put_req))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(json.loads(resp.body)["rules"], {})

    def test_carry_over_inherits_down_the_chain(self):
        """After A -> B -> C, C still gets what the person wrote to A; machinery text is not the person's."""
        att_dir = Path(self.tmp_dir) / "b-att"
        events = [
            {"type": "user", "text": "handoff text", "origin": "handoff", "seq": 1},
            {"type": "user", "text": "[System] not approved", "origin": "system", "seq": 2},
            {"type": "user", "text": "typed in B", "seq": 3},
        ]
        chat = SimpleNamespace(
            id="chat-b",
            attachments_dir=att_dir,
            meta={"handoff_from": {"person_messages": ["typed in A"],
                                   "attachments": [{"chat_id": "chat-a", "name": "ref.png"}]}},
            events_after=lambda after: events,
        )
        carried = handoff.carry_over(chat, self.config)
        self.assertEqual(carried["person_messages"], ["typed in A", "typed in B"])
        self.assertEqual(carried["attachments"], [{"chat_id": "chat-a", "name": "ref.png"}])

    def test_double_decision_creates_one_chat(self):
        """A second click on the same request (phone double tap) is refused, not a second handoff."""
        chat = SimpleNamespace(
            id="chat-double",
            meta={"kind": "project", "provider": "claude", "requested_model": "opus",
                  "pending_handoff": {"request_id": "r1", "target": "codex:gpt-6-astra",
                                      "task": "t", "reason": "r"}},
            emit=AsyncMock(), save_meta=MagicMock(), proc=None,
        )
        perform = AsyncMock(return_value=SimpleNamespace(id="new"))

        def req():
            return SimpleNamespace(path_params={"chat_id": chat.id, "request_id": "r1"},
                                   json=AsyncMock(return_value={"decision": "allow_once"}))

        async def both():
            return await asyncio.gather(runner.decide_handoff_route(req()), runner.decide_handoff_route(req()))

        with patch.dict(runner.chats, {chat.id: chat}), patch("runner.perform_handoff", perform):
            r1, r2 = run(both())
        self.assertEqual(sorted([r1.status_code, r2.status_code]), [200, 409])
        self.assertEqual(perform.await_count, 1)

    def test_repeated_identical_request_is_not_an_error(self):
        """An MCP client re-sending the same hand_off gets the same answer, and no second request."""
        chat = SimpleNamespace(
            id="chat-repeat",
            meta={"kind": "project", "provider": "claude", "requested_model": "opus",
                  "pending_handoff": {"request_id": "r1", "target": "codex:gpt-6-astra",
                                      "task": "Test task", "reason": "r"}},
            emit=AsyncMock(), save_meta=MagicMock(), proc=None,
        )
        req = SimpleNamespace(path_params={"chat_id": chat.id},
                              json=AsyncMock(return_value={"target": "codex:gpt-6-astra", "task": "Test task", "reason": "r"}))
        with patch.dict(runner.chats, {chat.id: chat}):
            resp = run(runner.request_handoff_route(req))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.body)["status"], "awaiting_person")
        self.assertEqual(chat.meta["pending_handoff"]["request_id"], "r1")
        chat.emit.assert_not_awaited()

    def test_failed_handoff_does_not_leave_chat_pending(self):
        chat = SimpleNamespace(
            id="chat-fail",
            meta={"kind": "project", "provider": "claude", "requested_model": "opus",
                  "pending_handoff": {"request_id": "r1", "target": "codex:gpt-6-astra",
                                      "task": "t", "reason": "r"}},
            emit=AsyncMock(), save_meta=MagicMock(), proc=None,
        )
        req = SimpleNamespace(path_params={"chat_id": chat.id, "request_id": "r1"},
                              json=AsyncMock(return_value={"decision": "allow_once"}))
        with patch.dict(runner.chats, {chat.id: chat}), \
             patch("runner.perform_handoff", AsyncMock(side_effect=RuntimeError("boom"))):
            resp = run(runner.decide_handoff_route(req))
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("pending_handoff", chat.meta)
        self.assertIn("error", [c.args[0]["type"] for c in chat.emit.call_args_list])

    # --- Stage 2 tests: pure limit_state & route limit checks ---

    def test_pure_limits_exhausted_and_disabled(self):
        """Exhausted window -> hidden; disabled exhausted window -> ignored."""
        limits_data = {
            "claude": {
                "windows": [
                    {"id": "five_hour", "utilization": 1.0, "resets_at": 20000.0}
                ]
            }
        }
        state, detail = limit_state("claude:sonnet", limits_data, self.config, now=10000.0)
        self.assertEqual(state, "hidden")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["window_id"], "five_hour")
        self.assertEqual(detail["used"], 1.0)

        # Disabled window
        limits_data_disabled = {
            "claude": {
                "windows": [
                    {"id": "five_hour", "utilization": 1.0, "resets_at": 20000.0, "disabled": True}
                ]
            }
        }
        state, detail = limit_state("claude:sonnet", limits_data_disabled, self.config, now=10000.0)
        self.assertEqual(state, "ok")
        self.assertIsNone(detail)

    def test_pure_limits_person_example_moderate_and_on_pace(self):
        """Person's example (80% used, 3 h of 5 h left) -> hidden; moderate -> scarce; on-pace -> ok."""
        now = 10000.0
        # 1. Person's example: 5h (18000s), 3h left (10800s), 80% used (0.8)
        # elapsed = 1 - 10800/18000 = 0.4. ahead = 0.8 - 0.4 = 0.4 >= 0.35 -> hidden
        limits_hidden = {
            "claude": {
                "windows": [
                    {"id": "five_hour", "utilization": 0.8, "resets_at": now + 10800.0}
                ]
            }
        }
        state, detail = limit_state("claude:sonnet", limits_hidden, self.config, now=now)
        self.assertEqual(state, "hidden")
        self.assertAlmostEqual(detail["ahead"], 0.4)

        # 2. Moderate case: 5h, 3.5h left (12600s), 50% used (0.5)
        # elapsed = 1 - 12600/18000 = 0.3. ahead = 0.5 - 0.3 = 0.2 >= 0.15 and < 0.35 -> scarce
        limits_scarce = {
            "claude": {
                "windows": [
                    {"id": "five_hour", "utilization": 0.5, "resets_at": now + 12600.0}
                ]
            }
        }
        state, detail = limit_state("claude:sonnet", limits_scarce, self.config, now=now)
        self.assertEqual(state, "scarce")
        self.assertAlmostEqual(detail["ahead"], 0.2)

        # 3. On-pace case: 5h, 4h left (14400s), 20% used (0.2)
        # elapsed = 1 - 14400/18000 = 0.2. ahead = 0.2 - 0.2 = 0.0 < 0.15 -> ok
        limits_ok = {
            "claude": {
                "windows": [
                    {"id": "five_hour", "utilization": 0.2, "resets_at": now + 14400.0}
                ]
            }
        }
        state, detail = limit_state("claude:sonnet", limits_ok, self.config, now=now)
        self.assertEqual(state, "ok")
        self.assertIsNone(detail)

    def test_pure_limits_reset_window_in_past(self):
        """Reset window (resets_at in the past) -> ok."""
        now = 10000.0
        limits_reset = {
            "claude": {
                "windows": [
                    {"id": "five_hour", "utilization": 0.99, "resets_at": now - 100.0}
                ]
            }
        }
        state, detail = limit_state("claude:sonnet", limits_reset, self.config, now=now)
        self.assertEqual(state, "ok")
        self.assertIsNone(detail)

    def test_pure_limits_cloud_credits_ignored(self):
        """cloud_credits window is ignored."""
        now = 10000.0
        limits_credits = {
            "claude": {
                "windows": [
                    {"id": "cloud_credits", "label": "Cloud credits: $0 of $100 used", "utilization": 1.0, "resets_at": now + 50000.0}
                ]
            }
        }
        state, detail = limit_state("claude:sonnet", limits_credits, self.config, now=now)
        self.assertEqual(state, "ok")
        self.assertIsNone(detail)

    def test_pure_limits_agy_pools_and_quota_windows(self):
        """agy: Flash unaffected by exhausted 3p group; Sonnet-in-agy hidden by it;
        model-id quota window hides only that model; target missing from pools isn't hidden by any group."""
        now = 10000.0
        # 3p-weekly exhausted
        limits_3p_exhausted = {
            "agy": {
                "windows": [
                    {"id": "3p-weekly", "group": "Claude and GPT models", "utilization": 1.0, "resets_at": now + 50000.0, "disabled": False},
                    {"id": "gemini-5h", "group": "Gemini Models", "utilization": 0.05, "resets_at": now + 15000.0, "disabled": False},
                ]
            }
        }
        # Flash is in Gemini Models -> unaffected
        state_flash, _ = limit_state("agy:gemini-3.8-flash", limits_3p_exhausted, self.config, now=now)
        self.assertEqual(state_flash, "ok")

        # Sonnet in agy is in Claude and GPT models -> hidden
        state_sonnet, detail_sonnet = limit_state("agy:claude-sonnet-5-5", limits_3p_exhausted, self.config, now=now)
        self.assertEqual(state_sonnet, "hidden")
        self.assertEqual(detail_sonnet["window_id"], "3p-weekly")

        # Model-id quota window hides only that model
        limits_model_quota = {
            "agy": {
                "windows": [
                    {"id": "gemini-3.8-flash", "utilization": 1.0, "resets_at": now + 20000.0}
                ]
            }
        }
        state_flash, _ = limit_state("agy:gemini-3.8-flash", limits_model_quota, self.config, now=now)
        self.assertEqual(state_flash, "hidden")
        state_sonnet, _ = limit_state("agy:claude-sonnet-5-5", limits_model_quota, self.config, now=now)
        self.assertEqual(state_sonnet, "ok")

        # Target missing from pools isn't hidden by any group
        limits_all_groups_exhausted = {
            "agy": {
                "windows": [
                    {"id": "3p-weekly", "group": "Claude and GPT models", "utilization": 1.0, "resets_at": now + 50000.0},
                    {"id": "gemini-weekly", "group": "Gemini Models", "utilization": 1.0, "resets_at": now + 50000.0},
                    {"id": "gemini-argon-wtu-weekly", "group": "Gemini 4 Argon", "utilization": 1.0, "resets_at": now + 50000.0},
                ]
            }
        }
        state_unknown, _ = limit_state("agy:unmapped-model", limits_all_groups_exhausted, self.config, now=now)
        self.assertEqual(state_unknown, "ok")

    def test_pure_limits_no_provider_data_and_unknown_window_id(self):
        """No provider data -> ok; unknown window id (no duration) -> only the 100% rule."""
        now = 10000.0
        # No provider data
        state, _ = limit_state("claude:sonnet", {}, self.config, now=now)
        self.assertEqual(state, "ok")
        state, _ = limit_state("claude:sonnet", None, self.config, now=now)
        self.assertEqual(state, "ok")

        # Unknown window id without duration
        limits_unknown = {
            "claude": {
                "windows": [
                    {"id": "custom-unrecognized-window", "utilization": 0.95, "resets_at": now + 1000.0}
                ]
            }
        }
        state, _ = limit_state("claude:sonnet", limits_unknown, self.config, now=now)
        self.assertEqual(state, "ok")

        limits_unknown_exhausted = {
            "claude": {
                "windows": [
                    {"id": "custom-unrecognized-window", "utilization": 1.0, "resets_at": now + 1000.0}
                ]
            }
        }
        state, _ = limit_state("claude:sonnet", limits_unknown_exhausted, self.config, now=now)
        self.assertEqual(state, "hidden")

    def test_route_scarce_target_listed_with_mode_ask_even_with_allow_rule(self):
        """Scarce target is listed with mode: 'ask' even with an allow rule."""
        now = 10000.0
        # Set an allow rule: claude:opus -> agy:gemini-3.8-flash
        self.config.set_rule("claude:opus", "agy:gemini-3.8-flash", "allow")

        # Set limits so agy:gemini-3.8-flash is scarce (5h window, 3.5h left, 50% used)
        limits_data = {
            "agy": {
                "windows": [
                    {"id": "gemini-5h", "group": "Gemini Models", "utilization": 0.5, "resets_at": now + 12600.0, "disabled": False}
                ]
            }
        }

        chat = SimpleNamespace(
            id="chat-allow-scarce",
            meta={
                "kind": "project",
                "provider": "claude",
                "requested_model": "opus",
            },
        )

        with patch.dict(providers.PROVIDERS, self.mock_providers, clear=True), \
             patch.dict(handoff.PROVIDERS, self.mock_providers, clear=True):
            targets = get_handoff_targets(chat, self.config, limits_data=limits_data, now=now)
            flash_info = next((t for t in targets if t["target"] == "agy:gemini-3.8-flash"), None)
            self.assertIsNotNone(flash_info)
            self.assertEqual(flash_info["mode"], "ask")
            self.assertTrue(flash_info.get("scarce"))
            self.assertIn("limits_detail", flash_info)
            self.assertEqual(flash_info.get("note"), "limits are being spent fast; prefer another target")

    def test_route_allow_always_on_scarce_returns_400_and_still_pending(self):
        """allow_always on a scarce request -> 400 and the request is still pending."""
        chat = SimpleNamespace(
            id="chat-scarce-pending",
            meta={
                "kind": "project",
                "provider": "claude",
                "requested_model": "opus",
                "pending_handoff": {
                    "request_id": "r-scarce",
                    "target": "agy:gemini-3.8-flash",
                    "task": "Do work",
                    "reason": "Test",
                    "scarce": True,
                },
            },
            emit=AsyncMock(),
            save_meta=MagicMock(),
            proc=None,
        )
        req = SimpleNamespace(
            path_params={"chat_id": chat.id, "request_id": "r-scarce"},
            json=AsyncMock(return_value={"decision": "allow_always"}),
        )

        with patch.dict(runner.chats, {chat.id: chat}), \
             patch.object(runner, "handoff_config", self.config):
            resp = run(runner.decide_handoff_route(req))
            self.assertEqual(resp.status_code, 400)
            # The request must still be pending!
            self.assertIn("pending_handoff", chat.meta)
            self.assertEqual(chat.meta["pending_handoff"]["request_id"], "r-scarce")

    def test_route_target_becomes_hidden_between_request_and_approval(self):
        """Target becomes hidden between request and approval -> no handoff, error event, nothing pending."""
        chat = SimpleNamespace(
            id="chat-hide-between",
            meta={
                "kind": "project",
                "provider": "claude",
                "requested_model": "opus",
                "pending_handoff": {
                    "request_id": "r-hide",
                    "target": "codex:gpt-6-astra",
                    "target_label": "Codex (Astra)",
                    "task": "Do work",
                    "reason": "Test",
                },
            },
            emit=AsyncMock(),
            save_meta=MagicMock(),
            set_status=AsyncMock(),
            task=None,
            proc=None,
        )
        req = SimpleNamespace(
            path_params={"chat_id": chat.id, "request_id": "r-hide"},
            json=AsyncMock(return_value={"decision": "allow_once"}),
        )

        perform = AsyncMock()
        # Codex limit is exhausted (utilization 1.0, reset in the future)
        exhausted_limits = {
            "codex": {
                "windows": [
                    {"id": "five_hour", "utilization": 1.0, "resets_at": time.time() + 50000.0}
                ]
            }
        }

        with patch.dict(runner.chats, {chat.id: chat}), \
             patch("runner.perform_handoff", perform), \
             patch.object(runner.limits, "data", exhausted_limits), \
             patch("runner.run_turn", AsyncMock()), \
             patch.object(runner, "handoff_config", self.config):
            resp = run(runner.decide_handoff_route(req))
            self.assertEqual(resp.status_code, 409)
            # No handoff performed
            perform.assert_not_awaited()
            # Request is claimed, nothing pending
            self.assertNotIn("pending_handoff", chat.meta)
            # The stopped requester is resumed with why, and won't ask for it again
            emitted = [c.args[0] for c in chat.emit.call_args_list]
            sys_msgs = [e for e in emitted if e.get("type") == "user" and e.get("origin") == "system"]
            self.assertEqual(len(sys_msgs), 1)
            self.assertIn("limit ran out meanwhile", sys_msgs[0]["text"])
            self.assertIn("Continue the task yourself", sys_msgs[0]["text"])
            self.assertIn("handoff_declined", [e.get("type") for e in emitted])


    def test_low_usage_is_always_ok_whatever_the_pace(self):
        """29% used ten minutes into a 5-hour window is far 'ahead', but under the floor: ok."""
        now = 1_000_000.0
        data = {"codex": {"windows": [{"id": "five_hour", "utilization": 0.29, "resets_at": now + 18000 - 600}]}}
        self.assertEqual(handoff.limit_state("codex:gpt-6-luna", data, self.config, now=now)[0], "ok")
        data["codex"]["windows"][0]["utilization"] = 0.31
        self.assertEqual(handoff.limit_state("codex:gpt-6-luna", data, self.config, now=now)[0], "scarce")

    def test_profiles_reach_the_agent_and_persist(self):
        """Profiles are hand-written: they show up in the target list and survive a reload."""
        chat = SimpleNamespace(id="chat-prof", meta={"kind": "project", "provider": "claude", "requested_model": "opus"})
        self.config.set_profile("codex:gpt-6-astra", "bulk work", "tiny", True)
        with patch.dict(handoff.PROVIDERS, self.mock_providers, clear=True):
            items = {t["target"]: t for t in get_handoff_targets(chat, self.config, limits_data={})}
        astra = items["codex:gpt-6-astra"]
        self.assertEqual((astra["good_for"], astra["appetite"], astra["draws_natively"]), ("bulk work", "tiny", True))
        reloaded = HandoffConfig(self.config.path)
        self.assertEqual(reloaded.profiles["codex:gpt-6-astra"]["good_for"], "bulk work")
        # Defaults for the others are still there after a partial override
        self.assertIn("claude:opus", reloaded.profiles)


if __name__ == "__main__":
    unittest.main()
