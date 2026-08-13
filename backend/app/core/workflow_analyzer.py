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
  of PROMPT_CLASS_TYPES (or gives up -- see _trace_prompt_node). A titled
  switch (SWITCH_BRANCH_INPUT_KEYS, e.g. ComfySwitchNode picking between
  "use reference image or not") has two data-valued branches instead of one
  link, so the trace tries each branch in turn rather than giving up on the
  link-count check.
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

Everything above is derived from the uploaded file alone. One thing can't be:
whether a detected widget is a dropdown and what its choices are, since a
workflow.json stores only the chosen value. apply_combo_options() at the bottom
folds that in from a live instance's /object_info when the caller has one.

Field/node "keys" used in the result are ComfyUI node ids from the uploaded
workflow; the caller resolves those to node titles when building param_mapping
(app/core/template_engine.py maps by title, the settled resolution of the
id-vs-title question), so this module also flags duplicate titles among the
detected nodes since that would make title-based mapping ambiguous.
"""
import re
from dataclasses import dataclass, field
from typing import Any

INPUT_IMAGE_CLASS_TYPES = {"LoadImage"}
OUTPUT_CLASS_TYPES = {"SaveImage", "PreviewImage"}
SAMPLER_CLASS_TYPES = {"KSampler", "KSamplerAdvanced", "SamplerCustomAdvanced"}
# SamplerCustomAdvanced-style graphs (Flux2, Ideogram 4) carry no
# positive/negative of their own -- conditioning reaches the sampler through a
# guider node instead, so the prompt trace has to hop through it or it finds
# nothing at all. class_type -> the (positive, negative) input keys on that
# guider; BasicGuider has a single unnamed conditioning and no negative.
GUIDER_CONDITIONING_KEYS: dict[str, tuple[str, str | None]] = {
    "CFGGuider": ("positive", "negative"),
    "DualModelGuider": ("positive", "negative"),
    "BasicGuider": ("conditioning", None),
}
# Checkpoint/diffusion-model loaders -> the input key naming the model file.
# Used only for describing a workflow ("what does this node type actually
# run"), never for execution.
MODEL_LOADER_INPUT_KEYS: dict[str, str] = {
    "CheckpointLoaderSimple": "ckpt_name",
    "CheckpointLoader": "ckpt_name",
    "UNETLoader": "unet_name",
    "DiffusionModelLoader": "unet_name",
}
# LoRA loaders -> (name key, strength key). LoraLoaderModelOnly has no CLIP
# strength, hence the separate entry rather than one shared shape.
LORA_LOADER_INPUT_KEYS: dict[str, tuple[str, str]] = {
    "LoraLoader": ("lora_name", "strength_model"),
    "LoraLoaderModelOnly": ("lora_name", "strength_model"),
}
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
    # Same idea as ResolutionSelector: the latent size is computed from these
    # rather than typed as width/height, so without them the output format is
    # frozen into the capability's baked workflow_json and changing a poster
    # from 4:5 to 1:1 means re-running the wizard. Both are "text" because
    # ComfyUI stores them as combo strings ("4:5 (Artistic Frame)", "2.5") --
    # writing a float back into megapixel would not match any combo option.
    # The frontend also reads aspect_ratio to shape the Ideogram caption
    # editor's canvas (parseAspectRatio in ideogram4.ts).
    "FluxResolutionNode": [
        (("aspect_ratio",), "aspect_ratio", "text"),
        (("megapixel",), "megapixel", "text"),
    ],
    # ComfyUI-Easy-Use's standalone seed widget -- common when a workflow
    # wants one seed feeding several samplers, which pulls "seed" off the
    # KSampler itself (turning it into a link SAMPLER_LITERAL_FIELDS can't
    # see) and onto this node instead.
    "easy seed": [(("seed",), "seed", "seed")],
    # rgthree's equivalent, and the same reasoning: with the seed living on a
    # separate node, RandomNoise/KSampler sees only a link and the schema ends
    # up with no seed field at all -- which means the backend has nothing to
    # randomize per variant and every variant of a batch comes back identical.
    "Seed (rgthree)": [(("seed",), "seed", "seed")],
    "EmptySD3LatentImage": [
        (("width",), "width", "int"),
        (("height",), "height", "int"),
    ],
    # SamplerCustomAdvanced-style graphs (e.g. Flux2) source their seed from
    # a standalone RandomNoise node instead of a KSampler widget, so it's
    # invisible to SAMPLER_LITERAL_FIELDS same as "easy seed" above.
    "RandomNoise": [(("noise_seed",), "seed", "seed")],
    # SAM3's text-grounded segmentation node -- a mask-extraction graph has no
    # sampler at all, so without this entry every one of its widgets is
    # invisible to the wizard (SAMPLER_LITERAL_FIELDS only looks at
    # KSampler-family nodes) and it detects nothing to map.
    "SAM3Grounding": [
        (("text_prompt",), "text_prompt", "text"),
        (("max_detections",), "max_detections", "int"),
        (("confidence_threshold",), "confidence_threshold", "float"),
    ],
    # ComfyUI_essentials' batch-slice node -- which detection this instance
    # picks out of SAM3Grounding's (possibly multi-detection) mask batch.
    "MaskFromBatch+": [
        (("start",), "start", "int"),
        (("length",), "length", "int"),
    ],
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

# The same switch nodes' *data* branches -- keyed by class_type -> (on_true
# input key, on_false input key). Used only by _trace_prompt_node: a switch
# has two link-valued data inputs plus its gate, so it fails the generic
# "exactly one link" pass-through test even though it very much is one for
# tracing purposes (both branches commonly converge on the same encoder, as
# in a "use reference image or not" toggle around one shared prompt).
SWITCH_BRANCH_INPUT_KEYS: dict[str, tuple[str, str]] = {
    "ComfySwitchNode": ("on_true", "on_false"),
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

    # A node named "conditioning" for the one input that actually carries the
    # chain (ReferenceLatent, ControlNetApply, ...) can have other, unrelated
    # link inputs alongside it -- ReferenceLatent's own "latent" -- that would
    # otherwise fail the generic "exactly one link" test below on a node that
    # was perfectly traceable (2026-08-13: a Flux2/Klein "Set Reference
    # Latent" node -- {conditioning, latent}, two links -- silently broke
    # prompt detection on every workflow built around it, so "prompt" simply
    # never showed up as a detectable field at all). ComfyUI's own conditioning
    # sockets are conventionally named exactly this, so this is a naming
    # convention to trust, not a per-class_type allow-list to maintain.
    conditioning_link = inputs.get("conditioning")
    if _is_link(conditioning_link):
        return _trace_prompt_node(workflow_json, conditioning_link[0], depth + 1)

    links = [v for v in inputs.values() if _is_link(v)]
    if len(links) != 1:
        branch_keys = SWITCH_BRANCH_INPUT_KEYS.get(class_type)
        if branch_keys is not None:
            for branch_key in branch_keys:
                branch_link = inputs.get(branch_key)
                if _is_link(branch_link):
                    traced = _trace_prompt_node(workflow_json, branch_link[0], depth + 1)
                    if traced is not None:
                        return traced
        return None
    return _trace_prompt_node(workflow_json, links[0][0], depth + 1)


@dataclass
class WorkflowNodeInfo:
    node_id: str
    class_type: str
    title: str | None
    # Same value space as AssetKind (db/models.py) -- "mask" for an
    # input_image_nodes entry (a LoadImage) whose output feeds an
    # ImageToMask node, None for a plain picture. Deliberately a kind string,
    # not a mask-only bool: the point isn't "detect masks specially", it's
    # "detect what an input slot actually is, generically" -- the day a mesh
    # (or anything else with its own ComfyUI loader) gets the same
    # treatment, it's another value here, not a second parallel flag. Set by
    # analyze_workflow, never guessed from the node's title -- a wizard that
    # zips input_image_nodes to user-declared slots purely by array position
    # (NodeTypeWizard.tsx) has no other way to tell "Load Mask" and
    # "Load Image" apart and can pair them backwards (2026-08-13 incident: a
    # 2-LoadImage workflow got its image/mask slots swapped because the
    # graph's own node order didn't match the order the slots were typed in).
    likely_kind: str | None = None


@dataclass
class DetectedField:
    key: str
    label: str
    type: str
    node_id: str
    input_key: str
    default: Any = None
    # Filled in by apply_combo_options() when the backend says this input is a
    # combo widget; None for a free-form one. type becomes "enum" alongside it.
    options: list[str] | None = None
    # True when this same (node_id, input_key) is also promoted to a
    # param_schema field (settable per node instance via Node.params) --
    # purely informational, shown so an editor here knows a cell that already
    # has its own value stored won't be affected. Does NOT change how the
    # field is edited: update_capability_text_field writes this literal
    # either way, since that's what a never-touched instance actually reads
    # at generation time (build_workflow only overrides it once Node.params
    # carries the key -- see CLAUDE.md's MCP section). A previous design had
    # a second write path here (update_variable_default, writing
    # NodeTemplate.param_schema's own decorative `default` instead) that
    # never actually affected generation; removed 2026-08-09 rather than
    # wired up, in favor of this one editor for both cases.
    is_variable: bool = False


@dataclass
class LoraInfo:
    name: str
    strength: float | None = None


@dataclass
class WorkflowAnalysis:
    input_image_nodes: list[WorkflowNodeInfo] = field(default_factory=list)
    output_nodes: list[WorkflowNodeInfo] = field(default_factory=list)
    detected_fields: list[DetectedField] = field(default_factory=list)
    duplicate_titles: list[str] = field(default_factory=list)
    # Descriptive only (node-type fingerprinting, see core/node_fingerprint.py)
    # -- nothing in execution reads these.
    models: list[str] = field(default_factory=list)
    loras: list[LoraInfo] = field(default_factory=list)


def _node_info(node_id: str, node: dict) -> WorkflowNodeInfo:
    return WorkflowNodeInfo(node_id=node_id, class_type=node.get("class_type", ""), title=(node.get("_meta") or {}).get("title"))


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)


def find_editable_text_fields(workflow_json: dict, param_mapping: dict) -> list[DetectedField]:
    """Literal prompt-shaped text values baked directly into an *existing*
    capability's workflow_json -- a CLIPTextEncode/TextEncodeQwenImageEditPlus
    node's text, or a titled PrimitiveString(Multiline) node's value.
    Reuses the same class_type detection analyze_workflow uses at wizard
    time, minus the sampler-link-tracing: this walks every matching node
    directly (wired into a sampler or not), against a workflow_json that's
    already live on a capability rather than a fresh upload.

    Includes a field even when it's already promoted to a param_schema
    variable via param_mapping (DetectedField.is_variable is set instead of
    excluding it) -- editing the literal here is still the one thing that
    changes what a never-touched instance of that field generates; see
    DetectedField.is_variable's own docstring for why."""
    mapped = {(target.get("node_id"), target.get("input_key")) for target in param_mapping.values()}
    fields: list[DetectedField] = []
    for node_id, node in workflow_json.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        text_key = PROMPT_CLASS_TYPES.get(class_type)
        if text_key is None and PRIMITIVE_CLASS_TYPES.get(class_type) == "text":
            text_key = "value"
        if text_key is None:
            continue

        inputs = node.get("inputs", {})
        value = inputs.get(text_key)
        if text_key not in inputs or _is_link(value):
            continue

        title = (node.get("_meta") or {}).get("title") or class_type or node_id
        fields.append(
            DetectedField(
                key=_slugify(f"{title}_{node_id}"),
                label=title,
                type="text",
                node_id=node_id,
                input_key=text_key,
                default=value,
                is_variable=(node_id, text_key) in mapped,
            )
        )
    return fields


def analyze_workflow(workflow_json: dict) -> WorkflowAnalysis:
    if not isinstance(workflow_json, dict):
        raise ValueError("workflow must be a JSON object of node_id -> node")

    input_nodes = []
    output_nodes = []
    detected_fields: list[DetectedField] = []
    models: list[str] = []
    loras: list[LoraInfo] = []

    # One pass over the graph for everything positional -- models/LoRAs are
    # collected here rather than in a second walk of their own.
    for node_id, node in workflow_json.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if class_type in INPUT_IMAGE_CLASS_TYPES:
            input_nodes.append(_node_info(node_id, node))
        elif class_type in OUTPUT_CLASS_TYPES:
            output_nodes.append(_node_info(node_id, node))

        inputs = node.get("inputs", {})
        model_key = MODEL_LOADER_INPUT_KEYS.get(class_type)
        if model_key and not _is_link(inputs.get(model_key)):
            value = inputs.get(model_key)
            if isinstance(value, str) and value not in models:
                models.append(value)
        lora_keys = LORA_LOADER_INPUT_KEYS.get(class_type)
        if lora_keys:
            name_key, strength_key = lora_keys
            name = inputs.get(name_key)
            if isinstance(name, str) and not any(l.name == name for l in loras):
                strength = inputs.get(strength_key)
                loras.append(LoraInfo(name=name, strength=strength if isinstance(strength, (int, float)) else None))

    # Second, short pass: which of input_nodes actually feeds an ImageToMask
    # -- can't be folded into the loop above since an ImageToMask can appear
    # either before or after the LoadImage it reads from in dict-iteration
    # order, so the full node set has to be known first.
    mask_source_ids = {
        source[0]
        for node in workflow_json.values()
        if isinstance(node, dict) and node.get("class_type") == "ImageToMask"
        for source in [node.get("inputs", {}).get("image")]
        if isinstance(source, list) and len(source) == 2 and isinstance(source[0], str)
    }
    for info in input_nodes:
        if info.node_id in mask_source_ids:
            info.likely_kind = "mask"

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

        # Where the conditioning links actually hang: on the sampler itself for
        # a KSampler, on its guider for SamplerCustomAdvanced.
        cond_inputs = inputs
        positive_key: str | None = "positive"
        negative_key: str | None = "negative"
        guider_link = inputs.get("guider")
        if "positive" not in inputs and _is_link(guider_link):
            guider = workflow_json.get(guider_link[0])
            if isinstance(guider, dict):
                keys = GUIDER_CONDITIONING_KEYS.get(guider.get("class_type"))
                if keys is not None:
                    cond_inputs = guider.get("inputs", {})
                    positive_key, negative_key = keys

        traced_positive: tuple[str, str, Any] | None = None
        for input_key, field_key, label in ((positive_key, "prompt", "Prompt"), (negative_key, "negative_prompt", "Negative prompt")):
            if input_key is None:
                continue
            link = cond_inputs.get(input_key)
            if not _is_link(link):
                continue
            traced = _trace_prompt_node(workflow_json, link[0])
            if traced is None:
                continue  # too indirect to auto-expose (e.g. a prompt-combiner with 2+ conditioning inputs)
            source_id, text_key, text_value = traced
            if field_key == "prompt":
                traced_positive = traced
            elif traced_positive is not None and (source_id, text_key) == traced_positive[:2]:
                # The negative is derived from the positive rather than being a
                # prompt of its own -- ConditioningZeroOut(positive) is the
                # standard way to say "no negative" in a Flux2/Ideogram graph,
                # and it traces back to the very same text encode. Exposing it
                # would put two fields on one input, where editing the
                # "negative" silently overwrites the actual prompt.
                continue
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
        models=models,
        loras=loras,
    )


def _combo_options(entry: dict, input_key: str) -> list[str] | None:
    """The option list of a ComfyUI combo widget, or None if that input isn't
    a combo. In an /object_info payload every input is `[spec, {...opts}]`,
    where spec is a type name ("INT", "STRING") for a scalar widget and the
    literal list of choices for a combo -- so "is this a dropdown" is exactly
    "is the spec a list". Non-string choices (a combo of ints) are skipped:
    param_schema's enum fields are string-valued, and writing an int back as a
    string would not match any option on the ComfyUI side."""
    for section in ("required", "optional"):
        spec = (entry.get("input", {}).get(section) or {}).get(input_key)
        if not isinstance(spec, list) or not spec:
            continue
        choices = spec[0]
        if isinstance(choices, list) and choices and all(isinstance(c, str) for c in choices):
            return list(choices)
    return None


def apply_combo_options(analysis: WorkflowAnalysis, workflow_json: dict, object_info: dict[str, dict]) -> None:
    """Upgrades detected fields whose ComfyUI widget is a combo from a free
    text box to an enum with the real option list, in place.

    A workflow.json records only the value that was chosen, so the list can
    only come from a live instance (see ComfyUIBackend.fetch_object_info) --
    and specifically from the instance this capability is being created for,
    since a custom node may be installed on one backend and not another.
    Anything not answered for stays exactly as detected, so an unreachable or
    differently-equipped backend degrades to today's behaviour rather than
    losing the field.

    The current value is kept as an option even when the backend doesn't list
    it: a workflow exported from an older version of a custom node can name a
    choice that has since been renamed, and silently dropping it would rewrite
    the user's setting on the next save.
    """
    for detected in analysis.detected_fields:
        node = workflow_json.get(detected.node_id)
        if not isinstance(node, dict):
            continue
        entry = object_info.get(node.get("class_type"))
        if not isinstance(entry, dict):
            continue
        options = _combo_options(entry, detected.input_key)
        if not options:
            continue
        if isinstance(detected.default, str) and detected.default not in options:
            options = [detected.default, *options]
        detected.type = "enum"
        detected.options = options
