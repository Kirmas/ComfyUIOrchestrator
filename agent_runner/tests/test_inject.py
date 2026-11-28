"""inject(): a message handed to the turn in flight (Claude stdin line, Codex turn/steer)."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers


def run(coro):
    return asyncio.run(coro)


def fake_proc(closing=False):
    stdin = SimpleNamespace(written=[], is_closing=lambda: closing, drain=AsyncMock(),
                            write=lambda b: stdin.written.append(json.loads(b)))
    return SimpleNamespace(stdin=stdin)


def fake_chat(**state):
    return SimpleNamespace(id="c", meta={"session_id": "T"}, turn_state=dict(state), emit=AsyncMock(), save_meta=MagicMock())


class InjectTests(unittest.TestCase):
    def test_base_provider_cannot(self):
        self.assertFalse(run(providers.AgyProvider().inject(fake_chat(), fake_proc(), "hi")))

    def test_claude_writes_user_line(self):
        proc = fake_proc()
        self.assertTrue(run(providers.ClaudeProvider().inject(fake_chat(), proc, "hi")))
        self.assertEqual(proc.stdin.written[0]["message"]["content"][0]["text"], "hi")

    def test_claude_refuses_after_result_or_closed_stdin(self):
        p = providers.ClaudeProvider()
        self.assertFalse(run(p.inject(fake_chat(result_received=True), fake_proc(), "hi")))
        self.assertFalse(run(p.inject(fake_chat(), fake_proc(closing=True), "hi")))

    def test_codex_steers_active_turn(self):
        p, proc, chat = providers.CodexProvider(), fake_proc(), fake_chat()
        run(p.on_event(chat, proc, {"method": "turn/started", "params": {"threadId": "T", "turn": {"id": "turn-9"}}}, None))
        self.assertTrue(run(p.inject(chat, proc, "hi")))
        sent = proc.stdin.written[0]
        self.assertEqual(sent["method"], "turn/steer")
        self.assertEqual(sent["params"]["expectedTurnId"], "turn-9")

    def test_codex_without_turn_id_refuses(self):
        self.assertFalse(run(providers.CodexProvider().inject(fake_chat(), fake_proc(), "hi")))

    def test_codex_refused_steer_goes_to_queue_not_failure(self):
        p, proc, chat = providers.CodexProvider(), fake_proc(), fake_chat(turn_id="turn-9")
        run(p.inject(chat, proc, "hi"))
        rpc_id = proc.stdin.written[0]["id"]
        run(p.on_event(chat, proc, {"id": rpc_id, "error": {"message": "turn mismatch"}}, None))
        self.assertFalse(chat.turn_state.get("failed"))
        self.assertEqual(chat.meta["queue"][0]["text"], "hi")


if __name__ == "__main__":
    unittest.main()
