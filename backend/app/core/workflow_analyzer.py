"""Parses a ComfyUI API-format workflow.json and detects the pieces a node
template wizard needs, so creating a template doesn't require hand-writing
param_schema/param_mapping JSON:

- LoadImage nodes -> candidate input image slots
- SaveImage/PreviewImage nodes -> candidate output slots
- KSampler/KSamplerAdvanced -> seed/steps/cfg/sampler_name/scheduler/denoise,
  plus its positive/negative conditioning inputs traced back to their source
  text-encode node (graph edges, not title-guessing -- more robust). The
  trace isn't assumed to be one hop: model families increasingly thread
  conditioning through modifier nodes first (e.g. Flux's
  FluxKontextMultiReferenceLatentMethod, timestep-range nodes, ControlNet
  applies) before it reaches the actual encoder, so the trace follows
  through any chain of single-link pass-through nodes until it lands on one
  of PROMPT_CLASS_TYPES (or gives up -- see _trace_prompt_node).
- Known custom nodes with directly-useful literal widgets (e.g.
  ResolutionSelector.aspect_ratio/megapixels) -- same idea as the KSampler
  literals above, just keyed by class_type instead of assuming one sampler.
- Any titled Primitive* node (PrimitiveString[Multiline]/Int/Float/Boolean)
  with a literal "value" -- this is how ComfyUI itself represents a
  promoted/exposed widget (incl. subgraph-promoted widgets, which flatten to
  "<subgraph instance>:<inner id>" node ids in the API export). Targeting the
  Primitive node directly -- rather than trying to trace forward through
  whatever switches/concatenations sit between it and its eventual consumer
  -- means detection doesn't need to understand those intermediate nodes at
  all; the graph's own links carry the edited value onward at execution time.
- Known custom nodes whose widget is a single *composite* value -- a dict of
  several scalars (e.g. ImageCropV2's crop_region: {x,y,width,height}) rather
  than one flat literal -- get flattened into one DetectedField per dict key.
  input_key on those fields is a dotted "<input>.<subkey>" path;
  template_engine.build_workflow knows to write those back as a nested
  assignment instead of a flat one.
- A titled switch-style node's own gate (SWITCH_CLASS_TYPES, e.g.
  ComfySwitchNode's "switch") when left as a bare literal true/false rather
  than wired to a separate PrimitiveBoolean -- same "titled literal widget"
  idea as the Primitive* case above, just for a one-off toggle nobody
  bothered breaking out into its own Primitive node.

Field/node "keys" used in the result are ComfyUI node ids from the uploaded
workflow; the caller resolves those to node titles when building param_mapping
(app/core/template_engine.py maps by title, per SPEC's resolution of the
id-vs-title question), so this module also flags duplicate titles among the
detected nodes since that would make title-based mapping ambiguous.
"""
import re
from dataclasses import dataclass, field
from typing import Any

INPUT_IMAGE_CLASS_TYPES = {"LoadImage"}
OUTPUT_CLASS_TYPES = {"SaveImage", "PreviewImage"}
SAMPLER_CLASS_TYPES = {"KSampler", "KSamplerAdvanced"}
# class_type -> the input key holding its literal prompt text (varies by
# node family -- ComfyUI's own CLIPTextEncode uses "text", but e.g. Qwen
# Image Edit's encoder uses "prompt").
PROMPT_CLASS_TYPES: dict[str, str] = {
    "CLIPTextEncode": "text",
    "TextEncodeQwenImageEditPlus": "prompt",
}

SAMPLER_LITERAL_FIELDS = [
    # (input_key_options, field_key, type)
    (("seed", "noise_seed"), "seed", "seed"),
    (("steps",), "steps", "int"),
    (("cfg",), "cfg", "float"),
    (("sampler_name",), "sampler_name", "text"),
    (("scheduler",), "scheduler", "text"),
    (("denoise",), "denoise", "float"),
]

# Other well-known custom nodes whose literal widgets are worth surfacing,
# keyed by class_type. Same shape as SAMPLER_LITERAL_FIELDS, but scanned for
# every matching node in the workflow rather than assuming a single instance.
KNOWN_NODE_LITERAL_FIELDS: dict[str, list[tuple[tuple[str, ...], str, str]]] = {
    "ResolutionSelector": [
        (("aspect_ratio",), "aspect_ratio", "text"),
        (("megapixels",), "megapixels", "float"),
    ],
    # ComfyUI-Easy-Use's standalone seed widget -- common when a workflow
    # wants one seed feeding several samplers, which pulls "seed" off the
    # KSampler itself (turning it into a link SAMPLER_LITERAL_FIELDS can't
    # see) and onto this node instead.
    "easy seed": [(("seed",), "seed", "seed")],
    "EmptySD3LatentImage": [
        (("width",), "width", "int"),
        (("height",), "height", "int"),
    ],
    # SamplerCustomAdvanced-style graphs (e.g. Flux2) source their seed from
    # a standalone RandomNoise node instead of a KSampler widget, so it's
    # invisible to SAMPLER_LITERAL_FIELDS same as "easy seed" above.
    "RandomNoise": [(("noise_seed",), "seed", "seed")],
}

# ComfyUI's own "promoted widget" primitives -> the param_schema field type to expose them as.
PRIMITIVE_CLASS_TYPES = {
    "PrimitiveString": "text",
    "PrimitiveStringMultiline": "text",
    "PrimitiveInt": "int",
    "PrimitiveFloat": "float",
    "PrimitiveBoolean": "bool",
}

# Switch-style nodes' own gate -- keyed by class_type -> the input key
# holding the literal true/false. Left wired to a separate PrimitiveBoolean
# (a link, see _is_link), that upstream node is already the field
# (PRIMITIVE_CLASS_TYPES below); left as a bare literal instead -- no
# PrimitiveBoolean anywhere, just a hardcoded true/false on the switch
# itself, e.g. a one-off toggle nobody bothered breaking out -- it's just as
# much a "promoted widget" as one, so it's detected the same way: by the
# switch node's own title, same as PRIMITIVE_CLASS_TYPES treats a titled
# Primitive node's "value".
SWITCH_CLASS_TYPES: dict[str, str] = {
    "ComfySwitchNode": "switch",
}

# Well-known nodes whose widget bundles several scalars into one dict-valued
# input, keyed by class_type -> [(input_key, field_key_prefix, label_prefix)].
# Each key of the dict becomes its own DetectedField ("<prefix>_<subkey>"),
# same as KNOWN_NODE_LITERAL_FIELDS but for inputs ComfyUI can't promote to a
# Primitive individually since they're one combined widget, not four.
KNOWN_NODE_COMPOSITE_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "ImageCropV2": [("crop_region", "crop", "Crop")],
}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "field"


def _scalar_type(value: Any) -> str | None:
    """bool must be checked before int -- isinstance(True, int) is True in Python."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "text"
    return None


def _trace_prompt_node(workflow_json: dict, node_id: str, depth: int = 0) -> tuple[str, str, Any] | None:
    """Follow a conditioning link forward from node_id until it reaches a
    node in PROMPT_CLASS_TYPES with a literal (non-link) value under that
    class's text key. Nodes in between are assumed to be simple pass-throughs
    (exactly one link-valued input) -- rather than allow-listing every
    possible conditioning-modifier node, just follow whichever single link
    each one has; if a node has zero or several, the chain is too ambiguous
    to auto-follow and detection gives up (same as a prompt-combiner would
    already have, pre-tracing). depth is a sanity bound, not an expected
    real-world case -- ComfyUI graphs are DAGs, so there's no cycle risk.
    """
    if depth > 6:
        return None
    node = workflow_json.get(node_id)
    if not isinstance(node, dict):
        return None
    class_type = node.get("class_type")
    inputs = node.get("inputs", {})
    text_key = PROMPT_CLASS_TYPES.get(class_type)
    if text_key is not None:
        text_value = inputs.get(text_key)
        if text_key in inputs and not _is_link(text_value):
            return node_id, text_key, text_value
        return None
    links = [v for v in inputs.values() if _is_link(v)]
    if len(links) != 1:
        return None
    return _trace_prompt_node(workflow_json, links[0][0], depth + 1)


@dataclass
class WorkflowNodeInfo:
    node_id: str
    class_type: str
    title: str | None


@dataclass
class DetectedField:
    key: str
    label: str
    type: str
    node_id: str
    input_key: str
    default: Any = None


@dataclass
class WorkflowAnalysis:
    input_image_nodes: list[WorkflowNodeInfo] = field(default_factory=list)
    output_nodes: list[WorkflowNodeInfo] = field(default_factory=list)
    detected_fields: list[DetectedField] = field(default_factory=list)
    duplicate_titles: list[str] = field(default_factory=list)


def _node_info(node_id: str, node: dict) -> WorkflowNodeInfo:
    return WorkflowNodeInfo(node_id=node_id, class_type=node.get("class_type", ""), title=(node.get("_meta") or {}).get("title"))


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def analyze_workflow(workflow_json: dict) -> WorkflowAnalysis:
    if not isinstance(workflow_json, dict):
        raise ValueError("workflow must be a JSON object of node_id -> node")

    input_nodes = []
    output_nodes = []
    detected_fields: list[DetectedField] = []

    for node_id, node in workflow_json.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if class_type in INPUT_IMAGE_CLASS_TYPES:
            input_nodes.append(_node_info(node_id, node))
        elif class_type in OUTPUT_CLASS_TYPES:
            output_nodes.append(_node_info(node_id, node))

    # Only the first sampler encountered in dict-iteration order is exposed as
    # fields -- a multi-stage workflow (e.g. a base + refiner pass with two
    # KSamplers) will have its second sampler's seed/steps/cfg invisible to
    # the template wizard. Not handled today; someone uploading such a
    # workflow needs to know only one sampler's params become editable fields.
    sampler_id, sampler_node = next(
        ((nid, n) for nid, n in workflow_json.items() if isinstance(n, dict) and n.get("class_type") in SAMPLER_CLASS_TYPES),
        (None, None),
    )

    if sampler_node is not None:
        inputs = sampler_node.get("inputs", {})

        for input_keys, field_key, field_type in SAMPLER_LITERAL_FIELDS:
            for input_key in input_keys:
                if input_key in inputs and not _is_link(inputs[input_key]):
                    detected_fields.append(
                        DetectedField(
                            key=field_key,
                            label=field_key.replace("_", " ").title(),
                            type=field_type,
                            node_id=sampler_id,
                            input_key=input_key,
                            default=inputs[input_key],
                        )
                    )
                    break

        for input_key, field_key, label in (("positive", "prompt", "Prompt"), ("negative", "negative_prompt", "Negative prompt")):
            link = inputs.get(input_key)
            if not _is_link(link):
                continue
            traced = _trace_prompt_node(workflow_json, link[0])
            if traced is None:
                continue  # too indirect to auto-expose (e.g. a prompt-combiner with 2+ conditioning inputs)
            source_id, text_key, text_value = traced
            detected_fields.append(
                DetectedField(key=field_key, label=label, type="text", node_id=source_id, input_key=text_key, default=text_value)
            )

    used_keys = {f.key for f in detected_fields}

    def _add_field(key: str, label: str, ftype: str, node_id: str, input_key: str, default: Any) -> None:
        unique_key = key if key not in used_keys else f"{key}_{node_id}"
        used_keys.add(unique_key)
        detected_fields.append(DetectedField(key=unique_key, label=label, type=ftype, node_id=node_id, input_key=input_key, default=default))

    for node_id, node in workflow_json.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs", {})

        for input_keys, field_key, field_type in KNOWN_NODE_LITERAL_FIELDS.get(class_type, []):
            for input_key in input_keys:
                if input_key in inputs and not _is_link(inputs[input_key]):
                    _add_field(field_key, field_key.replace("_", " ").title(), field_type, node_id, input_key, inputs[input_key])
                    break

        for input_key, field_key_prefix, label_prefix in KNOWN_NODE_COMPOSITE_FIELDS.get(class_type, []):
            composite = inputs.get(input_key)
            if not isinstance(composite, dict):
                continue
            for subkey in sorted(composite):
                subvalue = composite[subkey]
                subtype = _scalar_type(subvalue)
                if subtype is None:
                    continue  # not a plain scalar (e.g. itself a link) -- skip
                _add_field(
                    f"{field_key_prefix}_{subkey}",
                    f"{label_prefix} {subkey.replace('_', ' ').title()}",
                    subtype,
                    node_id,
                    f"{input_key}.{subkey}",
                    subvalue,
                )

        if class_type in PRIMITIVE_CLASS_TYPES:
            title = (node.get("_meta") or {}).get("title")
            value = inputs.get("value")
            if title and "value" in inputs and not _is_link(value):
                _add_field(_slugify(title), title, PRIMITIVE_CLASS_TYPES[class_type], node_id, "value", value)

        switch_key = SWITCH_CLASS_TYPES.get(class_type)
        if switch_key is not None:
            title = (node.get("_meta") or {}).get("title")
            switch_value = inputs.get(switch_key)
            if title and switch_key in inputs and not _is_link(switch_value):
                _add_field(_slugify(title), title, "bool", node_id, switch_key, switch_value)

    # Dedupe by node id before counting -- a single node (e.g. the one KSampler)
    # legitimately backs several detected_fields, and that must not look like
    # several *different* nodes sharing a title.
    title_to_node_ids: dict[str, set[str]] = {}

    def _record_title(node_id: str | None, title: str | None) -> None:
        if node_id and title:
            title_to_node_ids.setdefault(title, set()).add(node_id)

    for info in [*input_nodes, *output_nodes]:
        _record_title(info.node_id, info.title)
    if sampler_node is not None:
        _record_title(sampler_id, (sampler_node.get("_meta") or {}).get("title"))
    for detected in detected_fields:
        node = workflow_json.get(detected.node_id, {})
        _record_title(detected.node_id, (node.get("_meta") or {}).get("title"))

    duplicate_titles = sorted(t for t, node_ids in title_to_node_ids.items() if len(node_ids) > 1)

    return WorkflowAnalysis(
        input_image_nodes=input_nodes,
        output_nodes=output_nodes,
        detected_fields=detected_fields,
        duplicate_titles=duplicate_titles,
    )
