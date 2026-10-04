import { useEffect, useState } from "react";
import { api } from "../api";
import type {
  LiteratureAccessView,
  LiteraturePaperView,
  LiteratureSearchView,
  ReactionEvidenceView,
} from "../types";

/** Literature Search (Stage 4.2): metadata search over Crossref + OpenAlex.
 *  Metadata only -- no PDF download, no permission claim. When a `paperId` is
 *  given it also shows the reaction evidence that cites that local paper
 *  (Stage 4.4 reverse query). */
export default function LiteratureSearch({
  paperId,
  go,
}: {
  paperId?: string;
  go: (hash: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<LiteratureSearchView | null>(null);
  const [selected, setSelected] = useState<LiteraturePaperView | null>(null);
  const [access, setAccess] = useState<LiteratureAccessView | null>(null);
  const [related, setRelated] = useState<ReactionEvidenceView[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "offline" | "error">(
    "idle",
  );

  useEffect(() => {
    if (!paperId) {
      setRelated([]);
      return;
    }
    api
      .literatureReactionEvidence(paperId)
      .then((r) => setRelated(r.evidence))
      .catch(() => setRelated([]));
  }, [paperId]);

  const search = async () => {
    const q = query.trim();
    if (!q) return;
    setStatus("loading");
    setSelected(null);
    try {
      const r = await api.literatureSearch(q);
      setResult(r);
      setStatus("ready");
    } catch (e) {
      setStatus(String(e).startsWith("PROVIDERS_UNAVAILABLE_OFFLINE") ? "offline" : "error");
    }
  };

  const openDoi = async (doi: string) => {
    setStatus("loading");
    setAccess(null);
    try {
      const r = await api.literatureDoi(doi);
      setSelected(r.paper);
      setStatus("ready");
      // access is best-effort: a provider outage must not blank the record
      api.literatureAccess(doi).then(setAccess).catch(() => setAccess(null));
    } catch {
      setStatus("error");
    }
  };

  return (
    <>
      {paperId && (
        <section className="panel">
          <h2>Related Reaction Evidence</h2>
          <p className="muted">
            本地文献 <span className="mono">{paperId}</span> 被哪些 ReactionTemplate
            的证据引用（反向查询）。仅本地记录，不含网络检索。
          </p>
          {related.length === 0 ? (
            <p className="muted">没有反应证据引用该文献。</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Reaction Template</th>
                  <th>Type</th>
                  <th>Scope</th>
                </tr>
              </thead>
              <tbody>
                {related.map((e) => (
                  <tr key={e.evidence_id}>
                    <td>
                      <a onClick={() => go(`#/reactions/${e.reaction_template_id}`)}>
                        {e.reaction_template_id}
                      </a>
                    </td>
                    <td>
                      <span className="badge">{e.evidence_type}</span>
                    </td>
                    <td>{e.support_scope}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      <section className="panel">
        <h2>Literature Search</h2>
        <p className="muted">
          通过 Crossref / OpenAlex 检索文献**元数据**。不下载 PDF、不判断全文权限
          （权限属 Stage 4.3）。相同 DOI 由两个来源归并为一条记录。
        </p>
        <div className="row">
          <input
            type="text"
            value={query}
            placeholder="标题 / 关键词，例如 Suzuki coupling"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                search();
              }
            }}
          />
          <button onClick={search} disabled={status === "loading" || !query.trim()}>
            {status === "loading" ? "检索中…" : "Search"}
          </button>
        </div>
        {status === "offline" && (
          <p className="muted">
            文献 provider 当前不可用（offline）。Molecule / Reaction Explorer 与本地库照常工作。
          </p>
        )}
        {status === "error" && <p className="error">文献检索失败。</p>}
      </section>

      {result && (
        <section className="panel">
          <h2>Results（{result.papers.length}）</h2>
          <p className="muted">
            {result.provider_runs.map((r) => `${r.name}: ${r.status}`).join(" · ")}
          </p>
          {result.papers.length === 0 ? (
            <p className="muted">未找到记录。</p>
          ) : (
            result.papers.map((p) => (
              <div key={p.paper_id} className="reaction-card" style={{ marginBottom: 10 }}>
                <strong>{p.title ?? "(untitled)"}</strong>
                <div className="muted">
                  {p.authors.map((a) => a.family || a.name).filter(Boolean).join(", ")}
                  {p.year ? ` · ${p.year}` : ""}
                  {p.journal ? ` · ${p.journal}` : ""}
                </div>
                {p.identifiers.doi && <div className="mono muted">DOI: {p.identifiers.doi}</div>}
                <div className="row" style={{ marginTop: 6 }}>
                  <span className="badge">Sources: {p.sources.join(" + ") || "—"}</span>
                  {p.identifiers.doi && (
                    <button className="secondary" onClick={() => openDoi(p.identifiers.doi!)}>
                      Detail
                    </button>
                  )}
                </div>
              </div>
            ))
          )}
        </section>
      )}

      {selected && (
        <section className="panel">
          <h2>Literature Detail</h2>
          <dl className="kv">
            <dt>Title</dt>
            <dd>{selected.title ?? "(untitled)"}</dd>
            <dt>Authors</dt>
            <dd>
              {selected.authors.map((a) => a.family || a.name).filter(Boolean).join(", ") || "—"}
            </dd>
            <dt>Year</dt>
            <dd>{selected.year ?? "—"}</dd>
            <dt>Journal</dt>
            <dd>{selected.journal ?? "—"}</dd>
            <dt>DOI</dt>
            <dd className="mono">{selected.identifiers.doi ?? "—"}</dd>
            <dt>OA status</dt>
            <dd>{access?.assessment?.oa_status ?? selected.access.oa_status}</dd>
            <dt>Sources</dt>
            <dd>{selected.sources.join(" + ") || "—"}</dd>
          </dl>

          <h3>Access</h3>
          {access?.assessment ? (
            <div>
              <dl className="kv">
                <dt>OA Status</dt>
                <dd>{access.assessment.oa_status}</dd>
                <dt>Fulltext Permission</dt>
                <dd>
                  <span className="badge">{access.assessment.permission}</span>{" "}
                  <span className="muted">({access.assessment.permission_basis})</span>
                </dd>
              </dl>
              <p className="muted">
                {access.assessment.permission === "may_retrieve"
                  ? "Programmatic retrieval permitted (by policy). No download is performed here."
                  : access.assessment.permission === "not_permitted"
                    ? "Current evidence indicates retrieval is not permitted."
                    : "Permission cannot be established."}
              </p>
              {access.assessment.reasons.length > 0 && (
                <ul className="muted">
                  {access.assessment.reasons.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              )}
              {access.assessment.fulltext_url && (
                <a href={access.assessment.fulltext_url} target="_blank" rel="noreferrer">
                  Open fulltext location
                </a>
              )}
            </div>
          ) : (
            <p className="muted">Access assessment unavailable。</p>
          )}

          {selected.abstracts.length > 0 && (
            <div>
              <h3>Abstract</h3>
              <p className="muted">{selected.abstracts[0].text}</p>
              <p className="muted">source: {selected.abstracts[0].source}</p>
            </div>
          )}
          <button className="secondary" onClick={() => go("#/literature")}>
            Back to search
          </button>
        </section>
      )}
    </>
  );
}
