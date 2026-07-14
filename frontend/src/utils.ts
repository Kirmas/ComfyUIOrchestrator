/** Joins truthy class names with a space, dropping the rest -- the
 * `[...].filter(Boolean).join(" ")` one-liner repeated across NodeCell.tsx
 * and Grid.tsx for conditional className strings. */
export function cx(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}
