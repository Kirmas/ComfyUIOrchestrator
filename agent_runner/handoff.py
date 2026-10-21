"""Agent handoff core logic: config, filtering, and first-message assembly.

Stage 1 implementation:
- One-way task handoff between project chats.
- Config in DATA_DIR / "handoff.json".
- Single filtering function for target models.
- First message assembly for the receiving agent.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request as UrlRequest, urlopen

from providers import DATA_DIR, DEFAULT_PROVIDER, PROVIDERS


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

    def __init__(self, path: Path | None = None):
        self.path = path or (DATA_DIR / "handoff.json")
        self.timeout_minutes = self.DEFAULT_TIMEOUT_MINUTES
        self.user_messages_max = self.DEFAULT_USER_MESSAGES_MAX
        self.identities: dict[str, str] = dict(self.DEFAULT_IDENTITIES)
        self.default_effort: dict[str, str] = dict(self.DEFAULT_EFFORT)
        self.rules: dict[str, str] = {}
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


def get_handoff_targets(chat, config: HandoffConfig) -> list[dict]:
    """Return targets this chat may request now.

    Excludes:
    - Chat's own target
    - Targets whose identity is in the chain
    - Targets with a 'deny' rule from this chat's target
    - Targets this chat already got a no for (meta['handoff_refused'])
    - Unavailable or non-project providers, models not in provider.model_ids

    Each item: {"target": str, "label": str, "mode": "auto"|"ask"}
    """
    if chat.meta.get("kind") != "project" or chat.meta.get("handed_off_to"):
        return []

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
            # Stage 2 will insert limit pool checks and scarce mode filtering here.

            mode = "auto" if rule == "allow" else "ask"
            results.append({
                "target": target,
                "label": target_label(target),
                "mode": mode,
            })

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
