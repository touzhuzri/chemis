import { useEffect, useState } from "react";
import { api } from "../api";
import type {
  EntityView,
  EvidenceView,
  ProvenanceChain,
  ReactionView,
  RouteView,
} from "../types";

/** Knowledge browsing + provenance navigation (§97): every object leads
 *  to its sources along the real relation chain -- never a fake graph
 *  (§98). */
export default function KnowledgePage({
  kind,
  id,
  go,
}: {
  kind: string;
  id: string;
  go: (hash: string) => void;
}) {
  return (
    <>
      <section className="panel">
        <h2>Knowledge Base</h2>
        <SearchBox go={go} />
      </section>
      {id ? (
        kind === "entity" ? (
          <EntityViewPanel id={id} go={go} />
        ) : kind === "reaction" ? (
          <ReactionViewPanel id={id} go={go} />
        ) : kind === "evidence" ? (
          <EvidenceViewPanel id={id} />
        ) : kind === "route" ? (
          <RouteViewPanel id={id} go={go} />
        ) : null
      ) : (
        <section className="panel">
          <p className="muted">
            从上方搜索一个实体或反应；或从研究工作区的链接进入。
            所有知识均为只读展示（Evidence / Reaction 不提供编辑）。
          </p>
        </section>
      )}
    </>
  );
}

function SearchBox({ go }: { go: (h: string) => void }) {
  const [name, setName] = useState("");
  const [results, setResults] = useState<
    { kind: string; id: string; label: string; match: string }[]
  >([]);
  const [error, setError] = useState("");
  const run = async () => {
    setError("");
    try {
      const r = await api.search({ kind: "entity", name });
      setResults(r.results);
    } catch (exc) {
      setError(String(exc));
    }
  };
  return (
    <div>
      <label>按名称搜索实体（精确匹配；不会创建实体）</label>
      <div className="row">
        <input
          type="text"
          value={name}
          placeholder="phenylacetylene"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
        />
        <button className="secondary" onClick={run}>
          搜索
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <table>
        <tbody>
          {results.map((r) => (
            <tr key={`${r.kind}:${r.id}`}>
              <td>
                <span className="badge">{r.kind}</span>
              </td>
              <td>
                <a onClick={() => go(`#/knowledge/${r.kind}/${encodeURIComponent(r.id)}`)}>
                  {r.id}
                </a>
              </td>
              <td>{r.label}</td>
              <td>
                <span className={`badge ${r.match === "SCREENING" ? "screening" : "known"}`}>
                  {r.match}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ChainLinks({
  chain,
  go,
}: {
  chain: ProvenanceChain | null;
  go: (h: string) => void;
}) {
  if (!chain) return null;
  const labels: Record<string, string> = {
    mentions: "Mentions",
    reactions: "Reactions",
    papers: "Papers",
    documents: "Documents",
    evidence: "Evidence",
    entities: "Entities",
    routes: "Routes",
    records: "Records",
  };
  return (
    <div className="row" style={{ marginTop: 10 }}>
      {Object.entries(chain.links).map(([key, ids]) =>
        ids.length ? (
          <span key={key} className="badge">
            {labels[key] ?? key}:{" "}
            {ids.map((id, i) => (
              <a
                key={id}
                onClick={() =>
                  go(
                    `#/knowledge/${
                      key === "evidence"
                        ? "evidence"
                        : key === "routes"
                          ? "route"
                          : key === "reactions"
                            ? "reaction"
                            : key === "entities"
                              ? "entity"
                              : "entity"
                    }/${encodeURIComponent(id)}`,
                  )
                }
              >
                {id.slice(0, 12)}…{i < ids.length - 1 ? ", " : ""}
              </a>
            ))}
          </span>
        ) : null,
      )}
    </div>
  );
}

function EntityViewPanel({ id, go }: { id: string; go: (h: string) => void }) {
  const [entity, setEntity] = useState<EntityView | null>(null);
  const [chain, setChain] = useState<ProvenanceChain | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api.entity(id).then(setEntity).catch((e) => setError(String(e)));
    api.entityNavigation(id).then(setChain).catch(() => {});
  }, [id]);
  if (error) return <section className="panel"><p className="error">{error}</p></section>;
  if (!entity) return <section className="panel"><p className="muted">加载中…</p></section>;
  return (
    <section className="panel">
      <h2>Entity {entity.entity_id}</h2>
      <div className="row">
        {entity.canonical_smiles && (
          <div className="mol">
            <img src={api.moleculeSvgUrl(entity.canonical_smiles)} alt="2D" />
          </div>
        )}
        <dl className="kv">
          <dt>Names</dt>
          <dd>{entity.names.join(", ") || "（无）"}</dd>
          <dt>SMILES</dt>
          <dd>{entity.canonical_smiles}</dd>
          <dt>InChI</dt>
          <dd>{entity.inchi}</dd>
          <dt>InChIKey</dt>
          <dd>{entity.inchikey}</dd>
          <dt>Formula</dt>
          <dd>{entity.molecular_formula ?? "（无）"}</dd>
          <dt>identity_key</dt>
          <dd>{entity.identity_key}</dd>
          <dt>External IDs</dt>
          <dd>
            {entity.external_identifiers
              .map((x) => `${x.namespace}:${x.identifier}`)
              .join(", ") || "（无）"}
          </dd>
        </dl>
      </div>
      <h3>Provenance 链</h3>
      <ChainLinks chain={chain} go={go} />
    </section>
  );
}

function ReactionViewPanel({ id, go }: { id: string; go: (h: string) => void }) {
  const [reaction, setReaction] = useState<ReactionView | null>(null);
  const [chain, setChain] = useState<ProvenanceChain | null>(null);
  const [evidence, setEvidence] = useState<EvidenceView[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    api.reaction(id).then(setReaction).catch((e) => setError(String(e)));
    api.reactionNavigation(id).then(setChain).catch(() => {});
    api.reactionEvidence(id).then((r) => setEvidence(r.evidence)).catch(() => {});
  }, [id]);
  if (error) return <section className="panel"><p className="error">{error}</p></section>;
  if (!reaction) return <section className="panel"><p className="muted">加载中…</p></section>;
  return (
    <section className="panel">
      <h2>Reaction {reaction.reaction_id}</h2>
      <p>
        <span className="badge known">{reaction.status}</span>{" "}
        <span className="muted">
          {reaction.paper_id || reaction.doi || reaction.source_id || "（无来源）"}
        </span>
      </p>
      <table>
        <thead>
          <tr>
            <th>Role</th>
            <th>Name</th>
            <th>Entity</th>
            <th>Structure</th>
          </tr>
        </thead>
        <tbody>
          {reaction.components.map((c, i) => (
            <tr key={i}>
              <td>
                <span className="badge">{c.role}</span>
              </td>
              <td>{c.name || "（未命名）"}</td>
              <td>
                {c.entity_id ? (
                  <a onClick={() => go(`#/knowledge/entity/${c.entity_id}`)}>{c.entity_id}</a>
                ) : (
                  <span className="muted">未解析</span>
                )}
              </td>
              <td>{c.canonical_smiles || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Evidence</h3>
      {evidence.length ? (
        <table>
          <tbody>
            {evidence.map((e) => (
              <tr key={e.evidence_id}>
                <td>“{e.raw_quote}”</td>
                <td className="muted">
                  {e.evidence_type} · {e.source}
                  {e.paper_id ? ` · ${e.paper_id}` : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">没有可解析证据 — 该反应不因此获得实验支持</p>
      )}
      <h3>Provenance 链</h3>
      <ChainLinks chain={chain} go={go} />
    </section>
  );
}

function EvidenceViewPanel({ id }: { id: string }) {
  const [evidence, setEvidence] = useState<EvidenceView | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api.evidence(id).then(setEvidence).catch((e) => setError(String(e)));
  }, [id]);
  if (error) return <section className="panel"><p className="error">{error}</p></section>;
  if (!evidence) return <section className="panel"><p className="muted">加载中…</p></section>;
  return (
    <section className="panel">
      <h2>Evidence {evidence.evidence_id}</h2>
      <p className="note">Evidence 只读（§93）：没有编辑接口；修正走未来的 append-only 机制。</p>
      <dl className="kv">
        <dt>Type / Strength</dt>
        <dd>
          {evidence.evidence_type} · {evidence.strength}
        </dd>
        <dt>Quote</dt>
        <dd>“{evidence.raw_quote}”</dd>
        <dt>Source</dt>
        <dd>
          {evidence.source} · {evidence.source_id}
        </dd>
        <dt>Paper / DOI</dt>
        <dd>
          {evidence.paper_id ?? "—"} {evidence.doi ?? ""}
        </dd>
        <dt>retrieved_at</dt>
        <dd>{evidence.retrieved_at}</dd>
      </dl>
    </section>
  );
}

function RouteViewPanel({ id, go }: { id: string; go: (h: string) => void }) {
  const [route, setRoute] = useState<RouteView | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    api.route(id).then(setRoute).catch((e) => setError(String(e)));
  }, [id]);
  if (error) return <section className="panel"><p className="error">{error}</p></section>;
  if (!route) return <section className="panel"><p className="muted">加载中…</p></section>;
  return (
    <section className="panel">
      <h2>Route {route.route_id}</h2>
      <p className="muted">
        target: {route.target_smiles} · source: {route.source} · score:{" "}
        {route.score ?? "—"} ({route.score_source})
      </p>
      <table>
        <thead>
          <tr>
            <th>Step</th>
            <th>Transform</th>
            <th>Status</th>
            <th>Reaction links</th>
          </tr>
        </thead>
        <tbody>
          {route.steps.map((s) => (
            <tr key={s.step_id}>
              <td>{s.step_index}</td>
              <td>
                {s.reactant_smiles.join(" + ")} → {s.product_smiles.join(" + ")}
              </td>
              <td>
                <span className="badge">{s.status}</span>
                <div className="muted">{s.support_level}</div>
              </td>
              <td>
                {s.linked_reactions.map((l) => (
                  <div key={l.link_id}>
                    <a onClick={() => go(`#/knowledge/reaction/${l.reaction_id}`)}>
                      {l.reaction_id.slice(0, 12)}…
                    </a>{" "}
                    <span className={`badge ${l.screening_only ? "screening" : "known"}`}>
                      {l.screening_only ? "SCREENING（非支持）" : "EXACT"}
                    </span>
                  </div>
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
