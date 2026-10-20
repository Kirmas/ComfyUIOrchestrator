"""Agent CLIs a chat can run on: Claude Code, Codex, Antigravity.

The runner (runner.py) owns what is the same for every chat -- the transcript,
status, Stop, SSE, the process lifecycle. Everything that differs per CLI lives
in one `Provider` class here, and that *is* the interface:

- `launch`  -- the argv/env for one turn (one process per turn, resumed by the
  CLI's own session id);
- `begin`   -- how the message reaches the process (stdin protocol, stdin
  text, or already on argv);
- `on_event` -- one line of the CLI's output -> the runner's normalized
  transcript events (text / tool_use / tool_result / result / error);
- the lists the page shows: models and permission modes.

A provider never knows about chat kinds. The profile (runner.py) says *what*
the chat needs in neutral terms (`TurnSpec`: the system prompt, "only the
orchestrator's MCP, nothing else", the night-mode limits); each provider
translates that into its own flags. Only Claude Code can ask the page before
a tool call (`interactive`); Codex and Antigravity run headless, so their
permission modes are fixed policies chosen up front.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

DATA_DIR = Path(os.environ.get("RUNNER_DATA_DIR", Path.home() / ".local/share/comfy-agent-runner"))
RUNNER_PORT = int(os.environ.get("RUNNER_PORT", 8765))
TOKENS = {t.strip() for t in os.environ.get("RUNNER_TOKENS", "").split(",") if t.strip()}
HOOKS_CONFIG = Path.home() / ".gemini/config/hooks.json"
BRIDGE_SCRIPT = Path(__file__).resolve().parent / "comfy_hook_bridge.py"

# A tool result or tool input can be megabytes (a recipe read, a workflow
# JSON). The transcript is for a person to skim, so it keeps a prefix.
MAX_SNIPPET = 4000

IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def snippet(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= MAX_SNIPPET else text[:MAX_SNIPPET] + f"\n… ({len(text) - MAX_SNIPPET} more chars)"


def send_line(proc: asyncio.subprocess.Process, obj: dict) -> None:
    proc.stdin.write((json.dumps(obj) + "\n").encode())


def tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, dict):
                parts.append(f"[{c.get('type')}]")
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def find_binary(env_var: str, name: str) -> str | None:
    """The CLI's path, or None if it isn't installed. The runner is a systemd
    user service, whose PATH doesn't include ~/.local/bin."""
    explicit = os.environ.get(env_var)
    if explicit:
        return explicit
    return shutil.which(name) or (str(p) if (p := Path.home() / ".local/bin" / name).exists() else None)


class LimitsStore:
    """The last usage-limit reading per provider, kept on disk so the page has
    something to show right after a runner restart. A reading arrives for free
    with every turn (Claude reports it in its stream); `probe_limits` asks for
    one on demand. Shape, the same for every provider:
    {"status": str|None, "updated_at": ts,
     "windows": [{"id": "five_hour", "utilization": 0..1|None, "resets_at": ts|None}]}"""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict[str, dict] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}

    def update(self, provider_id: str, status: str | None, windows: list[dict]) -> None:
        self.data[provider_id] = {"status": status, "updated_at": time.time(), "windows": windows}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)


limits = LimitsStore(DATA_DIR / "limits.json")


class SetupError(Exception):
    """The CLI needs a one-time setting only the person should make; the
    message says which. Shown in the transcript as is."""


@dataclass
class TurnSpec:
    """What a chat needs from its CLI, in terms no CLI owns."""

    system_prompt: str
    # A project chat: the orchestrator's own MCP server and nothing else -- no
    # shell, no files, none of the user's own settings.
    isolated: bool = False
    mcp_url: str | None = None
    mcp_token: str | None = None
    # Night mode limits (Claude only; a night report never picks a provider).
    budget_usd: str | None = None
    disallowed: list[str] = field(default_factory=list)


@dataclass
class Launch:
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)


# The runner schedules a permission check or structured user question here.
AskPermission = Callable[[str, dict], None]


class Provider:
    id: str
    label: str
    binary_env: str
    binary_name: str
    # [{"id", "label"}], the first is the default.
    models: list[dict]
    # Dev chats' permission modes (id -> picked per chat), the first is the default.
    permission_modes: list[str] = []
    # Can the CLI stop and ask the page before a tool call (Allow / Deny)?
    interactive = False
    accepts_images = False
    # A project chat needs the isolation above, which not every CLI can give.
    supports_project = False
    # Reasoning effort levels the CLI takes (empty = no such knob), and whether
    # it has a faster-output mode. Both are per-chat picks, remembered in meta.
    efforts: list[str] = []
    supports_fast = False
    supports_compact = False

    def context_window(self, model: str) -> int:
        return 200_000

    def get_context_tokens(self, chat) -> int | None:
        return None

    @property
    def binary(self) -> str | None:
        return find_binary(self.binary_env, self.binary_name)

    @property
    def available(self) -> bool:
        return self.binary is not None

    @property
    def default_model(self) -> str:
        return self.models[0]["id"]

    @property
    def model_ids(self) -> set[str]:
        return {m["id"] for m in self.models}

    def model_for(self, meta: dict) -> str:
        requested = meta.get("requested_model")
        return requested if requested in self.model_ids else self.default_model

    def mode_for(self, meta: dict) -> str | None:
        if not self.permission_modes:
            return None
        mode = meta.get("permission_mode")
        return mode if mode in self.permission_modes else self.permission_modes[0]

    def effort_for(self, meta: dict) -> str | None:
        effort = meta.get("effort")
        return effort if effort in self.efforts else None

    def fast_allowed(self, model: str) -> bool:
        """Does this model have the faster-output mode at all?"""
        return False

    def fast_for(self, meta: dict) -> bool:
        return bool(self.supports_fast and meta.get("fast_mode") and self.fast_allowed(self.model_for(meta)))

    async def probe_limits(self) -> None:
        """Take a usage-limit reading now (into `limits`); CLIs that can't
        report one leave it be."""

    def describe(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "available": self.available,
            "models": self.models,
            "default": self.default_model,
            "permission_modes": self.permission_modes,
            "supports_project": self.supports_project,
            "accepts_images": self.accepts_images,
            "supports_compact": self.supports_compact,
            "efforts": self.efforts,
            "supports_fast": self.supports_fast,
            "fast_models": [m["id"] for m in self.models if self.fast_allowed(m["id"])],
        }

    # -- sessions ------------------------------------------------------------

    def new_session_id(self) -> str | None:
        """Id to store at creation, or None if the CLI hands one out itself."""
        return None

    def adopt_session(self, chat, session_id: str | None) -> None:
        """Remember the id the CLI reported; from then on a turn resumes it."""
        if session_id and chat.meta.get("session_id") != session_id:
            chat.meta["session_id"] = session_id
        if session_id and not chat.meta.get("session_started"):
            chat.meta["session_started"] = True
        chat.save_meta()

    # -- one turn ------------------------------------------------------------

    def cwd(self, chat, spec: TurnSpec, default: Path) -> Path:
        """Where the process runs; the profile's pick unless the CLI needs better."""
        return default

    def launch(self, chat, spec: TurnSpec, text: str, images: list[Path]) -> Launch:
        raise NotImplementedError

    async def begin(self, chat, proc: asyncio.subprocess.Process, spec: TurnSpec, text: str, images: list[Path]) -> None:
        """Get the message to the process. Default: it is already on argv."""
        proc.stdin.close()

    async def on_event(self, chat, proc: asyncio.subprocess.Process, event: dict, ask: AskPermission) -> None:
        raise NotImplementedError

    async def answer_request(self, proc, req_id, request, answer):
        send_line(proc, {"type": "control_response", "response": {"subtype": "success", "request_id": req_id, "response": answer}})
        await proc.stdin.drain()

    async def switch_mode(self, proc: asyncio.subprocess.Process, mode: str) -> None:
        """Apply a permission mode to a turn already running, if the CLI can."""

    def prompt_for(self, chat, spec: TurnSpec, text: str) -> str:
        """These CLIs have no system-prompt flag, so the instructions ride in
        the conversation's first message (the session keeps them after that)."""
        if chat.meta.get("session_started"):
            return text
        return (
            f"{spec.system_prompt}\n"
            "You cannot ask the person for permission mid-turn: if an action is refused or denied, "
            "say so plainly and stop instead of working around it."
            + ("" if spec.isolated else " Read CLAUDE.md in the repo root first.")
            + f"\n\n---\n{text}"
        )


# --- Claude Code ------------------------------------------------------------


class ClaudeProvider(Provider):
    id = "claude"
    label = "Claude"
    binary_env = "CLAUDE_BIN"
    binary_name = "claude"
    # What a chat can run on: model *families*, as the CLI's own aliases, which
    # it resolves to the newest model of that family. A CLI update therefore
    # brings a new Sonnet/Opus/Haiku with no change here -- deliberately, the
    # user wants "low/mid/high", not to track version numbers. What actually
    # answered is still visible: the CLI reports the resolved id on every turn.
    models = [
        {"id": "opus", "label": "Opus"},
        {"id": "sonnet", "label": "Sonnet"},
        {"id": "haiku", "label": "Haiku"},
    ]
    # How a dev chat's tool calls get approved (the CLI's --permission-mode).
    permission_modes = ["auto", "default", "acceptEdits"]
    interactive = True
    accepts_images = True
    supports_project = True
    # "ultracode" is not a level but a session mode the same flag turns on: the
    # model orchestrates dynamic workflows on every substantial task, optimizing
    # for the most exhaustive answer with no regard for token cost.
    efforts = ["low", "medium", "high", "xhigh", "max", "ultracode"]
    # Fast mode: the same Opus, answering faster, at a higher price per token
    # (a research preview of the CLI; `/fast` in a terminal). Only Opus has it.
    supports_fast = True

    def fast_allowed(self, model):
        return model == "opus"

    def model_for(self, meta):
        """Chats created before the switch to aliases stored an exact id
        ("claude-sonnet-5"); those map to their family."""
        requested = meta.get("requested_model") or self.default_model
        if requested in self.model_ids:
            return requested
        return next((m for m in self.model_ids if m in requested), self.default_model)

    def mode_for(self, meta):
        # A night report is always auto -- there is nobody to ask.
        if meta.get("origin") == "agent":
            return "auto"
        return super().mode_for(meta)

    def new_session_id(self):
        import uuid

        return str(uuid.uuid4())

    def session_args(self, meta: dict) -> list[str]:
        if meta.get("session_started"):
            return ["--resume", meta["session_id"]]
        return ["--session-id", meta["session_id"]]

    def launch(self, chat, spec, text, images):
        args = [
            self.binary, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            # Resuming with a different --model is fine: the session is the
            # transcript, not the model, so switching mid-chat takes effect on
            # the next turn.
            "--model", self.model_for(chat.meta),
        ]
        if spec.isolated:
            mcp_path = chat.dir / "mcp.json"
            mcp_path.write_text(json.dumps({
                "mcpServers": {
                    "orchestrator": {
                        "type": "http",
                        "url": spec.mcp_url,
                        "headers": {"Authorization": f"Bearer {spec.mcp_token}"},
                    }
                }
            }))
            mcp_path.chmod(0o600)
            args += [
                # Exactly one built-in tool (AskUserQuestion) plus the MCP
                # server below -- no Bash/Read/Edit/WebFetch/etc, and none of
                # the user's own settings/CLAUDE.md/plugins leak into a project
                # chat. AskUserQuestion always rides can_use_tool regardless of
                # --allowedTools (that's how its answer gets delivered back, see
                # ask_permission's is_question branch), hence
                # --permission-prompt-tool stdio here; every other tool call
                # this chat can make is already pre-allowed, so nothing else
                # reaches that channel or gets parked on the page.
                "--tools", "AskUserQuestion",
                "--setting-sources", "",
                "--strict-mcp-config", "--mcp-config", str(mcp_path),
                "--allowedTools", "mcp__orchestrator", "AskUserQuestion",
                "--permission-prompt-tool", "stdio",
            ]
        else:
            # The user's own settings, CLAUDE.md, memory and MCP servers load
            # as they would in a terminal (no --setting-sources override). The
            # permission mode is the chat's own pick; whatever it wants
            # confirmed comes to the page -- or, in a turn started by a bug
            # report, is denied (Profile.auto_decide).
            args += ["--permission-mode", self.mode_for(chat.meta), "--permission-prompt-tool", "stdio"]
            if spec.budget_usd:
                args += ["--max-budget-usd", spec.budget_usd]
            if spec.disallowed:
                args += ["--disallowedTools", *spec.disallowed]
        if effort := self.effort_for(chat.meta):
            args += ["--effort", effort]
        if self.fast_for(chat.meta):
            # --settings layers on top of every setting source, including a
            # project chat's empty one.
            args += ["--settings", json.dumps({"fastMode": True})]
        args += ["--append-system-prompt", spec.system_prompt, *self.session_args(chat.meta)]
        return Launch(args)

    async def begin(self, chat, proc, spec, text, images):
        send_line(proc, {"type": "control_request", "request_id": "init", "request": {"subtype": "initialize", "hooks": None}})
        content: str | list = text
        if images:
            # Straight into the message as image blocks, the same as a paste
            # into a terminal session -- no tool call needed to see them.
            content = [self._image_block(p) for p in images]
            if text:
                content.append({"type": "text", "text": text})
        send_line(proc, {"type": "user", "message": {"role": "user", "content": content}})
        await proc.stdin.drain()

    @staticmethod
    def _image_block(path: Path) -> dict:
        media_type = next(t for t, e in IMAGE_TYPES.items() if e == path.suffix[1:])
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(path.read_bytes()).decode()}}

    async def on_event(self, chat, proc, e, ask):
        t = e.get("type")
        if t == "control_request":
            req_id, request = e.get("request_id"), e.get("request") or {}
            if request.get("subtype") == "can_use_tool":
                ask(req_id, request)
            else:
                send_line(proc, {"type": "control_response", "response": {"subtype": "error", "request_id": req_id, "error": "not supported by the agent runner"}})
                await proc.stdin.drain()
        elif t == "system" and e.get("subtype") == "init":
            if not chat.meta.get("session_started"):
                chat.meta["session_started"] = True
                chat.save_meta()
            chat.meta["model"] = e.get("model")
            # claude.ai's own connectors come along with the user's login in a
            # dev chat and routinely don't connect headless; they're not ours
            # to warn about.
            failed = [
                s.get("name") for s in e.get("mcp_servers", [])
                if s.get("status") != "connected" and not str(s.get("name", "")).startswith("claude.ai ")
            ]
            if failed:
                await chat.emit({"type": "error", "text": f"MCP server not connected: {', '.join(failed)}"})
        elif t == "assistant":
            for c in e.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text", "").strip():
                    await chat.emit({"type": "text", "text": c["text"]})
                elif c.get("type") == "tool_use":
                    await chat.emit({"type": "tool_use", "id": c.get("id"), "name": c.get("name"), "input": snippet(c.get("input"))})
        elif t == "user":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        await chat.emit({
                            "type": "tool_result",
                            "tool_use_id": c.get("tool_use_id"),
                            "is_error": bool(c.get("is_error")),
                            "content": snippet(tool_result_text(c.get("content"))),
                        })
        elif t == "rate_limit_event":
            self.record_limits(e.get("rate_limit_info") or {})
        elif t == "result":
            await chat.emit({
                "type": "result",
                "is_error": bool(e.get("is_error")),
                "subtype": e.get("subtype"),
                "duration_ms": e.get("duration_ms"),
                "cost_usd": e.get("total_cost_usd"),
                "num_turns": e.get("num_turns"),
            })
            # The turn is over; closing stdin is what lets the CLI exit.
            proc.stdin.close()

    def record_limits(self, info: dict) -> None:
        """rate_limit_info: `unifiedWindows` holds each window the plan has
        (five_hour, seven_day...) with its utilization 0..1 and reset time;
        older shapes only name the window that's closest to its limit."""
        windows = [
            {"id": name, "utilization": w.get("utilization"), "resets_at": w.get("resetsAt")}
            for name, w in (info.get("unifiedWindows") or {}).items()
        ]
        if not windows and info.get("rateLimitType"):
            windows = [{"id": info["rateLimitType"], "utilization": None, "resets_at": info.get("resetsAt")}]
        if windows:
            limits.update(self.id, info.get("status"), windows)

    async def probe_limits(self):
        # The cheapest possible turn (Haiku, no tools, no settings): the limits
        # reading is part of every turn's stream.
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            self.binary, "-p", ".", "--output-format", "stream-json", "--verbose",
            "--model", "haiku", "--tools", "", "--setting-sources", "", "--no-session-persistence",
            cwd=DATA_DIR, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 90)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        for line in out.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "rate_limit_event":
                self.record_limits(e.get("rate_limit_info") or {})

    async def switch_mode(self, proc, mode):
        import uuid

        send_line(proc, {"type": "control_request", "request_id": f"mode-{uuid.uuid4().hex[:8]}", "request": {"subtype": "set_permission_mode", "mode": mode}})
        await proc.stdin.drain()


# --- Codex ------------------------------------------------------------------


class CodexProvider(Provider):
    """One stdio JSON-RPC app-server per turn, with persistent thread resume.
    Questions are interactive; tool permissions remain fixed sandbox policies.
    """

    id = "codex"
    label = "Codex"
    binary_env = "CODEX_BIN"
    binary_name = "codex"
    models = [
        {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol"},
        {"id": "gpt-6-astra", "label": "GPT-6 Astra"},
        {"id": "gpt-6-luna", "label": "GPT-6 Luna"},
    ]
    permission_modes = ["workspace", "full", "readOnly"]
    accepts_images = True
    # A project chat: read-only sandbox, the user's own config (other MCP
    # servers, plugins) not loaded, the orchestrator's MCP the only tool that
    # reaches anything. The shell is still there, but it can only read.
    supports_project = True
    MCP_TOKEN_ENV = "ORCH_MCP_TOKEN"
    # Common levels advertised for all three models by Codex 0.163.0.
    # Sol/Astra also advertise ultra; Luna does not.
    efforts = ["low", "medium", "high", "xhigh", "max"]
    supports_fast = True

    SANDBOX = {"workspace": "workspace-write", "full": "danger-full-access", "readOnly": "read-only"}

    def fast_allowed(self, model):
        return model in self.model_ids

    def launch(self, chat, spec, text, images):
        args = [self.binary, "app-server", "--stdio",
                "-c", 'features.default_mode_request_user_input=true']
        env = {}
        if spec.isolated:
            # app-server has no --ignore-user-config. A separate home loads
            # no personal config/plugins, but shares login and existing threads.
            home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            isolated_home = chat.dir / "codex-home"
            isolated_home.mkdir(exist_ok=True)
            for name in ("auth.json", "sessions", "models_cache.json"):
                target = isolated_home / name
                if not target.exists() and not target.is_symlink() and (home / name).exists():
                    target.symlink_to(home / name)
            env["CODEX_HOME"] = str(isolated_home)
            env[self.MCP_TOKEN_ENV] = spec.mcp_token
            args += ["-c", "features.plugins=false", "-c", "features.apps=false",
                     "-c", f"mcp_servers.orchestrator.url={json.dumps(spec.mcp_url)}",
                     "-c", f'mcp_servers.orchestrator.bearer_token_env_var="{self.MCP_TOKEN_ENV}"',
                     "-c", 'mcp_servers.orchestrator.default_tools_approval_mode="approve"']
        return Launch(args, env)

    async def begin(self, chat, proc, spec, text, images):
        mode = "readOnly" if spec.isolated else self.mode_for(chat.meta)
        prompt = spec.system_prompt
        if spec.isolated:
            prompt = prompt.replace(
                "You have no shell and no file\naccess.",
                "This runtime also exposes a read-only shell; use only the orchestrator MCP for project work.",
            )
        else:
            prompt += "\nRead CLAUDE.md in the repo root before doing repository work."
        instructions = prompt + (
            "\nThis Codex client supports structured user questions via request_user_input. "
            "Use it for questions that need an answer, and wait for the answer before finishing. "
            "Tool permissions are fixed for this turn; there are no Allow/Deny prompts. "
            "If a tool is denied, stop and report the denial."
        )
        params = {
            "model": self.model_for(chat.meta), "sandbox": self.SANDBOX[mode],
            "approvalPolicy": "never", "serviceTier": "priority" if self.fast_for(chat.meta) else "default",
            "developerInstructions": instructions,
        }
        if effort := self.effort_for(chat.meta):
            params["config"] = {"model_reasoning_effort": effort}
        if chat.meta.get("session_started"):
            params.update(threadId=chat.meta["session_id"], excludeTurns=True)
        chat.turn_state["rpc_thread"] = params
        chat.turn_state["expects_result"] = True
        chat.turn_state["rpc_input"] = [{"type": "text", "text": text}] + [
            {"type": "localImage", "path": str(path)} for path in images
        ]
        send_line(proc, {"id": "init", "method": "initialize", "params": {
            "clientInfo": {"name": "comfy_orchestrator", "version": "1"},
            "capabilities": {"experimentalApi": True},
        }})
        await proc.stdin.drain()

    async def on_event(self, chat, proc, e, ask):
        method, params = e.get("method"), e.get("params") or {}
        state = chat.turn_state
        if method and "id" in e:
            if method == "item/tool/requestUserInput":
                # Keep Codex ids private; the existing UI answers by question text.
                questions = params.get("questions") or []
                request = {"tool_name": "AskUserQuestion", "requires_user_interaction": True,
                           "input": {"questions": [dict(q, multiSelect=False, options=q.get("options") or []) for q in questions]},
                           "codex_id": e["id"], "codex_questions": questions}
                ask(f'{params["turnId"]}:{e["id"]}', request)
            else:
                # Never silently approve an unimplemented server request.
                send_line(proc, {"id": e["id"], "error": {"code": -32601, "message": f"Unsupported client request: {method}"}})
                await proc.stdin.drain()
                await chat.emit({"type": "error", "text": f"Codex requested unsupported interaction: {method}"})
            return
        if params.get("threadId") and params["threadId"] != chat.meta.get("session_id"):
            # A child thread's completion must not close the parent server.
            return
        if not method and "id" in e:
            if e.get("error"):
                state["failed"] = True
                await chat.emit({"type": "error", "text": (e["error"].get("message") or snippet(e["error"]))})
                proc.stdin.close()
                return
            result = e.get("result") or {}
            if e["id"] == "init":
                send_line(proc, {"method": "initialized"})
                send_line(proc, {"id": "thread", "method": "thread/resume" if chat.meta.get("session_started") else "thread/start",
                                 "params": state.pop("rpc_thread")})
            elif e["id"] == "thread":
                self.adopt_session(chat, result["thread"]["id"])
                chat.meta["model"] = result.get("model")
                chat.save_meta()
                send_line(proc, {"id": "turn", "method": "turn/start", "params": {
                    "threadId": chat.meta["session_id"], "input": state.pop("rpc_input"),
                }})
            await proc.stdin.drain()
            return
        if method == "account/rateLimits/updated":
            self.record_limits(params.get("rateLimits") or {})
        elif method == "thread/tokenUsage/updated":
            state["usage"] = params.get("tokenUsage", {}).get("last", {})
        elif method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            kind, item_id, done = item.get("type"), item.get("id"), method == "item/completed"
            if kind == "agentMessage" and done and item.get("text", "").strip():
                await chat.emit({"type": "text", "text": item["text"]})
            elif kind in ("commandExecution", "fileChange", "mcpToolCall", "webSearch"):
                normalized = dict(item, type={"commandExecution": "command_execution", "fileChange": "file_change",
                                             "mcpToolCall": "mcp_tool_call", "webSearch": "web_search"}[kind],
                                  exit_code=item.get("exitCode"), aggregated_output=item.get("aggregatedOutput"))
                seen = state.setdefault("tools", set())
                if item_id not in seen:
                    seen.add(item_id)
                    await chat.emit({"type": "tool_use", "id": item_id, **self._tool_call(normalized)})
                if done:
                    await chat.emit({"type": "tool_result", "tool_use_id": item_id, **self._tool_result(normalized)})
        elif method == "error":
            await chat.emit({"type": "error", "text": (params.get("error") or {}).get("message") or snippet(params)})
        elif method == "turn/completed":
            state["result_received"] = True
            turn = params.get("turn") or {}
            failed = turn.get("status") != "completed"
            state["failed"] = failed
            if turn.get("error"):
                await chat.emit({"type": "error", "text": turn["error"].get("message") or snippet(turn["error"])})
            await chat.emit({"type": "result", "is_error": failed, "subtype": turn.get("status"),
                             "num_turns": 1, "usage": state.get("usage", {})})
            proc.stdin.close()

    async def answer_request(self, proc, req_id, request, answer):
        picked = answer.get("updatedInput", {}).get("answers", {})
        answers = {q["id"]: {"answers": [picked[q["question"]]] if q["question"] in picked else []}
                   for q in request["codex_questions"]}
        send_line(proc, {"id": request["codex_id"], "result": {"answers": answers}})
        await proc.stdin.drain()

    def record_limits(self, info: dict) -> bool:
        """Accept token_count payloads, rollout envelopes, or a limit snapshot.
        Exec's token usage is NOT plan utilization; only rate_limits is."""
        info = info.get("payload", info)
        info = info.get("rate_limits", info)
        if not isinstance(info, dict):
            return False
        windows = []
        for name in ("primary", "secondary"):
            w = info.get(name)
            if not isinstance(w, dict):
                continue
            used = w.get("used_percent", w.get("usedPercent"))
            if not isinstance(used, (int, float)) or not 0 <= used <= 100:
                continue
            window_id = {300: "five_hour", 10080: "seven_day"}.get(w.get("window_minutes", w.get("windowDurationMins")), name)
            windows.append({"id": window_id, "utilization": used / 100, "resets_at": w.get("resets_at", w.get("resetsAt"))})
        if not windows:
            return False
        status = "rejected" if info.get("rate_limit_reached_type", info.get("rateLimitReachedType")) or any(w["utilization"] >= 1 for w in windows) else "allowed"
        limits.update(self.id, status, windows)
        return True

    async def probe_limits(self):
        """app-server can read account limits without a model turn or tokens."""
        proc = await asyncio.create_subprocess_exec(
            self.binary, "app-server", "--stdio", "-c", 'sandbox_mode="read-only"',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            send_line(proc, {"id": "init", "method": "initialize", "params": {
                "clientInfo": {"name": "comfy_orchestrator_limits", "version": "1"},
            }})
            await proc.stdin.drain()
            async with asyncio.timeout(30):
                async for line in proc.stdout:
                    event = json.loads(line)
                    if event.get("error"):
                        raise RuntimeError(event["error"].get("message", "Codex limits request failed"))
                    if event.get("id") == "init":
                        send_line(proc, {"method": "initialized"})
                        send_line(proc, {"id": "limits", "method": "account/rateLimits/read"})
                        await proc.stdin.drain()
                    elif event.get("id") == "limits":
                        if self.record_limits((event.get("result") or {}).get("rateLimits") or {}):
                            return
                        break
            raise RuntimeError("Codex returned no plan limits; previous reading retained.")
        finally:
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    @staticmethod
    def _tool_call(item: dict) -> dict:
        kind = item["type"]
        if kind == "command_execution":
            return {"name": "Bash", "input": snippet({"command": item.get("command")})}
        if kind == "file_change":
            return {"name": "Edit", "input": snippet([f"{c.get('kind')} {c.get('path')}" for c in item.get("changes") or []])}
        if kind == "mcp_tool_call":
            return {"name": f"mcp__{item.get('server')}__{item.get('tool')}", "input": snippet(item.get("arguments"))}
        return {"name": "WebSearch", "input": snippet({"query": item.get("query")})}

    @staticmethod
    def _tool_result(item: dict) -> dict:
        kind = item["type"]
        if kind == "command_execution":
            return {"is_error": item.get("exit_code") not in (0, None), "content": snippet(item.get("aggregated_output") or "")}
        if kind == "mcp_tool_call":
            failed = item.get("status") == "failed" or bool(item.get("error"))
            body = item.get("error") if failed else item.get("result")
            if isinstance(body, dict) and "content" in body:
                body = tool_result_text(body["content"])
            elif isinstance(body, dict) and "message" in body:
                body = body["message"]
            return {"is_error": failed, "content": snippet(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str))}
        return {"is_error": item.get("status") == "failed", "content": item.get("status") or "done"}


# --- Antigravity ------------------------------------------------------------


class AgyProvider(Provider):
    """`agy -p --output-format stream-json`. Interactive questions (`ask_question`)
    and tool permission checks are handled via a PreToolUse lifecycle hook bridge
    that routes them to comfy-agent-runner's web UI.
    """

    id = "agy"
    label = "Antigravity"
    binary_env = "AGY_BIN"
    binary_name = "agy"
    models = [
        {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "efforts": ["low", "medium", "high"]},
        {"id": "gemini-4-argon", "label": "Gemini 4 Argon", "efforts": ["low", "medium", "high"]},
        {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "efforts": ["low", "medium", "high"]},
        {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "efforts": ["low", "medium", "high"]},
        {"id": "gpt-oss-120b", "label": "GPT-OSS 120B", "efforts": ["medium"]},
    ]
    permission_modes = ["edits", "auto", "full", "plan"]
    interactive = True
    accepts_images = True
    # A project chat: edits-only (so a command is refused, not run), the
    # orchestrator's MCP from a workspace config. Two one-time settings belong
    # to the person, not to this code (an agent's permission rules and trusted
    # folders are theirs to grant): the workspace must be trusted, and MCP
    # calls allowed -- _check_project_setup says exactly what to add.
    supports_project = True
    efforts = ["low", "medium", "high"]
    supports_fast = False
    supports_compact = True
    SETTINGS = Path.home() / ".gemini/antigravity-cli/settings.json"
    MCP_RULE = "mcp(orchestrator/*)"
    # One fixed folder for every project chat (they share the same MCP
    # server), so there is a single path to trust instead of one per chat.
    PROJECT_DIR = DATA_DIR / "agy-project"

    @staticmethod
    def context_window(model: str) -> int:
        base = re.sub(r"-(low|medium|high|xhigh|max)$", "", model or "")
        if "gemini" in base:
            return 1_048_576
        if "claude" in base:
            return 200_000
        if "gpt" in base:
            return 128_000
        return 1_048_576

    def get_context_tokens(self, chat) -> int | None:
        session_id = chat.meta.get("session_id")
        if not session_id:
            return None
        try:
            p = Path.home() / ".gemini/antigravity-cli/brain" / session_id / ".system_generated/logs/transcript.jsonl"
            if not p.exists():
                return None
            size = p.stat().st_size
            with p.open("rb") as f:
                f.seek(max(0, size - 131072))
                lines = f.read().decode("utf-8", errors="replace").splitlines()
            for line in reversed(lines):
                if '"input_tokens"' in line:
                    data = json.loads(line)
                    inp = data.get("input_tokens") or 0
                    cache = data.get("cache_read_tokens") or 0
                    if inp or cache:
                        return inp + cache
        except Exception:
            pass
        return None

    def model_for(self, meta: dict) -> str:
        requested = meta.get("requested_model")
        if requested in self.model_ids:
            return requested
        if requested:
            base = re.sub(r"-(low|medium|high|xhigh|max)$", "", requested)
            if base in self.model_ids:
                return base
        return self.default_model

    def cwd(self, chat, spec, default):
        return self.PROJECT_DIR if spec.isolated else default

    def _ensure_hooks_setup(self) -> None:
        try:
            HOOKS_CONFIG.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(HOOKS_CONFIG.read_text())
            except (OSError, ValueError):
                data = {}
            bridge_cmd = f"python3 {BRIDGE_SCRIPT}"
            data["comfy-runner-bridge"] = {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": bridge_cmd,
                                "timeout": 3600,
                            }
                        ],
                    }
                ]
            }
            tmp = HOOKS_CONFIG.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(HOOKS_CONFIG)
        except OSError:
            pass

    def prompt_for(self, chat, spec: TurnSpec, text: str) -> str:
        if chat.meta.get("session_started"):
            return text
        return (
            f"{spec.system_prompt}\n"
            "When you need clarification or a decision from the user, use the ask_question tool. "
            "If an action is refused or denied, explain clearly and proceed without that action."
            + ("" if spec.isolated else " Read CLAUDE.md in the repo root first.")
            + f"\n\n---\n{text}"
        )

    def _check_project_setup(self, spec) -> None:
        self.PROJECT_DIR.mkdir(parents=True, exist_ok=True)
        config = self.PROJECT_DIR / ".agents" / "mcp_config.json"
        config.parent.mkdir(exist_ok=True)
        config.write_text(json.dumps({"mcpServers": {"orchestrator": {
            "serverUrl": spec.mcp_url,
            "headers": {"Authorization": f"Bearer {spec.mcp_token}"},
        }}}))
        config.chmod(0o600)
        try:
            settings = json.loads(self.SETTINGS.read_text())
        except (OSError, ValueError):
            settings = {}
        missing = []
        if str(self.PROJECT_DIR) not in settings.get("trustedWorkspaces", []):
            missing.append(f'add "{self.PROJECT_DIR}" to "trustedWorkspaces"')
        if self.MCP_RULE not in (settings.get("permissions") or {}).get("allow", []):
            missing.append(f'add "{self.MCP_RULE}" to "permissions" -> "allow"')
        if missing:
            raise SetupError(
                f"Antigravity project chats need a one-time setting in {self.SETTINGS}: " + "; ".join(missing)
                + ". (Headless, it can't ask, so without them it refuses the folder and the MCP tools.)"
            )

    def launch(self, chat, spec, text, images):
        if spec.isolated:
            self._check_project_setup(spec)
        self._ensure_hooks_setup()
        mode = "edits" if spec.isolated else self.mode_for(chat.meta)
        model = self.model_for(chat.meta)
        effort = self.effort_for(chat.meta)
        if effort:
            # Strip model suffix (-low/-medium/-high/-xhigh/-max) so it doesn't conflict with --effort
            base_model = re.sub(r"-(low|medium|high|xhigh|max)$", "", model)
            args = [self.binary, "--output-format", "stream-json", "--model", base_model, "--effort", effort, "--dangerously-skip-permissions"]
        else:
            args = [self.binary, "--output-format", "stream-json", "--model", model, "--dangerously-skip-permissions"]
        if chat.meta.get("session_started"):
            args += ["--conversation", chat.meta["session_id"]]
        token = next(iter(TOKENS), "")
        env = {
            "COMFY_AGENT_RUNNER_IPC": f"http://127.0.0.1:{RUNNER_PORT}/hooks/pre-tool",
            "COMFY_CHAT_ID": chat.id,
            "COMFY_PERMISSION_MODE": mode or "edits",
            "COMFY_IPC_TOKEN": token,
        }
        prompt_text = text
        if images:
            img_list = "\n".join(f"- {p}" for p in images)
            prompt_text = (
                f"{text}\n\n"
                f"[Attached images:\n{img_list}\n"
                "Please use the read_file tool to inspect the image file(s).]"
            )
        # `-p=...` (not `-p ...`): the CLI takes the next argument as the
        # prompt even when it is a flag.
        return Launch(args + [f"-p={self.prompt_for(chat, spec, prompt_text)}"], env=env)

    @staticmethod
    def _parse_reset_seconds(text: str) -> int | None:
        m = re.search(r"Resets in\s+(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", text, re.IGNORECASE)
        if not m:
            return None
        days = int(m.group(1) or 0)
        hours = int(m.group(2) or 0)
        minutes = int(m.group(3) or 0)
        seconds = int(m.group(4) or 0)
        total = days * 86400 + hours * 3600 + minutes * 60 + seconds
        return total if total > 0 else None

    def record_quota_error(self, model: str, error_text: str) -> bool:
        seconds = self._parse_reset_seconds(error_text)
        if seconds is None and "429" not in error_text and "RESOURCE_EXHAUSTED" not in error_text and "quota reached" not in error_text.lower():
            return False
        resets_at = int(time.time() + (seconds or 0)) if seconds else None
        new_window = {"id": model, "utilization": 1.0, "resets_at": resets_at}
        existing = limits.data.get(self.id, {}).get("windows", [])
        updated = [w for w in existing if w.get("id") != model]
        updated.append(new_window)
        limits.update(self.id, "rejected", updated)
        return True

    def record_limits(self, info: dict) -> bool:
        """Parse structured quota data from /usage command."""
        windows = []
        for g in info.get("groups", []):
            group_name = g.get("name")
            for b in g.get("buckets", []):
                rem = b.get("remaining_fraction")
                disabled = bool(b.get("disabled"))
                utilization = round(1.0 - rem, 4) if rem is not None else None
                resets_at = None
                if reset_time := b.get("reset_time"):
                    try:
                        resets_at = int(datetime.fromisoformat(reset_time.replace("Z", "+00:00")).timestamp())
                    except (ValueError, TypeError):
                        pass
                windows.append({
                    "id": b.get("id"),
                    "label": b.get("name") or b.get("id"),
                    "group": group_name,
                    "utilization": utilization,
                    "resets_at": resets_at,
                    "disabled": disabled,
                })
        if not windows:
            return False
        status = "rejected" if any(w["utilization"] is not None and w["utilization"] >= 1.0 and not w.get("disabled") for w in windows) else "allowed"
        limits.update(self.id, status, windows)
        return True

    async def probe_limits(self):
        """Read account limits from agy slash-command /usage."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            self.binary, "-p=/usage", "--mode", "plan", "--output-format", "stream-json",
            cwd=DATA_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        for line in out.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") == "command_result":
                cmd = e.get("command") or {}
                if cmd.get("name") == "usage" and self.record_limits(cmd.get("data") or {}):
                    return
        raise RuntimeError("Antigravity returned no usage limits; previous reading retained.")

    async def on_event(self, chat, proc, e, ask):
        t = e.get("event")
        state: dict = chat.turn_state
        if t == "init":
            self.adopt_session(chat, e.get("conversation_id"))
            chat.meta["model"] = (e.get("init") or {}).get("model") or chat.meta.get("model")
        elif t == "step_update":
            s = e.get("step_update") or {}
            if u := s.get("usage"):
                inp = u.get("input_tokens") or 0
                cache = u.get("cache_read_tokens") or 0
                if inp or cache:
                    chat.meta["context_tokens"] = inp + cache
            kind, step, status = s.get("step_type"), s.get("step_index"), s.get("state")
            if kind == "agent_response":
                state.setdefault("text", {})[step] = state.get("text", {}).get(step, "") + (s.get("text_delta") or "")
                if status == "DONE" and state["text"][step].strip():
                    await chat.emit({"type": "text", "text": state["text"][step]})
            elif kind == "tool":
                info = s.get("tool_info") or {}
                tool_id = f"{chat.id}:{step}"
                if status == "ACTIVE":
                    await chat.emit({"type": "tool_use", "id": tool_id, "name": s.get("tool_name") or info.get("name"), "input": snippet(info.get("parameters"))})
                elif status in ("DONE", "ERROR"):
                    error = (info.get("error") or {}).get("message") if status == "ERROR" else None
                    await chat.emit({"type": "tool_result", "tool_use_id": tool_id, "is_error": status == "ERROR", "content": snippet(error or "done")})
        elif t == "result":
            r = e.get("result") or {}
            denied = [d.get("display_name") or d.get("action") for d in r.get("denied_actions") or []]
            if denied:
                await chat.emit({
                    "type": "error",
                    "text": f"Antigravity can't ask for permission when run headless, so it refused: {', '.join(map(str, denied))}. "
                    "Switch this chat's mode to Full to let it run commands.",
                })
            err_msg = r.get("error")
            if err_msg:
                self.record_quota_error(self.model_for(chat.meta), err_msg)
            if r.get("status") != "SUCCESS" or err_msg:
                state["failed"] = True
                await chat.emit({"type": "error", "text": err_msg or f"Antigravity turn ended with status {r.get('status')}"})
            if ctx := self.get_context_tokens(chat):
                chat.meta["context_tokens"] = ctx
            if hasattr(chat, "save_meta"):
                chat.save_meta()
            await chat.emit({
                "type": "result",
                "is_error": r.get("status") != "SUCCESS",
                "subtype": r.get("status"),
                "duration_ms": int(r["duration_seconds"] * 1000) if r.get("duration_seconds") is not None else None,
                "num_turns": r.get("num_turns"),
                "usage": r.get("usage"),
                "context_tokens": chat.meta.get("context_tokens"),
                "context_window": self.context_window(self.model_for(chat.meta)),
            })


DEFAULT_PROVIDER = "claude"
PROVIDERS: dict[str, Provider] = {p.id: p for p in (ClaudeProvider(), CodexProvider(), AgyProvider())}


def provider_of(chat) -> Provider:
    """Chats created before providers existed have no `provider`: Claude."""
    return PROVIDERS[chat.meta.get("provider") or DEFAULT_PROVIDER]
