import { useEffect, useState } from "react";
import { api } from "../api";
import type { ReactionEvidenceView, ReactionTemplateView } from "../types";

/** Reaction Detail (Stage 3.2, §五): one ReactionTemplate, fully displayed.
 *  All fields come from the template -- nothing is guessed from the name (§七). */
export default function ReactionDetail({
  templateId,
  from,
  go,
}: {
  templateId: string;
  from: string;
  go: (hash: string) => void;
}) {
  const [template, setTemplate] = useState<ReactionTemplateView | null>(null);
  const [evidence, setEvidence] = useState<ReactionEvidenceView[]>([]);
  const [error, setError] = useState("");
  const [linkDoi, setLinkDoi] = useState("");
  const [linkScope, setLinkScope] = useState("");
  const [linkType, setLinkType] = useState("transformation_support");
  const [linkMsg, setLinkMsg] = useState("");

  const refreshEvidence = () =>
    api
      .reactionTemplateEvidence(templateId)
      .then((r) => setEvidence(r.evidence))
      .catch(() => setEvidence([]));

  useEffect(() => {
    api
      .getReaction(templateId)
      .then(setTemplate)
      .catch((e) => setError(String(e)));
    refreshEvidence();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templateId]);

  const linkLiterature = async () => {
    if (!linkDoi.trim() || !linkScope.trim()) return;
    setLinkMsg("");
    try {
      const r = await api.linkLiteratureEvidence(templateId, {
        doi: linkDoi.trim(),
        evidence_type: linkType,
        support_scope: linkScope.trim(),
      });
      setLinkMsg(r.is_new ? "已建立文献证据。" : "该证据已存在，未重复写入。");
      refreshEvidence();
    } catch (e) {
      setLinkMsg(String(e));
    }
  };

  if (error) {
    return (
      <section className="panel">
        <h2>Reaction</h2>
        <p className="error">{error}</p>
        <button className="secondary" onClick={() => go("#/reactions")}>
          Back to Reaction Explorer
        </button>
      </section>
    );
  }

  if (!template) return <p className="muted">加载中…</p>;

  const selectivity = template.selectivity;
  const hasSelectivity =
    selectivity &&
    (selectivity.regioselectivity ||
      selectivity.stereoselectivity ||
      selectivity.chemoselectivity);

  return (
    <>
      <section className="panel">
        <div className="row spread">
          <div>
            <h2 style={{ margin: 0 }}>{template.name}</h2>
            <div style={{ marginTop: 6 }}>
              <span className="badge">{template.class_family}</span>{" "}
              <span className="muted">{template.reaction_class}</span>
            </div>
          </div>
          <div className="row">
            <button className="secondary" onClick={() => go("#/reactions")}>
              Back to Reaction Explorer
            </button>
            {from && (
              <button
                className="secondary"
                onClick={() => go(`#/molecule/${encodeURIComponent(from)}`)}
              >
                Back to Molecule
              </button>
            )}
          </div>
        </div>
      </section>

      <div className="grid">
        <section className="panel">
          <h2>Transformation</h2>
          <p className="reaction-scheme">
            {template.transformation.input_features.join(" + ")} →{" "}
            {template.transformation.output_features.join(" + ")}
          </p>
          <p className="muted">{template.transformation.note}</p>
        </section>

        <section className="panel">
          <h2>Evidence</h2>
          <dl className="kv">
            <dt>Evidence status</dt>
            <dd>
              <span className="badge known">{template.evidence_status}</span>
            </dd>
            <dt>Source</dt>
            <dd>{template.source_type}</dd>
          </dl>
          {evidence.length === 0 ? (
            <p className="muted">No evidence records.</p>
          ) : (
            <div>
              <table style={{ marginTop: 8 }}>
                <thead>
                  <tr>
                    <th>Type</th>
                    <th>Scope</th>
                    <th>Source</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {evidence.map((e) => (
                    <tr key={e.evidence_id}>
                      <td>
                        <span className="badge">{e.evidence_type}</span>
                      </td>
                      <td>{e.support_scope}</td>
                      <td className="muted">
                        {e.source.source_type === "literature" && e.source.source_id
                          ? (
                            <a onClick={() => go(`#/literature/${encodeURIComponent(e.source.source_id)}`)}>
                              {e.source.title || e.source.source_id}
                            </a>
                          )
                          : `${e.source.source_type}${e.source.title ? ` · ${e.source.title}` : ""}`}
                      </td>
                      <td>
                        <span className="badge known">{e.status}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h3>Link Literature Evidence</h3>
              <p className="muted">
                仅链接**本地已有**的文献记录（DOI 在本地库中查找）；不会联网检索。
                curated 证据不受影响。
              </p>
              <div className="row">
                <input
                  type="text"
                  value={linkDoi}
                  placeholder="DOI，例如 10.1000/abc"
                  onChange={(e) => setLinkDoi(e.target.value)}
                />
                <select value={linkType} onChange={(e) => setLinkType(e.target.value)}>
                  <option value="transformation_support">transformation_support</option>
                  <option value="mechanism_support">mechanism_support</option>
                  <option value="selectivity_support">selectivity_support</option>
                  <option value="condition_support">condition_support</option>
                  <option value="limitation_support">limitation_support</option>
                </select>
                <input
                  type="text"
                  value={linkScope}
                  placeholder="支持范围，例如 primary alcohol -> aldehyde"
                  onChange={(e) => setLinkScope(e.target.value)}
                />
                <button onClick={linkLiterature} disabled={!linkDoi.trim() || !linkScope.trim()}>
                  Link
                </button>
              </div>
              {linkMsg && <p className="muted">{linkMsg}</p>}
            </div>
          )}
        </section>
      </div>

      <div className="grid">
        <section className="panel">
          <h2>Substrate Pattern</h2>
          <dl className="kv">
            <dt>Pattern</dt>
            <dd>{template.transformation.input_features.join(" + ")}</dd>
            <dt>SMARTS</dt>
            <dd className="mono">{template.substrate_patterns.join("  |  ")}</dd>
          </dl>
        </section>

        <section className="panel">
          <h2>Product Pattern</h2>
          {template.product_patterns.length === 0 ? (
            <p className="muted">Product pattern not encoded.</p>
          ) : (
            <dl className="kv">
              <dt>Pattern</dt>
              <dd>{template.transformation.output_features.join(" + ")}</dd>
              <dt>SMARTS</dt>
              <dd className="mono">{template.product_patterns.join("  |  ")}</dd>
            </dl>
          )}
        </section>
      </div>

      <div className="grid">
        <section className="panel">
          <h2>Reagents</h2>
          {template.reagents.length === 0 ? (
            <p className="muted">（未指定）</p>
          ) : (
            <ul>
              {template.reagents.map((r, i) => (
                <li key={i}>
                  {r.name}
                  {r.role ? <span className="muted"> · {r.role}</span> : null}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="panel">
          <h2>Typical Conditions</h2>
          {template.conditions.length === 0 ? (
            <p className="muted">（未指定）</p>
          ) : (
            <ul>
              {template.conditions.map((c, i) => (
                <li key={i}>{c.value}</li>
              ))}
            </ul>
          )}
          <p className="muted">典型条件，非实验实例。</p>
        </section>
      </div>

      <div className="grid">
        <section className="panel">
          <h2>Mechanism</h2>
          <p>{template.mechanism_class || "Unknown / Not specified"}</p>
        </section>

        <section className="panel">
          <h2>Selectivity</h2>
          {hasSelectivity ? (
            <dl className="kv">
              <dt>Regioselectivity</dt>
              <dd>{selectivity!.regioselectivity || "Not specified"}</dd>
              <dt>Stereoselectivity</dt>
              <dd>{selectivity!.stereoselectivity || "Not specified"}</dd>
              <dt>Chemoselectivity</dt>
              <dd>{selectivity!.chemoselectivity || "Not specified"}</dd>
            </dl>
          ) : (
            <p className="muted">Not specified</p>
          )}
        </section>
      </div>

      <section className="panel">
        <h2>Limitations</h2>
        {template.limitations.length === 0 ? (
          <p className="muted">（未记录）</p>
        ) : (
          <ul>
            {template.limitations.map((l, i) => (
              <li key={i}>{l.text}</li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel">
        <h2>Internal Information</h2>
        <dl className="kv">
          <dt>template_id</dt>
          <dd className="mono">{template.template_id}</dd>
        </dl>
        <p className="muted">
          这是 CREA 内部记录 ID，不是国际反应 ID。
        </p>
      </section>
    </>
  );
}
