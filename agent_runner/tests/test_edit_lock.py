"""edit_lock: what counts as a change, and who holds the lease."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import edit_lock as el


class ChangeClassification(unittest.TestCase):
    def test_write_tools(self):
        for t in ("Edit", "Write", "NotebookEdit", "write_to_file", "replace_file_content"):
            self.assertTrue(el.is_change(t, {}), t)

    def test_reads_are_not_changes(self):
        for t in ("Read", "Grep", "Glob", "view_file", "mcp__orchestrator__list_tracks"):
            self.assertFalse(el.is_change(t, {}), t)

    def test_safe_shell(self):
        for c in ("ls -la", "git status", "git diff HEAD~1 | head -50", "grep -rn foo backend/ | wc -l",
                  "cd /tmp && git add -A && git commit -m x", "git -C /x log --oneline", "curl -s localhost:8000/api/health",
                  "cat a.py 2>&1", "find . -name '*.py' | sort", "sed -n 1,5p f", "FOO=1 ls"):
            self.assertFalse(el.is_change("Bash", {"command": c}), c)

    def test_changing_shell(self):
        for c in ("rm -rf x", "echo hi > f", "cat a >> b", "sed -i s/a/b/ f", "git checkout main", "git reset --hard",
                  "git stash", "npm run build", "deploy/deploy.sh", "python3 x.py", "find . -delete", "curl -o f http://x",
                  "ls $(rm x)", "echo `touch y`", "tee f", "mv a b", "cp a b", "xargs rm", "ls; rm x", "ls && python3 y.py"):
            self.assertTrue(el.is_change("Bash", {"command": c}), c)

    def test_unparseable_or_missing_command_is_a_change(self):
        self.assertTrue(el.is_change("Bash", {"command": "echo 'unterminated"}))
        self.assertTrue(el.is_change("Bash", {}))

    def test_agy_command_key(self):
        self.assertFalse(el.is_change("run_command", {"CommandLine": "git status"}))
        self.assertTrue(el.is_change("run_command", {"CommandLine": "rm x"}))

    def test_codex_bash_wrapper_is_unwrapped(self):
        self.assertFalse(el.is_change("shell", {"command": ["bash", "-lc", "git status"]}))
        self.assertTrue(el.is_change("shell", {"command": ["bash", "-lc", "rm -rf x"]}))
        self.assertFalse(el.is_change("Bash", {"command": "bash -c 'git diff | head'"}))
        self.assertTrue(el.is_change("Bash", {"command": "bash -c 'rm x'"}))
        self.assertTrue(el.is_change("apply_patch", {}))

    def test_harmless_redirects_allowed(self):
        self.assertFalse(el.is_change("Bash", {"command": "ls nope 2>/dev/null"}))
        self.assertFalse(el.is_change("Bash", {"command": "git status > /dev/null 2>&1"}))


class Lease(unittest.TestCase):
    def test_first_taker_wins_and_others_wait(self):
        lease, alive = el.EditLease(), (lambda cid: True)
        self.assertIsNone(lease.try_take("A", alive))
        self.assertIsNone(lease.try_take("A", alive))  # reentrant
        self.assertEqual(lease.try_take("B", alive), "A")
        lease.wait("B"); lease.wait("B")
        self.assertEqual(lease.release("A"), ["B"])
        self.assertIsNone(lease.try_take("B", alive))

    def test_holder_whose_turn_is_over_does_not_block(self):
        lease = el.EditLease()
        lease.try_take("A", lambda c: True)
        self.assertIsNone(lease.try_take("B", lambda c: False))
        self.assertEqual(lease.holder, "B")

    def test_release_by_non_holder_is_a_noop(self):
        lease = el.EditLease()
        lease.try_take("A", lambda c: True)
        lease.wait("B")
        self.assertEqual(lease.release("B"), [])
        self.assertEqual(lease.holder, "A")


if __name__ == "__main__":
    unittest.main()
