"""Auto-derived descriptions of node types.

A node type's own name is often all a human bothered to write, so a description
has to be built from what its workflows actually *do*: which model and LoRAs
they load, how many images they take in, what the prompts say.

A node type can be configured differently on different machines (the same
"createimage" may run one graph on one GPU box and another elsewhere, or reach
a paid API instead). The fingerprint therefore merges its capabilities
attribute by attribute: where every backend agrees, one value is shown; where
they differ, each backend's value is named. That difference is exactly what
someone -- or an agent -- needs to see before trusting "the same node" to
behave the same everywhere.
"""
import hashlib
import json

from app.core.workflow_analyzer import analyze_workflow
from app.db.models import Capability, ExecutionType


def _api_model(config: dict) -> str | None:
    """The model an api_call capability is bound to. `model_id` is the key
    dispatcher.py actually passes to build_api_backend; `model`/`provider` are
    only read as fallbacks for older/hand-written configs."""
    value = config.get("model") or config.get("model_id") or config.get("provider")
    return value if isinstance(value, str) and value else None


def capability_models(capability: Capability) -> list[str]:
    """Every model this capability loads, by raw name -- the ComfyUI graph's
    own loaders, or the API instance's model id.

    Shared with core/node_category.py so the picker's grouping and the
    description's "model:" line can't end up stating different things.
    """
    config = capability.config or {}
    workflow = config.get("workflow_json")
    if isinstance(workflow, dict) and workflow:
        try:
            return analyze_workflow(workflow).models
        except ValueError:
            return []
    api_model = _api_model(config)
    return [api_model] if api_model else []


def _capability_attributes(capability: Capability) -> dict[str, str]:
    """Reduce one capability to a flat name -> readable value map."""
    config = capability.config or {}
    execution = capability.execution_type
    execution = execution.value if isinstance(execution, ExecutionType) else str(execution)
    attrs: dict[str, str] = {"runs": execution}

    workflow = config.get("workflow_json")
    if isinstance(workflow, dict) and workflow:
        try:
            analysis = analyze_workflow(workflow)
        except ValueError:
            return attrs
        if analysis.models:
            attrs["model"] = ", ".join(analysis.models)
        if analysis.loras:
            attrs["lora"] = ", ".join(
                l.name if l.strength is None else f"{l.name}@{l.strength:g}" for l in analysis.loras
            )
        attrs["image inputs"] = str(len(analysis.input_image_nodes))
        # By field key, not by type: sampler_name and scheduler are "text"
        # fields too, and matching on type put "euler" in here as the prompt.
        prompts = [
            f.default
            for f in analysis.detected_fields
            if f.key == "prompt" and isinstance(f.default, str) and f.default.strip()
        ]
        if prompts:
            attrs["prompt"] = _shorten(prompts[0])
        sampler = {f.key: f.default for f in analysis.detected_fields if f.key in ("steps", "cfg", "sampler_name")}
        if sampler:
            attrs["sampler"] = ", ".join(f"{k}={v}" for k, v in sorted(sampler.items()))
    elif execution == "api_call":
        # No graph to read. A baked, non-parameter prompt describes the node
        # directly; otherwise the exposed parameter names are the only hint
        # there is, and they make a weak guess rather than a description.
        baked = config.get("prompt")
        if isinstance(baked, str) and baked.strip():
            attrs["prompt"] = _shorten(baked)
        else:
            mapping = config.get("param_mapping") or {}
            if mapping:
                attrs["api params"] = ", ".join(sorted(mapping))
        api_model = _api_model(config)
        if api_model:
            attrs["model"] = api_model
    return attrs


def _shorten(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def build_fingerprint(capabilities: list[Capability], backend_names: dict) -> dict[str, str]:
    """Merge capabilities into one attribute -> value map.

    Identical across every backend -> the plain value. Differing -> each
    backend's own value, named, so the divergence is visible at a glance.
    """
    per_backend: list[tuple[str, dict[str, str]]] = []
    for capability in capabilities:
        name = backend_names.get(capability.backend_id) or str(capability.backend_id)[:8]
        per_backend.append((name, _capability_attributes(capability)))

    if not per_backend:
        return {}

    merged: dict[str, str] = {}
    for key in sorted({k for _, attrs in per_backend for k in attrs}):
        values = {name: attrs[key] for name, attrs in per_backend if key in attrs}
        distinct = set(values.values())
        if len(distinct) == 1:
            # Everyone that has this attribute agrees on it, so show it once.
            # Naming each backend when the values are identical is noise, and
            # it buried the cases where they genuinely differ. An attribute
            # only some backends have (an API binding has no LoRA) still reads
            # as agreement -- that it's absent elsewhere is already visible
            # from "runs".
            merged[key] = next(iter(distinct))
        else:
            merged[key] = " | ".join(f"{name}: {value}" for name, value in sorted(values.items()))
    return merged


def describe_fingerprint(name: str, fingerprint: dict[str, str]) -> str:
    """Render a fingerprint as the one-line description shown when nobody has
    written a better one."""
    if not fingerprint:
        return name
    parts = [f"{key}: {value}" for key, value in fingerprint.items()]
    return f"{name} — " + "; ".join(parts)


def compute_config_hash(capabilities: list[Capability]) -> str:
    """Identity of a node type's whole configuration.

    A cached description is only trustworthy while the thing it described is
    unchanged; comparing this hash on read is what catches a workflow being
    re-uploaded or a prompt edited underneath a stale description.
    """
    payload = sorted(
        (str(c.backend_id), c.execution_type.value if isinstance(c.execution_type, ExecutionType) else str(c.execution_type), json.dumps(c.config or {}, sort_keys=True, default=str))
        for c in capabilities
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
