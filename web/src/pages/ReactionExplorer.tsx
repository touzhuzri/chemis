import { useEffect, useState } from "react";
import { api } from "../api";
import type { ReactionTemplateView } from "../types";

// The real ReactionClassFamily enum values from Stage 3.1 (§15) -- no
// frontend-invented reaction class vocabulary.
const FAMILIES = [
  "substitution",
  "addition",
  "elimination",
  "oxidation",
  "reduction",
  "coupling",
  "rearrangement",
  "condensation",
];

/** Reaction Explorer (Stage 3.2, §四): browse the curated ReactionTemplates. */
export default function ReactionExplorer({
  go,
}: {
  go: (hash: string) => void;
}) {
  const [templates, setTemplates] = useState<ReactionTemplateView[]>([]);
  const [query, setQuery] = useState("");
  const [family, setFamily] = useState<string>("");
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .listReactions()
      .then((r) => setTemplates(r.reactions))
      .catch((e) => setError(String(e)));
  }, []);

  const shown = templates.filter((t) => {
    if (family && t.class_family !== family) return false;
    const q = query.trim().toLowerCase();
    if (q && !t.name.toLowerCase().includes(q) && !t.reaction_class.toLowerCase().includes(q)) {
      return false;
    }
    return true;
  });

  return (
    <>
      <section className="panel">
        <h2>Reaction Explorer</h2>
        <p className="muted">
          浏览 18 条 curated 反应模板。SMARTS 匹配 = 「结构符合该转化模式」，
          不是「反应一定成功」。
        </p>
        <input
          type="text"
          value={query}
          placeholder="按名称搜索反应…"
          onChange={(e) => setQuery(e.target.value)}
        />
        <div className="row" style={{ marginTop: 8, gap: 6 }}>
          <button
            className={!family ? "secondary" : ""}
            style={!family ? { fontWeight: 600 } : {}}
            onClick={() => setFamily("")}
          >
            All
          </button>
          {FAMILIES.map((f) => (
            <button
              key={f}
              className={family === f ? "secondary" : ""}
              style={family === f ? { fontWeight: 600 } : {}}
              onClick={() => setFamily(f)}
            >
              {f}
            </button>
          ))}
        </div>
        {error && <p className="error">{error}</p>}
      </section>

      <section className="panel">
        <h2>Reaction Templates（{shown.length}）</h2>
        {shown.length === 0 ? (
          <p className="muted">（无匹配反应模板）</p>
        ) : (
          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
            {shown.map((t) => (
              <div key={t.template_id} className="reaction-card">
                <a onClick={() => go(`#/reactions/${t.template_id}`)}>{t.name}</a>
                <div className="muted">{t.transformation.note}</div>
                <div>
                  <span className="badge">{t.class_family}</span>{" "}
                  <span className="badge known">{t.evidence_status}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </>
  );
}
