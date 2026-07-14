"""Generic template engine: validates Node.params against a NodeTemplate.param_schema
and maps resolved field values into a ComfyUI API-format workflow_json.

param_schema shape (JSON-Schema-ish, SPEC section 3):
{
  "fields": [
    {"name": "prompt", "type": "text", "required": true},
    {"name": "seed", "type": "seed", "default": 0},
    {"name": "steps", "type": "int", "default": 20, "min": 1, "max": 150},
    {"name": "image", "type": "image", "required": true},
    ...
  ]
}

param_mapping shape (SPEC section 2.3 -- "поля ноди-шаблона -> input-и workflow"),
keyed by template field name, value is "<ComfyUI node title>.<input key>":
{
  "prompt": "Positive Prompt.text",
  "seed": "KSampler.seed",
  "image": "Load Image.image"
}

A field can be flagged "optional": true in param_schema when it's not
guaranteed to be meaningful for every capability of this node_type_slug --
e.g. a toggle that only one of several backend variants' workflows wires up.
This needs no special handling here: build_workflow() below only touches
fields that a given capability's own param_mapping actually maps, so a
capability that leaves such a field out of its mapping just ignores whatever
value the user set for it. "optional" is purely a UI/documentation hint --
it does not relax validate_params() (already governed by "required").
"""
import copy
from typing import Any

FIELD_TYPES = {"image", "text", "int", "float", "seed", "enum", "file", "bool"}


class TemplateValidationError(ValueError):
    pass


def validate_params(param_schema: dict, params: dict[str, Any]) -> None:
    fields = param_schema.get("fields", [])
    for field in fields:
        name = field["name"]
        ftype = field.get("type")
        if ftype in ("image", "file"):
            # Never in Node.params -- these are supplied positionally via
            # Node.inputs instead (see resolve_node_inputs in worker/tasks.py
            # and slotFields in frontend/src/templateUtils.ts). A required
            # image/file field with nothing in params is the normal case,
            # not a validation failure -- checking it here would reject
            # every node of every template that has one.
            continue
        required = field.get("required", False)
        if required and name not in params:
            raise TemplateValidationError(f"missing required field '{name}'")
        if name not in params:
            continue
        value = params[name]
        if ftype in ("int", "seed") and not isinstance(value, int):
            raise TemplateValidationError(f"field '{name}' must be an int")
        if ftype == "float" and not isinstance(value, (int, float)):
            raise TemplateValidationError(f"field '{name}' must be a number")
        if ftype == "bool" and not isinstance(value, bool):
            raise TemplateValidationError(f"field '{name}' must be a bool")
        if ftype == "enum":
            options = field.get("options", [])
            if options and value not in options:
                raise TemplateValidationError(f"field '{name}' must be one of {options}")
        if ftype in ("int", "float"):
            if "min" in field and value < field["min"]:
                raise TemplateValidationError(f"field '{name}' below minimum {field['min']}")
            if "max" in field and value > field["max"]:
                raise TemplateValidationError(f"field '{name}' above maximum {field['max']}")


def _title_index(workflow_json: dict) -> dict[str, str]:
    """Map ComfyUI node title -> node id, per SPEC's resolution of open question #3
    (title is more stable across workflow re-exports than numeric node id)."""
    index: dict[str, str] = {}
    for node_id, node in workflow_json.items():
        title = (node.get("_meta") or {}).get("title")
        if title:
            index[title] = node_id
    return index


def build_workflow(workflow_json: dict, param_mapping: dict[str, str], resolved_inputs: dict[str, Any]) -> dict:
    """Return a deep copy of workflow_json with resolved_inputs applied per param_mapping.

    An input_key with further dots (e.g. "crop_region.x", from a
    workflow_analyzer composite field -- see KNOWN_NODE_COMPOSITE_FIELDS) is
    a nested path into that input's own value rather than a literal key
    name: ImageCropV2's "crop_region" input is one dict {x,y,width,height},
    not four separate inputs, so writing the resolved x/y/width/height back
    means indexing into that dict, not setting a key literally called
    "crop_region.x".
    """
    workflow = copy.deepcopy(workflow_json)
    titles = _title_index(workflow)

    for field_name, target in param_mapping.items():
        if field_name not in resolved_inputs:
            continue
        if "." not in target:
            raise TemplateValidationError(f"param_mapping target '{target}' must be '<node title>.<input key>'")
        title, input_key = target.split(".", 1)
        node_id = titles.get(title)
        if node_id is None:
            raise TemplateValidationError(f"workflow has no node titled '{title}' (param_mapping for '{field_name}')")
        inputs = workflow[node_id].setdefault("inputs", {})
        path = input_key.split(".")
        for key in path[:-1]:
            inputs = inputs.setdefault(key, {})
        inputs[path[-1]] = resolved_inputs[field_name]

    return workflow
