export function Model3DThumb({ url }: { url: string | null }) {
  if (!url) return <div className="slot-thumb" />;
  return (
    // @ts-expect-error -- <model-viewer> is a web component registered via @google/model-viewer, no React types
    <model-viewer src={url} camera-controls disable-zoom style={{ width: "100%", aspectRatio: "1" }} />
  );
}
