"""Which model family a node type runs on -- the sub-group the node-type
picker offers it under by default.

A node type's own name says what it does ("Copy Pose"), never what it runs on,
and the list has grown past the point where scanning it is quick. The one fact
that actually clusters those names is the model behind them, which the app
already reads out of each capability for the description fingerprint
(core/node_fingerprint.py's capability_models) -- so the grouping is derived
from that rather than being a second thing to keep in sync by hand.

Derived, not stored: adding a ComfyUI instance that loads a different
checkpoint re-groups the type on the next load. `NodeTemplate.category_override`
is there for the cases where the file name is a bad label (or where a type
belongs with a group it doesn't technically share a model with).
"""
import re

from app.core.node_fingerprint import capability_models
from app.db.models import Capability, ExecutionType

# Where stripping the version digits off the first token gets the label wrong
# or just reads badly. Everything else falls through to the generic rule
# below, which already handles every model name in use here:
# qwen_image_edit_2511_int8_convrot -> Qwen, flux2_dev_fp8mixed -> Flux,
# flux-2-klein-9b-fp8 -> Flux, krea2_turbo_fp8_scaled -> Krea,
# ideogram4_fp8_scaled -> Ideogram, gemini-3-pro-image-preview -> Gemini.
_FAMILY_NAMES = {
    "sd": "SD",
    "sd15": "SD 1.5",
    "sd3": "SD 3",
    "sdxl": "SDXL",
    "wan": "Wan",
    "hidream": "HiDream",
    "dalle": "DALL·E",
    "gpt": "GPT",
}

_WEIGHT_SUFFIX = re.compile(r"\.(safetensors|sft|ckpt|pt|pth|bin|gguf)$", re.IGNORECASE)


def model_family(model: str) -> str:
    """"flux2_dev_fp8mixed.safetensors" -> "Flux". Everything after the first
    word is quantisation/variant noise that would split one family into a
    dozen one-entry groups, so only the leading word survives, minus its
    version number."""
    stem = _WEIGHT_SUFFIX.sub("", model.replace("\\", "/").rsplit("/", 1)[-1])
    parts = [part.lower() for part in re.split(r"[^A-Za-z0-9]+", stem) if part]
    token = parts[0] if parts else ""
    if not token:
        return ""
    # Only for names the table already knows split across two words, e.g.
    # "sd_xl_base_1.0" -- checked before the single-token lookup so it wins
    # over the plainer "sd". An unknown pair falls straight through.
    if len(parts) > 1 and (token + parts[1]) in _FAMILY_NAMES:
        return _FAMILY_NAMES[token + parts[1]]
    if token in _FAMILY_NAMES:
        return _FAMILY_NAMES[token]
    base = token.rstrip("0123456789") or token
    return _FAMILY_NAMES.get(base, base.capitalize())


def derive_category(models) -> str:
    """The category for one node type, given every model its capabilities
    load. Families rather than raw names is what makes this collapse instead
    of fragment -- a workflow that loads flux2_dev *and* flux-2-klein, or two
    backends running different quantisations of the same checkpoint, is one
    group. A type that genuinely spans two families is named after both
    rather than being silently filed under whichever came first."""
    families = sorted({family for family in (model_family(m) for m in models) if family})
    return " + ".join(families)


def category_for_capabilities(capabilities: list[Capability]) -> str:
    """The derived category for one node type.

    A paid API instance only gets a say when nothing else does. A node type
    usually reaches an API as an *alternative* route to the same result the
    local graph produces, so counting both put "CreateImage" under
    "Gemini + Krea" -- naming a provider the node only sometimes goes to,
    in the place meant to say what it runs on. The local checkpoint is the
    honest answer whenever there is one; an API-only type still gets named
    after its model rather than dropping into the uncategorized bucket.
    """
    local = [model for c in capabilities if c.execution_type != ExecutionType.api_call for model in capability_models(c)]
    return derive_category(local or [model for c in capabilities for model in capability_models(c)])
