"""runner.gate_change / release_lease / wake_waiters with fake chats."""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner


def fake(cid, status="running", kind="dev", provider="claude"):
    return SimpleNamespace(id=cid, meta={"kind": kind, "status": status, "title": f"chat {cid}", "provider": provider},
                           proc=None, emit=AsyncMock(), save_meta=MagicMock(), turn_state={})


class Gate(unittest.TestCase):
    def setUp(self):
        runner.chats.clear()
        runner.lease = runner.edit_lock.EditLease()
        self.a, self.b = fake("A"), fake("B")
        runner.chats.update(A=self.a, B=self.b)

    def test_second_chat_refused_until_first_turn_ends(self):
        self.assertIsNone(runner.gate_change(self.a, "Edit", {}))
        reason = runner.gate_change(self.b, "Edit", {})
        self.assertIn("chat A", reason)
        self.assertIsNone(runner.gate_change(self.b, "Read", {}))             # reading is open
        self.assertIsNone(runner.gate_change(self.b, "Bash", {"command": "git commit -m x"}))  # committing too
        self.assertIsNotNone(runner.gate_change(self.b, "Bash", {"command": "rm x"}))
        self.a.meta["status"] = "idle"                                          # A's turn is over
        self.assertIsNone(runner.gate_change(self.b, "Edit", {}))

    def test_project_chats_are_not_gated(self):
        p = fake("P", kind="project")
        self.assertIsNone(runner.gate_change(self.a, "Edit", {}))
        self.assertIsNone(runner.gate_change(p, "Edit", {}))

    def test_release_wakes_waiters_with_a_turn(self):
        async def go():
            runner.gate_change(self.a, "Edit", {})
            runner.gate_change(self.b, "Edit", {})        # B waits
            self.b.meta["status"] = "idle"
            started = []
            runner.drain_queue = AsyncMock(side_effect=lambda c: started.append(c.id))
            runner.release_lease(self.a)
            await asyncio.sleep(0.05)
            return started, self.b.meta["queue"][0]["text"]
        started, text = asyncio.run(go())
        self.assertEqual(started, ["B"])
        self.assertIn("working copy is free", text)

    def test_stopped_waiter_is_not_woken(self):
        async def go():
            runner.gate_change(self.a, "Edit", {})
            runner.gate_change(self.b, "Edit", {})
            self.b.meta["status"] = "stopped"
            runner.drain_queue = AsyncMock()
            runner.release_lease(self.a)
            await asyncio.sleep(0.05)
            return runner.drain_queue.await_count
        self.assertEqual(asyncio.run(go()), 0)


if __name__ == "__main__":
    unittest.main()
