"""Agent handoff core logic: config, filtering, and first-message assembly.

Stage 1 implementation:
- One-way task handoff between project chats.
- Config in DATA_DIR / "handoff.json".
- Single filtering function for target models.
- First message assembly for the receiving agent.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request as UrlRequest, urlopen

from providers import DATA_DIR, DEFAULT_PROVIDER, PROVIDERS


DEFAULT_LIMITS = {
    "hide_margin": 0.35,
    "scarce_margin": 0.15,
    # Below this share used, a window is always ok whatever the pace: right
    # after a reset even a little use looks "ahead", and asking then is noise.
    "always_ok_below": 0.30,
    "ignore_windows": ["cloud_credits"],
    "durations": {},
    "pools": {
        "agy:gemini-3.8-flash": "Gemini Models",
        "agy:gemini-4-argon": "Gemini 4 Argon",
        "agy:claude-sonnet-5-5": "Claude and GPT models",
        "agy:claude-opus-5-5": "Claude and GPT models",
        "agy:gpt-oss-120b": "Claude and GPT models",
    },
}


# What each target is good for, how hungry it is, and whether it can draw by
# itself. Written by hand from public reviews (2026-10-22) plus the person's own
# experience, which wins: e.g. Opus spends fewer tokens per task than its price
# suggests, and Claude inside Antigravity is a different harness, so nothing
# about Claude Code carries over to it. Never inferred at run time.
# draws_natively = a built-in image generator of the model's own CLI. Every
# agent can draw through the orchestrator's ComfyUI tools regardless.
DEFAULT_PROFILES = {
    "claude:opus": {
        "good_for": "hardest work: understanding what the person wants, design decisions, rewriting a task for another model",
        "appetite": "high per token, but spends fewer tokens per task than its price suggests",
        "draws_natively": False,
    },
    "claude:sonnet": {
        "good_for": "everyday agentic work, multi-step tool use",
        "appetite": "medium per token, wordy",
        "draws_natively": False,
    },
    "claude:haiku": {
        "good_for": "narrow mechanical subtasks; weak on long multi-step chains",
        "appetite": "very low",
        "draws_natively": False,
    },
    "codex:gpt-6-astra": {
        "good_for": "maximum capability, hard science/terminal-style problems",
        "appetite": "highest of all targets",
        "draws_natively": True,
    },
    "codex:gpt-6.1-sol": {
        "good_for": "demanding everyday coding and reasoning, close to Astra",
        "appetite": "medium per token (about 1/5 of Astra), uses more tokens",
        "draws_natively": True,
    },
    "codex:gpt-6-luna": {
        "good_for": "repetitive high-volume work; clearly weaker",
        "appetite": "very low",
        "draws_natively": True,
    },
    "agy:gemini-3.8-flash": {
        "good_for": "simple to medium tasks, fast; the default cheap target",
        "appetite": "low, generous separate pool",
        "draws_natively": True,
    },
    "agy:gemini-4-argon": {
        "good_for": "strongest Gemini: code, tool use, reasoning",
        "appetite": "high in practice (thinking billed as output), small separate pool",
        "draws_natively": True,
    },
    "agy:claude-sonnet-5-5": {
        "good_for": "Sonnet in a foreign harness; behaviour may differ from Claude Code",
        "appetite": "unknown here; shares the Antigravity 'Claude and GPT' pool",
        "draws_natively": False,
    },
    "agy:claude-opus-5-5": {
        "good_for": "Opus in a foreign harness; behaviour may differ from Claude Code",
        "appetite": "unknown here; shares the Antigravity 'Claude and GPT' pool",
        "draws_natively": False,
    },
    "agy:gpt-oss-120b": {
        "good_for": "only very simple tasks; weak instruction following",
        "appetite": "low; shares the Antigravity 'Claude and GPT' pool",
        "draws_natively": False,
    },
}
PROFILE_TEXT_MAX = 300


class HandoffConfig:
    """Manages handoff settings and persistent rules in DATA_DIR / 'handoff.json'."""

    DEFAULT_TIMEOUT_MINUTES = 5
    DEFAULT_USER_MESSAGES_MAX = 20
    DEFAULT_EFFORT = {"*": "low"}
    DEFAULT_IDENTITIES = {
        "claude:opus": "claude-opus",
        "agy:claude-opus-5-5": "claude-opus",
        "claude:sonnet": "claude-sonnet",
        "agy:claude-sonnet-5-5": "claude-sonnet",
    }
    DEFAULT_LIMITS = DEFAULT_LIMITS

    def __init__(self, path: Path | None = None):
        self.path = path or (DATA_DIR / "handoff.json")
        self.timeout_minutes = self.DEFAULT_TIMEOUT_MINUTES
        self.user_messages_max = self.DEFAULT_USER_MESSAGES_MAX
        self.identities: dict[str, str] = dict(self.DEFAULT_IDENTITIES)
        self.default_effort: dict[str, str] = dict(self.DEFAULT_EFFORT)
        self.rules: dict[str, str] = {}
        self.limits: dict = copy.deepcopy(DEFAULT_LIMITS)
        self.profiles: dict[str, dict] = copy.deepcopy(DEFAULT_PROFILES)
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data.get("timeout_minutes"), (int, float)):
                self.timeout_minutes = int(data["timeout_minutes"])
            if isinstance(data.get("user_messages_max"), int):
                self.user_messages_max = data["user_messages_max"]
            if isinstance(data.get("identities"), dict):
                self.identities = dict(data["identities"])
            if isinstance(data.get("default_effort"), dict):
                self.default_effort = dict(data["default_effort"])
            if isinstance(data.get("rules"), dict):
                self.rules = dict(data["rules"])
            if isinstance(data.get("limits"), dict):
                lim = copy.deepcopy(DEFAULT_LIMITS)
                for k, v in data["limits"].items():
                    if isinstance(v, dict) and isinstance(lim.get(k), dict):
                        lim[k].update(v)
                    else:
                        lim[k] = v
                self.limits = lim
            if isinstance(data.get("profiles"), dict):
                profiles = copy.deepcopy(DEFAULT_PROFILES)
                for target, prof in data["profiles"].items():
                    if isinstance(prof, dict):
                        profiles.setdefault(target, {}).update(prof)
                self.profiles = profiles
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "timeout_minutes": self.timeout_minutes,
            "user_messages_max": self.user_messages_max,
            "identities": self.identities,
            "default_effort": self.default_effort,
            "rules": self.rules,
            "limits": self.limits,
            "profiles": self.profiles,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get_identity(self, target: str) -> str:
        """A target missing from the map is its own identity."""
        return self.identities.get(target, target)

    def rule_for(self, from_target: str, to_target: str) -> str | None:
        return self.rules.get(f"{from_target} -> {to_target}")

    def set_rule(self, from_target: str, to_target: str, decision: str) -> None:
        self.rules[f"{from_target} -> {to_target}"] = decision
        self.save()

    def remove_rule(self, from_target: str, to_target: str) -> None:
        key = f"{from_target} -> {to_target}"
        if key in self.rules:
            del self.rules[key]
            self.save()

    def update_rules(self, new_rules: dict[str, str]) -> None:
        self.rules = dict(new_rules)
        self.save()

    def set_profile(self, target: str, good_for: str, appetite: str, draws_natively: bool) -> None:
        self.profiles[target] = {
            "good_for": good_for.strip()[:PROFILE_TEXT_MAX],
            "appetite": appetite.strip()[:PROFILE_TEXT_MAX],
            "draws_natively": bool(draws_natively),
        }
        self.save()

    def effort_for(self, target: str, provider) -> str | None:
        effort = self.default_effort.get(target) or self.default_effort.get("*")
        if not effort:
            return None
        model = target.split(":", 1)[1] if ":" in target else ""
        if effort in provider.efforts_for(model):
            return effort
        return None


def target_label(target: str) -> str:
    """Human-readable label for a target '<provider>:<model>'."""
    if ":" not in target:
        return target
    prov_id, model_id = target.split(":", 1)
    prov = PROVIDERS.get(prov_id)
    if not prov:
        return target
    model_info = next((m for m in prov.models if m["id"] == model_id), None)
    model_label = model_info["label"] if model_info else model_id
    return f"{prov.label} ({model_label})"


def identity_label(identity: str) -> str:
    """Human-readable label for a model identity in the chain."""
    if ":" in identity:
        return target_label(identity)
    # Check if identity matches any known model id across providers
    for prov in PROVIDERS.values():
        for m in prov.models:
            if m["id"] == identity:
                return f"{prov.label} ({m['label']})"
    # Predefined known identities:
    if identity.startswith("claude-"):
        name = identity[len("claude-"):].title()
        return f"Claude {name}"
    return identity.replace("-", " ").title()


def limit_state(
    target: str,
    limits_data: dict | None,
    config: HandoffConfig,
    now: float | None = None,
) -> tuple[str, dict | None]:
    """Pure function calculating the limit state for a target.

    Returns (state, detail) where state is "ok" | "scarce" | "hidden".
    detail contains the window numbers that decided the state:
    {"window_id": str, "label": str, "group": str | None, "used": float,
     "resets_at": float | None, "duration": float | None, "ahead": float | None}.
    """
    if now is None:
        now = time.time()

    if not limits_data or ":" not in target:
        return "ok", None

    prov_id, model_id = target.split(":", 1)
    prov_data = limits_data.get(prov_id)
    if not isinstance(prov_data, dict):
        return "ok", None

    raw_windows = prov_data.get("windows")
    if not isinstance(raw_windows, list):
        return "ok", None

    limits_cfg = config.limits if hasattr(config, "limits") else DEFAULT_LIMITS
    ignore_windows = set(limits_cfg.get("ignore_windows") or ["cloud_credits"])
    hide_margin = float(limits_cfg.get("hide_margin", 0.35))
    scarce_margin = float(limits_cfg.get("scarce_margin", 0.15))
    always_ok_below = float(limits_cfg.get("always_ok_below", 0.30))
    custom_durations = limits_cfg.get("durations") or {}
    pools = limits_cfg.get("pools") or {}

    target_pool = pools.get(target) if prov_id == "agy" else None

    applicable_windows = []
    for w in raw_windows:
        if not isinstance(w, dict):
            continue
        if w.get("disabled"):
            continue
        win_id = str(w.get("id") or "")
        if win_id in ignore_windows:
            continue

        if prov_id == "agy":
            group = w.get("group")
            matches_group = (target_pool is not None) and (group == target_pool)
            matches_model = (win_id == model_id)
            if not (matches_group or matches_model):
                continue

        applicable_windows.append(w)

    if not applicable_windows:
        return "ok", None

    SEVERITY = {"hidden": 2, "scarce": 1, "ok": 0}

    worst_severity = 0
    worst_state = "ok"
    worst_detail = None
    worst_ahead = -999.0
    worst_used = -999.0

    for w in applicable_windows:
        win_id = str(w.get("id") or "")
        resets_at = w.get("resets_at")
        if resets_at is not None:
            try:
                resets_at = float(resets_at)
            except (ValueError, TypeError):
                resets_at = None

        utilization = w.get("utilization")
        if resets_at is not None and resets_at <= now:
            used = 0.0
        elif utilization is not None:
            try:
                used = float(utilization)
            except (ValueError, TypeError):
                continue
        else:
            continue

        if isinstance(custom_durations.get(win_id), (int, float)) and custom_durations[win_id] > 0:
            duration = float(custom_durations[win_id])
        elif "five_hour" in win_id or "5h" in win_id:
            duration = 18000.0
        elif "seven_day" in win_id or "weekly" in win_id:
            duration = 604800.0
        else:
            duration = None

        ahead = None
        if used >= 1.0:
            w_state = "hidden"
            if duration is not None and resets_at is not None:
                elapsed = max(0.0, min(1.0, 1.0 - (resets_at - now) / duration))
                ahead = used - elapsed
            else:
                ahead = 1.0
        elif duration is None or resets_at is None or used < always_ok_below:
            w_state = "ok"
        else:
            elapsed = max(0.0, min(1.0, 1.0 - (resets_at - now) / duration))
            ahead = used - elapsed
            if ahead >= hide_margin:
                w_state = "hidden"
            elif ahead >= scarce_margin:
                w_state = "scarce"
            else:
                w_state = "ok"

        w_detail = {
            "window_id": win_id,
            "label": w.get("label") or w.get("group") or win_id,
            "group": w.get("group"),
            "used": used,
            "resets_at": resets_at,
            "duration": duration,
            "ahead": ahead,
        }

        sev = SEVERITY[w_state]
        cur_ahead = ahead if ahead is not None else (used - 1.0)
        if (sev > worst_severity) or (
            sev == worst_severity and (cur_ahead > worst_ahead or (cur_ahead == worst_ahead and used > worst_used))
        ):
            worst_severity = sev
            worst_state = w_state
            worst_detail = w_detail
            worst_ahead = cur_ahead
            worst_used = used

    if worst_state == "ok":
        return "ok", None
    return worst_state, worst_detail


def get_handoff_targets(
    chat,
    config: HandoffConfig,
    limits_data: dict | None = None,
    now: float | None = None,
) -> list[dict]:
    """Return targets this chat may request now.

    Excludes:
    - Chat's own target
    - Targets whose identity is in the chain
    - Targets with a 'deny' rule from this chat's target
    - Targets this chat already got a no for (meta['handoff_refused'])
    - Unavailable or non-project providers, models not in provider.model_ids
    - Targets whose limit state is 'hidden'

    Scarce targets are included with mode='ask' and 'scarce'=True.
    Each item: {"target": str, "label": str, "mode": "auto"|"ask", ...}
    """
    if chat.meta.get("kind") != "project" or chat.meta.get("handed_off_to"):
        return []

    if limits_data is None:
        try:
            from providers import limits
            limits_data = limits.data
        except Exception:
            limits_data = None

    cur_provider = chat.meta.get("provider") or DEFAULT_PROVIDER
    cur_model = chat.meta.get("requested_model") or chat.meta.get("model")
    from_target = f"{cur_provider}:{cur_model}"
    cur_identity = config.get_identity(from_target)

    # Chain = list of identities the task has passed through, oldest first,
    # including current chat's.
    handoff_from = chat.meta.get("handoff_from") or {}
    chain = list(handoff_from.get("chain") or [])
    if not chain:
        chain = [cur_identity]
    elif cur_identity not in chain:
        chain.append(cur_identity)

    refused = set(chat.meta.get("handoff_refused") or [])

    results = []
    for prov in PROVIDERS.values():
        if not prov.available or not prov.supports_project:
            continue
        for m in prov.models:
            model_id = m["id"]
            target = f"{prov.id}:{model_id}"
            if target == from_target:
                continue
            target_ident = config.get_identity(target)
            if target_ident in chain:
                continue
            rule = config.rule_for(from_target, target)
            if rule == "deny":
                continue
            if target in refused:
                continue

            # --- Stage 2 hook point (limits / pace / scarce) ---
            state, detail = limit_state(target, limits_data, config, now=now)
            if state == "hidden":
                continue

            mode = "auto" if rule == "allow" else "ask"
            item: dict = {
                "target": target,
                "label": target_label(target),
                "mode": mode,
            }
            profile = config.profiles.get(target)
            if profile:
                for key in ("good_for", "appetite"):
                    if profile.get(key):
                        item[key] = profile[key]
                item["draws_natively"] = bool(profile.get("draws_natively"))
            if state == "scarce":
                item["mode"] = "ask"
                item["scarce"] = True
                item["note"] = "limits are being spent fast; prefer another target"
                if detail:
                    item["limits_detail"] = detail
            results.append(item)

    return results


def validate_focus_ids(focus: list[str], api_url: str | None, api_token: str | None) -> str:
    """Validate focus IDs against the Orchestrator API; dropped unknown IDs with a note."""
    if not focus:
        return "none"

    lines = []
    for item in focus:
        fid = str(item).strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", fid):
            lines.append(f"- {fid} (dropped: invalid id format)")
            continue
        if not api_url or not api_token:
            lines.append(f"- {fid}")
            continue

        valid = False
        for endpoint in (f"{api_url}/api/nodes/{fid}", f"{api_url}/api/dashboards/{fid}"):
            try:
                req = UrlRequest(
                    endpoint,
                    headers={"Authorization": f"Bearer {api_token}"},
                )
                with urlopen(req, timeout=3) as res:
                    if res.status == 200:
                        valid = True
                        break
            except (URLError, OSError):
                pass
            except Exception:
                pass

        if valid:
            lines.append(f"- {fid}")
        else:
            lines.append(f"- {fid} (dropped: not found)")

    return "\n".join(lines) if lines else "none"


def carry_over(source_chat, config: HandoffConfig) -> dict:
    """What travels down the chain: the person's own messages (verbatim, last N)
    and attachment refs. Inherited from the source's own handoff_from first, so
    after A -> B -> C the receiver still sees what the person wrote to A.
    Only messages without an origin are the person's; "handoff" and "system"
    events are the machinery's own text."""
    inherited = source_chat.meta.get("handoff_from") or {}
    messages = list(inherited.get("person_messages") or [])
    messages += [
        ev["text"] for ev in source_chat.events_after(0)
        if ev.get("type") == "user" and not ev.get("origin") and ev.get("text")
    ]
    attachments = list(inherited.get("attachments") or [])
    if source_chat.attachments_dir.is_dir():
        attachments += [
            {"chat_id": source_chat.id, "name": p.name}
            for p in sorted(source_chat.attachments_dir.iterdir())
            if p.is_file() and not p.is_symlink()
        ]
    n = config.user_messages_max
    return {"person_messages": messages[-n:], "attachments": attachments}


async def build_first_message(
    source_chat,
    target: str,
    task: str,
    reason: str,
    done_so_far: str,
    focus: list[str],
    config: HandoffConfig,
) -> str:
    """Assemble the system-assembled first message for the receiving agent."""
    source_provider = source_chat.meta.get("provider") or DEFAULT_PROVIDER
    source_model = source_chat.meta.get("requested_model") or source_chat.meta.get("model")
    source_target = f"{source_provider}:{source_model}"
    source_lbl = target_label(source_target)

    # Chain identities
    source_identity = config.get_identity(source_target)
    source_chain = list((source_chat.meta.get("handoff_from") or {}).get("chain") or [source_identity])
    target_identity = config.get_identity(target)
    full_chain = list(source_chain)
    if target_identity not in full_chain:
        full_chain.append(target_identity)

    chain_parts = [identity_label(ident) for ident in full_chain[:-1]] + ["you"]
    chain_str = " → ".join(chain_parts)

    banned_identities = [identity_label(ident) for ident in full_chain[:-1]]
    banned_str = ", ".join(banned_identities) if banned_identities else "none"

    project_name = source_chat.meta.get("project_name") or "Unnamed project"
    project_id = source_chat.meta.get("project_id") or "unknown"
    source_chat_id = source_chat.id

    # Blocking HTTP: keep it off the event loop, or every chat stalls with it.
    where_to_work = await asyncio.to_thread(
        validate_focus_ids,
        focus,
        source_chat.meta.get("api_url"),
        source_chat.meta.get("api_token"),
    )

    carried = carry_over(source_chat, config)
    user_msgs_text = "\n\n".join(carried["person_messages"]) or "none"
    if carried["attachments"]:
        att_str = "\n".join(f"- {a['name']} (chat_id=\"{a['chat_id']}\")" for a in carried["attachments"])
        att_str += "\nImport one with import_chat_attachment(chat_id=<its chat_id>, name=..., node_id=...)."
    else:
        att_str = "none"

    done_text = done_so_far.strip() if done_so_far and done_so_far.strip() else "nothing"

    template = f"""[Handoff] This task was handed to you by {source_lbl}. Chain: {chain_str}.
You may not hand it back to: {banned_str}.
Project: {project_name} ({project_id}). Previous chat: {source_chat_id}.
Reply to the person in the language of their messages below.

## Task
{task.strip()}

## Why you
{reason.strip()}

## Already done
{done_text}

## Where to work
{where_to_work}

## The person's own messages (source of truth; if the task above contradicts them, follow these or ask)
{user_msgs_text}

## Attachments from earlier chats
{att_str}

If you are not sure what the person wants, ask one short question instead of guessing."""

    return template
