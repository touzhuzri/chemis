import type { ReactionGraphView } from "../types";

/** A minimal, dependency-free SVG reaction graph (Stage 5.3 §25/§26).
 *  Representation only: nodes and edges, clickable, no route planning. */
export default function ReactionGraphViewSvg({
  graph,
  go,
}: {
  graph: ReactionGraphView;
  go: (hash: string) => void;
}) {
  const columns: Record<string, number> = { molecule: 90, reaction: 320, literature: 570 };
  const grouped: Record<string, typeof graph.nodes> = {
    molecule: [], reaction: [], literature: [],
  };
  for (const node of graph.nodes) grouped[node.type].push(node);

  const positions = new Map<string, { x: number; y: number }>();
  const rowGap = 58;
  for (const [type, nodes] of Object.entries(grouped)) {
    nodes.forEach((node, i) => {
      positions.set(node.id, { x: columns[type], y: 70 + i * rowGap });
    });
  }

  const width = 720;
  const height = Math.max(
    160,
    100 + Math.max(...Object.values(grouped).map((n) => n.length)) * rowGap,
  );

  const color: Record<string, string> = {
    molecule: "#1f5fa8", reaction: "#1d7a4f", literature: "#8a5a00",
  };

  const onClick = (type: string, meta: Record<string, unknown>) => {
    if (type === "molecule" && meta.canonical_smiles) {
      go(`#/molecule/${encodeURIComponent(String(meta.canonical_smiles))}`);
    } else if (type === "reaction" && meta.template_id) {
      go(`#/reactions/${meta.template_id}`);
    } else if (type === "literature" && meta.paper_id) {
      go(`#/literature/${meta.paper_id}`);
    }
  };

  return (
    <svg width={width} height={height} style={{ border: "1px solid var(--line)", borderRadius: 8, background: "#fff" }}>
      {graph.edges.map((edge, i) => {
        const a = positions.get(edge.source);
        const b = positions.get(edge.target);
        if (!a || !b) return null;
        return (
          <line
            key={i}
            x1={a.x}
            y1={a.y}
            x2={b.x}
            y2={b.y}
            stroke="#c3ccd6"
            strokeWidth={1.4}
          />
        );
      })}
      {graph.nodes.map((node) => {
        const p = positions.get(node.id);
        if (!p) return null;
        const label = node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label;
        return (
          <g
            key={node.id}
            style={{ cursor: "pointer" }}
            onClick={() => onClick(node.type, node.metadata)}
          >
            <circle cx={p.x} cy={p.y} r={9} fill={color[node.type] ?? "#666"} />
            <text x={p.x + 15} y={p.y + 4} fontSize={12} fill="#1c2430">
              {label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
