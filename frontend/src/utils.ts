/** Joins truthy class names with a space, dropping the rest -- the
 * `[...].filter(Boolean).join(" ")` one-liner repeated across NodeCell.tsx
 * and Grid.tsx for conditional className strings. */
export function cx(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}

/** A stable colour per backend, so a glance at a node's progress bars says
 * which agent each variant landed on.
 *
 * Computed from the backend list every render, never stored: a colour column
 * would be one more thing to migrate, keep in sync and let the user edit, for
 * something that only has to be *distinct*, not chosen. Hues are spread evenly
 * over the list (sorted by id, so the mapping doesn't shuffle when the API
 * returns backends in a different order) rather than hashed per id -- two
 * hashed uuids land within 30deg of each other about a fifth of the time, which
 * for the two-agent case this exists for is exactly the collision that would
 * make the whole thing useless.
 *
 * The range skips reds (hue < 40) on purpose: the progress bar already paints
 * `var(--danger)` to mean "this job errored", so no backend can be handed a
 * colour that reads as failure. */
export function backendColors(backends: { id: string }[]): Record<string, string> {
  const ids = backends.map((b) => b.id).sort();
  const colors: Record<string, string> = {};
  ids.forEach((id, i) => {
    colors[id] = `hsl(${Math.round(40 + (280 * i) / Math.max(1, ids.length))}deg 65% 55%)`;
  });
  return colors;
}

/** Seconds as "4:07" / "1:02:33" -- hours only once there are any. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const fields = total >= 3600 ? [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60] : [Math.floor(total / 60), total % 60];
  return fields.map((value, i) => (i === 0 ? String(value) : String(value).padStart(2, "0"))).join(":");
}

/** File extension (no dot) for a suggested download filename, derived from
 * the asset's own `mime_type` -- never from its URL. `storage.py::put_object`
 * deliberately drops the extension when writing to disk ("content-type is
 * served from the Asset.mime_type DB column, not the filename"), and an
 * asset's URL is `/api/assets/<id>/file?token=...`, which never had one
 * either. Guessing off the URL (`url.split(".").pop()`) grabbed the whole
 * "api/assets/<uuid>/file?token=..." tail once there was no "." to split on,
 * producing a garbage suggested filename once a caller supplied its own
 * (2026-08-09) -- mime_type is the one field that's actually always right. */
export function extensionForMimeType(mimeType: string | null | undefined): string {
  // Strip a ";charset=..." parameter and a "+xml"-style suffix (e.g.
  // "image/svg+xml" -> "svg") before falling back to the bare subtype.
  const subtype = mimeType?.split("/")[1]?.split(/[+;]/)[0];
  if (!subtype) return "bin";
  const overrides: Record<string, string> = { jpeg: "jpg", "gltf-binary": "glb" };
  return overrides[subtype] ?? subtype;
}
