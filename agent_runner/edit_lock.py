"""One working copy, many dev chats: the first change locks it until that turn ends.

Reading, thinking, answering the person and committing stay open to every chat;
only a call that CHANGES files needs the lease. The lease belongs to one chat from
its first change until its turn is over (the runner releases it), so two agents
never edit the same tree at once and neither sees the other's half-done work as
its own.

`is_change` decides what counts as a change: a file-writing tool, or a shell
command that is not on the read-only / `git add|commit` allow-list. Unknown means
"change" -- refusing a harmless command costs a retry, letting a write through
defeats the lock.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Callable

# Claude's and Antigravity's file-writing tools.
WRITE_TOOLS = {
    "Edit", "Write", "MultiEdit", "NotebookEdit",
    "write_to_file", "replace_file_content", "sed_file", "multi_replace_file_content", "notebook_edit",
    "apply_patch",  # Codex
}
# Codex's names for its shell tool are not pinned down here; any of them is a shell.
SHELL_TOOLS = {"Bash", "run_command", "notebook_execution", "shell", "local_shell", "exec_command", "unified_exec"}
COMMAND_KEYS = ("command", "CommandLine", "commandLine", "cmd")

SAFE_PROGRAMS = {
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "find", "sort", "uniq", "cut", "tr",
    "jq", "diff", "cmp", "sed", "awk", "column", "nl", "strings", "xxd", "hexdump", "md5sum", "sha256sum",
    "pwd", "echo", "printf", "date", "which", "file", "stat", "du", "df", "tree", "readlink", "realpath",
    "basename", "dirname", "true", "false", "test", "[", "sleep", "env", "ps", "pgrep", "ss", "lsof",
    "uname", "whoami", "id", "curl", "cd", "git",
}
# `git add`/`git commit` are allowed on purpose: committing finished work is
# something any chat may do while another one edits.
SAFE_GIT = {"status", "diff", "log", "show", "add", "commit", "rev-parse", "ls-files", "blame", "describe", "shortlog"}

_SEGMENTS = re.compile(r"&&|\|\||;|\||\n")
_HARMLESS_REDIRECTS = re.compile(r"\d?>\s*/dev/null|2>&1|&>\s*/dev/null")


SHELLS = {"bash", "sh", "zsh", "dash"}
_C_FLAGS = {"-c", "-lc", "-cl", "-ic", "-ci"}


def _inner_command(tokens: list[str]) -> str | None:
    """`bash -lc "<cmd>"` (how Codex runs everything): the command that counts is <cmd>."""
    if len(tokens) >= 3 and os.path.basename(tokens[0]) in SHELLS and tokens[1] in _C_FLAGS:
        return tokens[2]
    return None


def _command_of(args: dict) -> str | None:
    for key in COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return _inner_command(value) or shlex.join(value)  # Codex hands the argv as a list
    return None


def _segment_is_safe(tokens: list[str]) -> bool:
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return True
    inner = _inner_command(tokens)
    if inner is not None:
        return shell_is_safe(inner)
    prog = os.path.basename(tokens[0])
    if prog not in SAFE_PROGRAMS:
        return False
    rest = tokens[1:]
    if prog == "find":
        return not any(t in ("-delete", "-exec", "-execdir", "-ok", "-fprint", "-fprintf", "-fls") for t in rest)
    if prog == "sed":
        return not any(t == "-i" or t.startswith("-i") or t == "--in-place" or t.startswith("--in-place") for t in rest)
    if prog == "curl":
        return not any(t in ("-o", "-O", "--output", "--remote-name") or t.startswith("--output=") for t in rest)
    if prog == "git":
        sub = [t for t in rest if not t.startswith("-")]
        # `git -C dir status`: the first non-flag is the dir only when -C precedes it.
        if "-C" in rest:
            i = rest.index("-C")
            sub = [t for t in rest[i + 2:] if not t.startswith("-")]
        return bool(sub) and sub[0] in SAFE_GIT
    return True


def shell_is_safe(command: str) -> bool:
    cleaned = _HARMLESS_REDIRECTS.sub(" ", command)
    if ">" in cleaned or "`" in cleaned or "$(" in cleaned or "<(" in cleaned:
        return False
    try:
        whole = shlex.split(cleaned)
    except ValueError:
        whole = []
    if len(whole) == 3 and (inner := _inner_command(whole)) is not None:
        return shell_is_safe(inner)  # `bash -c '...'` is one word to split() but many commands to us
    for segment in _SEGMENTS.split(cleaned):
        if not segment.strip():
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        if not _segment_is_safe(tokens):
            return False
    return True


def is_change(tool: str, args: dict | None) -> bool:
    """Would this tool call change the working copy (or might it)?"""
    if tool in WRITE_TOOLS:
        return True
    if tool in SHELL_TOOLS:
        command = _command_of(args or {})
        return command is None or not shell_is_safe(command)
    return False


class EditLease:
    """Which chat may change files right now. `alive(chat_id)` says whether a
    chat is still in a turn; a holder whose turn is over never blocks anyone
    even if nobody released it explicitly."""

    def __init__(self) -> None:
        self.holder: str | None = None
        self.waiters: list[str] = []

    def try_take(self, chat_id: str, alive: Callable[[str], bool]) -> str | None:
        """None = it is yours now (or already was); else the id of the holder."""
        if self.holder in (None, chat_id) or not alive(self.holder):
            self.holder = chat_id
            self.forget(chat_id)
            return None
        return self.holder

    def wait(self, chat_id: str) -> None:
        if chat_id not in self.waiters:
            self.waiters.append(chat_id)

    def forget(self, chat_id: str) -> None:
        if chat_id in self.waiters:
            self.waiters.remove(chat_id)

    def release(self, chat_id: str) -> list[str]:
        """The holder's turn ended: free the lease, return who was waiting."""
        if self.holder != chat_id:
            return []
        self.holder = None
        waiting, self.waiters = self.waiters, []
        return waiting
