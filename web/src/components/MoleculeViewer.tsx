import { useEffect, useRef, useState } from "react";
import type { ConformerView } from "../types";

/** CPK element colours (frontend only -- the backend never stores colour as
 *  a chemical fact, §7). Common organic elements are covered. */
const COLORS: Record<string, string> = {
  H: "#e6e6e6",
  C: "#404040",
  N: "#3050f8",
  O: "#ff0d0d",
  S: "#c8b400",
  P: "#ff8000",
  F: "#90e050",
  Cl: "#1ff01f",
  Br: "#a62929",
  I: "#940094",
};

function colorOf(element: string): string {
  return COLORS[element] ?? "#808080";
}

type View = { rx: number; ry: number; zoom: number; ox: number; oy: number };

/** A dependency-free ball-and-stick viewer over a generated conformer.
 *  Rotate (drag), zoom (wheel), pan (shift+drag), reset, and a show-hydrogens
 *  toggle. Renders the atoms/bonds the API already returned -- no RDKit here. */
export default function MoleculeViewer({
  conformer,
}: {
  conformer: ConformerView;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [view, setView] = useState<View>({ rx: -0.4, ry: 0.5, zoom: 1, ox: 0, oy: 0 });
  const [showHydrogens, setShowHydrogens] = useState(false);
  const drag = useRef<{ x: number; y: number; pan: boolean } | null>(null);

  const reset = () => setView({ rx: -0.4, ry: 0.5, zoom: 1, ox: 0, oy: 0 });

  useEffect(() => {
    draw();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conformer, view, showHydrogens]);

  function draw() {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const W = canvas.width;
    const H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    const atoms = showHydrogens
      ? conformer.atoms
      : conformer.atoms.filter((a) => a.atomic_number !== 1);

    // centre on the centroid
    let cx = 0;
    let cy = 0;
    let cz = 0;
    for (const a of atoms) {
      cx += a.x;
      cy += a.y;
      cz += a.z;
    }
    cx /= atoms.length;
    cy /= atoms.length;
    cz /= atoms.length;

    // scale to fit
    let extent = 1e-6;
    for (const a of atoms) {
      extent = Math.max(
        extent,
        Math.abs(a.x - cx),
        Math.abs(a.y - cy),
        Math.abs(a.z - cz),
      );
    }
    const scale = (Math.min(W, H) / (extent * 2.4)) * view.zoom;

    const cosX = Math.cos(view.rx);
    const sinX = Math.sin(view.rx);
    const cosY = Math.cos(view.ry);
    const sinY = Math.sin(view.ry);

    const project = (a: { x: number; y: number; z: number }) => {
      const dx = a.x - cx;
      const dy = a.y - cy;
      const dz = a.z - cz;
      // yaw around Y, then pitch around X
      const x1 = dx * cosY + dz * sinY;
      const z1 = -dx * sinY + dz * cosY;
      const y2 = dy * cosX - z1 * sinX;
      const z2 = dy * sinX + z1 * cosX;
      return {
        sx: x1 * scale + W / 2 + view.ox,
        sy: -y2 * scale + H / 2 + view.oy,
        depth: z2,
      };
    };

    const projected = atoms.map(project);
    const atomById = new Map(atoms.map((a) => [a.atom_index, a]));
    const radius = 7 * view.zoom;
    const hRadius = 3.5 * view.zoom;

    // bonds (behind atoms), depth-sorted by midpoint depth
    const bonds = conformer.bonds
      .filter(
        (b) =>
          atomById.has(b.a) &&
          atomById.has(b.b) &&
          (showHydrogens ||
            (atomById.get(b.a)!.atomic_number !== 1 &&
              atomById.get(b.b)!.atomic_number !== 1)),
      )
      .map((b) => ({ ...b, depth: (projected[b.a].depth + projected[b.b].depth) / 2 }))
      .sort((p, q) => q.depth - p.depth);

    for (const bond of bonds) {
      const a = projected[bond.a];
      const b = projected[bond.b];
      ctx.strokeStyle = "#909090";
      ctx.lineWidth = (bond.order === "aromatic" ? 2 : 1.6) * view.zoom;
      ctx.beginPath();
      ctx.moveTo(a.sx, a.sy);
      ctx.lineTo(b.sx, b.sy);
      ctx.stroke();
    }

    // atoms, far-to-near so the front atom paints over the back bond
    const sorted = atoms
      .map((a, i) => ({ a, p: projected[i] }))
      .sort((x, y) => x.p.depth - y.p.depth);
    for (const { a, p } of sorted) {
      const r = a.atomic_number === 1 ? hRadius : radius;
      ctx.fillStyle = colorOf(a.element);
      ctx.beginPath();
      ctx.arc(p.sx, p.sy, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#00000030";
      ctx.lineWidth = 1;
      ctx.stroke();
    }
  }

  const onMouseDown = (e: React.MouseEvent) => {
    drag.current = { x: e.clientX, y: e.clientY, pan: e.shiftKey };
  };
  const onMouseMove = (e: React.MouseEvent) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.x;
    const dy = e.clientY - drag.current.y;
    drag.current = { x: e.clientX, y: e.clientY, pan: drag.current.pan };
    setView((v) =>
      drag.current!.pan
        ? { ...v, ox: v.ox + dx, oy: v.oy + dy }
        : { ...v, ry: v.ry + dx * 0.008, rx: v.rx + dy * 0.008 },
    );
  };
  const onMouseUp = () => {
    drag.current = null;
  };
  const onWheel = (e: React.WheelEvent) => {
    setView((v) => ({
      ...v,
      zoom: Math.max(0.3, Math.min(4, v.zoom * (e.deltaY < 0 ? 1.1 : 0.9))),
    }));
  };

  return (
    <div>
      <canvas
        ref={canvasRef}
        width={440}
        height={340}
        style={{ border: "1px solid var(--line)", borderRadius: 6, background: "#fff", cursor: "grab" }}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onMouseLeave={onMouseUp}
        onWheel={onWheel}
      />
      <div className="row" style={{ marginTop: 8 }}>
        <button className="secondary" onClick={reset}>
          Reset View
        </button>
        <label style={{ display: "flex", alignItems: "center", gap: 6, margin: 0 }}>
          <input
            type="checkbox"
            checked={showHydrogens}
            onChange={(e) => setShowHydrogens(e.target.checked)}
          />
          Show Hydrogens
        </label>
        <span className="muted">拖拽旋转 · 滚轮缩放 · Shift+拖拽平移</span>
      </div>
    </div>
  );
}
