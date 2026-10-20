"""Per-PC model overrides: run the same workflow with a lighter/heavier model
file depending on which ComfyUI instance it lands on.

`Backend.model_overrides` is a flat {workflow's file name: this PC's file name}.
It is applied to the loader inputs listed in OVERRIDABLE_INPUT_KEYS -- model
(UNET/checkpoint), CLIP and VAE, deliberately NOT LoRA (a LoRA follows the
model's family/variant, not its size, so it stays as the workflow has it).

A swap is only legal between files of the same *family and variant*; size
(4b vs 9b) and quantization (fp8/int8/bf16...) are the free axes. ComfyUI only
knows file names, so family/variant/size are read off the name
(`flux-2-klein-base-9b-fp8` -> family flux-2-klein, variant base, size 9b).
The variant is the one that matters: a base model wants 20-40 steps and a
distilled one ~4, and since this never touches steps/CFG/sampler, crossing it
would silently produce garbage -- so it is refused, not warned about.
"""
import re
from dataclasses import asdict, dataclass
from typing import Any

# Loader input keys we may rewrite. lora_name is intentionally absent.
OVERRIDABLE_INPUT_KEYS = ("unet_name", "ckpt_name", "clip_name", "vae_name")

_EXTENSIONS = (".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin")
_SIZE = re.compile(r"^\d+(?:\.\d+)?b$")
_QUANT = re.compile(
    r"^(?:(?:fp|bf|int|nvfp)\d+\w*|nf4|q\d.*|scaled|mixed|awq|pruned|convrot|e\dm\d.*|fast|gguf|upscale\d*x?)$"
)
_DISTILLED = {"turbo", "distilled", "distill", "lightning", "schnell", "lcm", "hyper"}


@dataclass(frozen=True)
class ModelInfo:
    name: str
    family: str
    variant: str  # "base" | "distilled" | "default"
    size: str | None
    quant: str


def parse_model_name(name: str) -> ModelInfo:
    stem = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for ext in _EXTENSIONS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    # "2.1" / "3.1" are version numbers, keep them as one token; only split on - and _
    tokens = [t for t in re.split(r"[-_]+", stem.lower()) if t]
    variant = "default"
    size: str | None = None
    quant: list[str] = []
    family: list[str] = []
    for tok in tokens:
        if tok == "base":
            variant = "base"
        elif tok in _DISTILLED:
            variant = "distilled"
        elif _SIZE.match(tok):
            size = tok
        elif _QUANT.match(tok):
            quant.append(tok)
        else:
            family.append(tok)
    return ModelInfo(name=name, family="-".join(family), variant=variant, size=size, quant="-".join(quant))


def swap_problem(source: str, target: str) -> str | None:
    """Why `source` may not be replaced by `target`, or None if it may."""
    a, b = parse_model_name(source), parse_model_name(target)
    if a.family != b.family:
        return f"different model family ({a.family} vs {b.family})"
    if a.variant != b.variant:
        return f"different variant ({a.variant} vs {b.variant}): base and distilled need different step counts"
    return None


def validate_overrides(overrides: dict[str, str]) -> None:
    """Raises ValueError naming the first illegal pair."""
    for source, target in overrides.items():
        if problem := swap_problem(source, target):
            raise ValueError(f"{source} -> {target}: {problem}")


def apply_model_overrides(workflow: dict[str, Any], overrides: dict[str, str]) -> None:
    """Rewrites `workflow` (a built prompt, already a private copy) in place.
    An illegal pair raises rather than being skipped: running the original
    heavy model on a machine configured to avoid it would just OOM."""
    if not overrides:
        return
    validate_overrides(overrides)
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key in OVERRIDABLE_INPUT_KEYS:
            value = inputs.get(key)
            if isinstance(value, str) and value in overrides:
                inputs[key] = overrides[value]


def workflow_model_files(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    """(input key, file name) for every overridable loader input in a workflow
    graph, de-duplicated -- i.e. what this workflow could have swapped."""
    found: dict[tuple[str, str], None] = {}
    for node in workflow.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if isinstance(inputs, dict):
            for key in OVERRIDABLE_INPUT_KEYS:
                if isinstance(inputs.get(key), str):
                    found[(key, inputs[key])] = None
    return list(found)


def describe_models(names: list[str]) -> list[dict]:
    return [asdict(parse_model_name(n)) for n in names]
