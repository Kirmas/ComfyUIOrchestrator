/** Joins truthy class names with a space, dropping the rest -- the
 * `[...].filter(Boolean).join(" ")` one-liner repeated across NodeCell.tsx
 * and Grid.tsx for conditional className strings. */
export function cx(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
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
