import type { Capability } from "./types";

/** True if this capability's ComfyUI workflow runs Ideogram 4 -- the signal
 * that its prompt field holds a structured JSON caption rather than prose, and
 * therefore gets the box editor (see ideogramCaption.ts).
 *
 * Two independent tells, either of which is enough: the diffusion model
 * filename (`ideogram4_fp8_scaled.safetensors` and its unconditional twin), or
 * a CLIPLoader explicitly typed `ideogram4` -- the text encoder is a Qwen3-VL
 * checkpoint whose filename says nothing about Ideogram, so the `type` widget
 * is the only hint on that side. Same shape as capabilityUsesMultiAngleLora:
 * the graph lives in `config.workflow_json` (a ComfyUI API-format prompt
 * dict), and any capability for the slug may carry it. */
export function capabilityUsesIdeogram4(capability: Capability | undefined | null): boolean {
  const wf = capability?.config?.["workflow_json"];
  if (!wf || typeof wf !== "object") return false;
  for (const node of Object.values(wf as Record<string, unknown>)) {
    if (!node || typeof node !== "object") continue;
    const inputs = (node as { inputs?: unknown }).inputs;
    if (!inputs || typeof inputs !== "object") continue;
    const { unet_name: unetName, type } = inputs as { unet_name?: unknown; type?: unknown };
    if (typeof unetName === "string" && /ideogram\s*4/i.test(unetName)) return true;
    if (typeof type === "string" && /^ideogram\s*4$/i.test(type)) return true;
  }
  return false;
}

/** The leading "W:H" of a FluxResolutionNode-style aspect string
 * ("4:5 (Artistic Frame)" -> 4/5), or null. Only the ratio is taken: the
 * pixel size is computed inside that node from megapixel/divisible_by, and
 * mirroring that arithmetic here would be a second implementation to keep in
 * sync for no gain -- the editor only needs the shape of the canvas. */
export function parseAspectRatio(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const match = value.trim().match(/^(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)/);
  if (!match) return null;
  const w = Number(match[1]);
  const h = Number(match[2]);
  if (!(w > 0) || !(h > 0)) return null;
  return w / h;
}
