import { useEffect, useState } from "react";

/** Renders a mesh asset.
 *
 * `@google/model-viewer` bundles the whole of three.js -- roughly a megabyte of
 * the built JS, which used to sit in the main chunk because main.tsx imported
 * it unconditionally at startup. Every page load paid for it, and rollup had to
 * minify it as part of one giant chunk, which is what kept getting the build
 * OOM-killed on this ~2 GB box.
 *
 * Loading it here instead means the custom element (and three.js with it) is
 * fetched only when a mesh is actually on screen. A project with no mesh assets
 * -- which is the normal case -- never downloads it at all. The import is
 * module-cached, so several mesh cells still only fetch it once.
 */
export function Model3DThumb({ url }: { url: string | null }) {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!url) return;
    let cancelled = false;
    import("@google/model-viewer")
      .then(() => {
        if (!cancelled) setReady(true);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [url]);

  if (!url) return <div className="slot-thumb" />;
  // Placeholder keeps the cell's shape while three.js is on its way, so the
  // grid doesn't reflow around it when the element finally registers.
  if (!ready) return <div className="slot-thumb model-thumb-loading" />;
  return (
    // @ts-expect-error -- <model-viewer> is a web component registered via @google/model-viewer, no React types
    <model-viewer src={url} camera-controls disable-zoom style={{ width: "100%", aspectRatio: "1" }} />
  );
}
