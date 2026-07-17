import { useEffect, useState } from "react";

export interface Edge {
  from: string;
  to: string;
  // "spawn": track forked from a candidate pick (unchanged). "ref": a RefAsset
  // node pointing at the real asset node it stands in for -- the only other
  // arrow kind left once ordinary workflow<->input/output connections switched
  // to position (row alignment) instead of drawn arrows.
  kind: "spawn" | "ref";
}

interface Props {
  edges: Edge[];
  cellRefs: Map<string, HTMLDivElement>;
  containerRef: React.RefObject<HTMLDivElement>;
  deps: unknown[];
}

export function ArrowsOverlay({ edges, cellRefs, containerRef, deps }: Props) {
  const [paths, setPaths] = useState<{ d: string; kind: Edge["kind"] }[]>([]);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const compute = () => {
      const container = containerRef.current;
      if (!container) return;
      const containerRect = container.getBoundingClientRect();
      setSize({ width: container.scrollWidth, height: container.scrollHeight });

      const next: { d: string; kind: Edge["kind"] }[] = [];
      for (const edge of edges) {
        const fromEl = cellRefs.get(edge.from);
        const toEl = cellRefs.get(edge.to);
        if (!fromEl || !toEl) continue;
        const fromRect = fromEl.getBoundingClientRect();
        const toRect = toEl.getBoundingClientRect();

        const x1 = fromRect.right - containerRect.left + container.scrollLeft;
        const y1 = fromRect.top - containerRect.top + fromRect.height / 2 + container.scrollTop;
        const x2 = toRect.left - containerRect.left + container.scrollLeft;
        const y2 = toRect.top - containerRect.top + toRect.height / 2 + container.scrollTop;

        const midX = (x1 + x2) / 2;
        next.push({ d: `M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`, kind: edge.kind });
      }
      setPaths(next);
    };

    compute();
    window.addEventListener("resize", compute);
    const id = window.setInterval(compute, 500);
    return () => {
      window.removeEventListener("resize", compute);
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [edges, cellRefs, containerRef, ...deps]);

  return (
    <svg className="arrows-svg" width={size.width} height={size.height}>
      <defs>
        <marker id="arrow-merge" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="var(--accent)" />
        </marker>
        <marker id="arrow-spawn" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="var(--success)" />
        </marker>
        <marker id="arrow-ref" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 Z" fill="var(--warning)" />
        </marker>
      </defs>
      {paths.map((p, i) => (
        <path
          key={i}
          d={p.d}
          fill="none"
          stroke={p.kind === "spawn" ? "var(--success)" : p.kind === "ref" ? "var(--warning)" : "var(--accent)"}
          strokeWidth={1.5}
          strokeDasharray={p.kind === "spawn" ? "4 3" : p.kind === "ref" ? "2 2" : undefined}
          markerEnd={`url(#arrow-${p.kind})`}
        />
      ))}
    </svg>
  );
}
