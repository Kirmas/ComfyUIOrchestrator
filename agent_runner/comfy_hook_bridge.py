#!/usr/bin/env python3
"""Antigravity Lifecycle Hook bridge for comfy-agent-runner.

Intercepts PreToolUse events from Google Antigravity (agy). When run under
comfy-agent-runner (COMFY_AGENT_RUNNER_IPC is set), routes interactive queries
(ask_question) and guarded tool execution (run_command in edits mode) to the runner's
HTTP endpoint, allowing the user to answer questions or grant permissions in the web UI.

When run outside comfy-agent-runner, exits immediately with allow (no-op).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


COMMAND_TOOLS = ("run_command", "notebook_execution")
FILE_WRITE_TOOLS = (
    "write_to_file",
    "replace_file_content",
    "sed_file",
    "multi_replace_file_content",
    "notebook_edit",
)


def respond(decision: str, reason: str | None = None) -> None:
    out: dict[str, str] = {"decision": decision}
    if reason:
        out["reason"] = reason
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        respond("allow")

    ipc_url = os.environ.get("COMFY_AGENT_RUNNER_IPC")
    chat_id = os.environ.get("COMFY_CHAT_ID")
    if not ipc_url or not chat_id:
        respond("allow")

    mode = os.environ.get("COMFY_PERMISSION_MODE", "edits")
    token = os.environ.get("COMFY_IPC_TOKEN", "")

    tool_call = payload.get("toolCall") or {}
    tool_name = tool_call.get("name")
    tool_args = tool_call.get("args") or {}

    if tool_name == "ask_question":
        req_body = {
            "chat_id": chat_id,
            "tool": "ask_question",
            "args": tool_args,
        }
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            ipc_url,
            data=json.dumps(req_body).encode("utf-8"),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=3600) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                answers = data.get("answers") or {}
                if answers:
                    ans_text = "; ".join(f"'{k}': '{v}'" for k, v in answers.items())
                    respond(
                        "deny",
                        f"The user answered your question: {ans_text}. Continue using this user answer.",
                    )
                else:
                    respond("allow")
        except Exception:
            respond("allow")

    if mode == "project":
        # Project chats work only through the orchestrator's MCP. Commands and
        # file writes would run as the runner's own user, anywhere on disk
        # (--dangerously-skip-permissions approves them), so they are refused
        # here rather than offered to the person as Allow / Deny.
        # ask_question (above), generate_image, reads and MCP are unaffected.
        if tool_name in COMMAND_TOOLS:
            respond(
                "deny",
                "Project chats have no shell. Read tool outputs with read_file and work from them directly.",
            )
        if tool_name in FILE_WRITE_TOOLS:
            respond(
                "deny",
                "Project chats can't write files. Keep notes in your reply; project state lives in the orchestrator (MCP).",
            )
        respond("allow")

    if mode == "plan":
        if tool_name in ("run_command", *FILE_WRITE_TOOLS):
            respond(
                "deny",
                "Chat is in Plan mode. File modifications and command execution are disabled.",
            )
        respond("allow")

    if mode == "edits":
        if tool_name in ("run_command", "notebook_execution"):
            req_body = {
                "chat_id": chat_id,
                "tool": tool_name,
                "args": tool_args,
            }
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(
                ipc_url,
                data=json.dumps(req_body).encode("utf-8"),
                headers=headers,
            )
            try:
                with urllib.request.urlopen(req, timeout=3600) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    dec = data.get("decision", "deny")
                    reason = data.get("reason", "Denied by user")
                    respond(dec, reason if dec == "deny" else None)
            except Exception:
                respond("deny", "Agent runner connection failed.")
        respond("allow")

    respond("allow")


if __name__ == "__main__":
    main()
