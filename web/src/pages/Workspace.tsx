import { useEffect, useState } from "react";
import { api } from "../api";
import type {
  ConflictView,
  EntityView,
  EvidenceView,
  JobView,
  ReactionView,
  ResearchEvent,
  RouteView,
  SessionDetail,
} from "../types";

/** Research Workspace (§16): the observability page. */
export default function Workspace({
  id,
  go,
}: {
  id: string;
  go: (hash: string) => void;
}) {
  const [job, setJob] = useState<JobView | null>(null);
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [events, setEvents] = useState<ResearchEvent[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let stop = false;
    const tick = async () => {
      try {
        const j = await api.job(id);
        if (stop) return;
        setJob(j);
        const ev = await api.events(id);
        if (!stop) setEvents(ev.events);
        if (j.session_id) {
          try {
            const s = await api.session(j.session_id);
            if (!stop) setSession(s);
          } catch {
            /* session not queryable yet */
          }
        }
        if (["queued", "running"].includes(j.status)) {
          setTimeout(tick, 1200);
        }
      } catch (exc) {
        if (!stop) setError(String(exc));
      }
    };
    tick();
    return () => {
      stop = true;
    };
  }, [id]);

  const cancel = async () => {
    try {
      const j = await api.cancel(id);
      setJob(j);
    } catch (exc) {
      setError(String(exc));
    }
  };

  return (
    <>
      <section className="panel">
        <div className="row spread">
          <div>
            <h2>Research</h2>
            <p style={{ margin: 0 }}>{job?.question ?? id}</p>
            <p className="muted" style={{ margin: "6px 0 0" }}>
              状态：<span className="badge">{job?.status ?? "…"}</span>
              {job?.phase ? `　阶段：${job.phase}` : ""}
              {job?.structure_value ? `　结构：${job.structure_value}` : ""}
            </p>
          </div>
          <div className="row">
            {job && ["queued", "running"].includes(job.status) && (
              <button className="cancel" onClick={cancel}>
                取消研究
              </button>
            )}
          </div>
        </div>
        {job?.error && <p className="error">{job.error}</p>}
        {error && <p className="error">{error}</p>}
        {job && ["queued", "running"].includes(job.status) && (
          <p className="muted">研究进行中…（页面自动刷新，服务端不被阻塞）</p>
        )}
      </section>

      <section className="panel">
        <h2>Research Progress</h2>
        <ul className="events">
          {events.map((e, i) => (
            <li key={i}>
              <span className="at">{e.at}</span>
              <span className="badge">{e.type}</span> {e.message}
            </li>
          ))}
          {events.length === 0 && <li className="muted">尚无事件</li>}
        </ul>
      </section>

      {session?.knowledge_before && (
        <section className="panel">
          <h2>Existing Knowledge</h2>
          <p className="note">{session.knowledge_before.note}</p>
          <table>
            <thead>
              <tr>
                <th>Knowledge State</th>
                <th>状态</th>
                <th>依据</th>
              </tr>
            </thead>
            <tbody>
              {session.knowledge_before.facets.map((f) => (
                <tr key={f.facet}>
                  <td>{f.facet}</td>
                  <td>
                    <span className={`badge ${f.state}`}>{f.state}</span>
                  </td>
                  <td className="muted">{f.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>本次新增（New）</h3>
          <div className="row">
            {Object.entries(session.knowledge_added).map(([k, ids]) =>
              ids.length ? (
                <span key={k} className="badge known">
                  {k}: {ids.join(", ")}
                </span>
              ) : null,
            )}
            {Object.values(session.knowledge_added).every((v) => !v.length) && (
              <span className="muted">（无新增知识对象）</span>
            )}
          </div>
        </section>
      )}

      <section className="panel">
        <h2>Chemical Identity</h2>
        <IdentitySection session={session} go={go} />
      </section>

      <section className="panel">
        <h2>Molecular Analysis</h2>
        {session?.analysis?.length ? (
          <ul>
            {session.analysis.map((line, i) => (
              <li key={i}>
                <span className="badge known">[Computed]</span> {line}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">（未执行计算分析 — 通常因为身份未确定）</p>
        )}
      </section>

      <section className="panel">
        <h2>Literature / Sources</h2>
        {session?.source_ids.length ? (
          <ul>
            {session.source_ids.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">本次没有来源记录</p>
        )}
      </section>

      <section className="panel">
        <h2>Reactions</h2>
        <ReactionSection session={session} go={go} />
      </section>

      <section className="panel">
        <h2>Evidence</h2>
        <EvidenceSection session={session} />
      </section>

      <section className="panel">
        <h2>Routes</h2>
        <RouteSection session={session} go={go} />
      </section>

      <div className="grid">
        <section className="panel">
          <h2>Knowledge Gaps</h2>
          {session?.knowledge_gaps.length ? (
            session.knowledge_gaps.map((g, i) => (
              <div key={i} style={{ marginBottom: 8 }}>
                <span className="badge unknown">{g.facet}</span>{" "}
                {g.reason}
                {g.suggested_action && (
                  <div className="muted">建议：{g.suggested_action}</div>
                )}
              </div>
            ))
          ) : (
            <p className="muted">（无缺口）</p>
          )}
        </section>
        <section className="panel">
          <h2>Conflicts</h2>
          <ConflictsSection conflicts={session?.conflicts ?? []} go={go} />
        </section>
      </div>

      <section className="panel">
        <h2>Final Report</h2>
        {session?.report_markdown ? (
          <div className="report">{session.report_markdown}</div>
        ) : (
          <p className="muted">报告尚未生成</p>
        )}
      </section>
    </>
  );
}

function IdentitySection({
  session,
  go,
}: {
  session: SessionDetail | null;
  go: (h: string) => void;
}) {
  const identity = session?.identity ?? null;
  const entityId = identity?.entity_id || session?.entity_ids[0] || null;
  const [entity, setEntity] = useState<EntityView | null>(null);
  useEffect(() => {
    if (entityId) {
      api.entity(entityId).then(setEntity).catch(() => setEntity(null));
    }
  }, [entityId]);

  if (!identity) {
    return (
      <p className="muted">
        Identity unresolved — 未提供结构输入。不猜测结构。
      </p>
    );
  }
  if (identity.status !== "resolved") {
    return (
      <div>
        <p className="error">Identity unresolved（resolution status: {identity.status}）</p>
        <p className="muted">
          input: {identity.input_kind} `{identity.input_value}`
          {identity.reason ? ` — ${identity.reason}` : ""}
        </p>
        <p className="muted">未解析即不给出结构；不猜测、不选第一候选。</p>
      </div>
    );
  }
  return (
    <div>
      <div className="row">
        {(identity.canonical_smiles || entity?.canonical_smiles) && (
          <div className="mol">
            <img
              src={api.moleculeSvgUrl(
                identity.canonical_smiles || entity?.canonical_smiles || "",
              )}
              alt="2D structure"
            />
          </div>
        )}
        <dl className="kv">
          <dt>Name</dt>
          <dd>{identity.names.join(", ") || "（无名称；名称从不是身份）"}</dd>
          <dt>SMILES</dt>
          <dd>{identity.canonical_smiles || entity?.canonical_smiles || "—"}</dd>
          <dt>InChI</dt>
          <dd>{identity.inchi || entity?.inchi || "—"}</dd>
          <dt>InChIKey</dt>
          <dd>{identity.inchikey || entity?.inchikey || "—"}</dd>
          <dt>Entity ID</dt>
          <dd>
            {entityId ? (
              <a onClick={() => go(`#/knowledge/entity/${entityId}`)}>{entityId}</a>
            ) : (
              "（未注册实体）"
            )}
          </dd>
          <dt>Identity Status</dt>
          <dd>
            <span className="badge known">resolved</span>
            {entity ? ` · ${entity.identity_basis}` : ""}
          </dd>
          <dt>Input</dt>
          <dd>
            {identity.input_kind} `{identity.input_value}`
          </dd>
          <dt>Provenance</dt>
          <dd>{entity?.provenance ? JSON.stringify(entity.provenance) : "（无）"}</dd>
        </dl>
      </div>
    </div>
  );
}

function ReactionSection({
  session,
  go,
}: {
  session: SessionDetail | null;
  go: (h: string) => void;
}) {
  const [reactions, setReactions] = useState<ReactionView[]>([]);
  useEffect(() => {
    const ids = session?.reaction_ids ?? [];
    Promise.all(ids.map((id) => api.reaction(id).catch(() => null))).then(
      (list) => setReactions(list.filter(Boolean) as ReactionView[]),
    );
  }, [session?.reaction_ids.join(",")]);
  if (!reactions.length) return <p className="muted">没有已建立的反应</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>Reaction</th>
          <th>Status</th>
          <th>组分（role / name / entity）</th>
          <th>Evidence</th>
        </tr>
      </thead>
      <tbody>
        {reactions.map((r) => (
          <tr key={r.reaction_id}>
            <td>
              <a onClick={() => go(`#/knowledge/reaction/${r.reaction_id}`)}>
                {r.reaction_id}
              </a>
            </td>
            <td>
              <span className={`badge ${r.status === "experimentally_supported" ? "known" : ""}`}>
                {r.status}
              </span>
            </td>
            <td>
              {r.components.map((c, i) => (
                <div key={i}>
                  <span className="badge">{c.role}</span> {c.name || "（未命名）"}{" "}
                  {c.entity_id ? (
                    <a onClick={() => go(`#/knowledge/entity/${c.entity_id}`)}>
                      → {c.entity_id}
                    </a>
                  ) : (
                    <span className="muted">（未解析）</span>
                  )}
                </div>
              ))}
            </td>
            <td>{r.evidence_ids.length ? r.evidence_ids.join(", ") : "（无）"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function EvidenceSection({ session }: { session: SessionDetail | null }) {
  const [items, setItems] = useState<EvidenceView[]>([]);
  useEffect(() => {
    const ids = session?.evidence_ids ?? [];
    Promise.all(ids.map((id) => api.evidence(id).catch(() => null))).then(
      (list) => setItems(list.filter(Boolean) as EvidenceView[]),
    );
  }, [session?.evidence_ids.join(",")]);
  if (!items.length) return <p className="muted">没有证据条目</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>Evidence</th>
          <th>Type</th>
          <th>Quote</th>
          <th>Source</th>
        </tr>
      </thead>
      <tbody>
        {items.map((e) => (
          <tr key={e.evidence_id}>
            <td>{e.evidence_id.slice(0, 10)}…</td>
            <td>
              <span className="badge">{e.evidence_type}</span>
            </td>
            <td>“{e.raw_quote}”</td>
            <td className="muted">
              {e.source}
              {e.paper_id ? ` · ${e.paper_id}` : ""}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RouteSection({
  session,
  go,
}: {
  session: SessionDetail | null;
  go: (h: string) => void;
}) {
  const [routes, setRoutes] = useState<RouteView[]>([]);
  useEffect(() => {
    const ids = session?.route_ids ?? [];
    Promise.all(ids.map((id) => api.route(id).catch(() => null))).then(
      (list) => setRoutes(list.filter(Boolean) as RouteView[]),
    );
  }, [session?.route_ids.join(",")]);
  if (!routes.length) return <p className="muted">没有候选路线</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>Route</th>
          <th>Steps</th>
          <th>Step ↔ Reaction</th>
        </tr>
      </thead>
      <tbody>
        {routes.map((r) => (
          <tr key={r.route_id}>
            <td>
              <a onClick={() => go(`#/knowledge/route/${r.route_id}`)}>
                {r.route_id}
              </a>
              <div className="muted">{r.target_smiles}</div>
            </td>
            <td>
              {r.steps.map((s) => (
                <div key={s.step_id}>
                  <span className="badge">{s.status}</span>{" "}
                  {s.reactant_smiles.join(" + ")} → {s.product_smiles.join(" + ")}
                </div>
              ))}
            </td>
            <td>
              {r.steps.flatMap((s) =>
                s.linked_reactions.map((l) => (
                  <div key={l.link_id}>
                    <a onClick={() => go(`#/knowledge/reaction/${l.reaction_id}`)}>
                      {l.reaction_id.slice(0, 12)}…
                    </a>{" "}
                    <span className={`badge ${l.screening_only ? "screening" : "known"}`}>
                      {l.screening_only ? "SCREENING" : "EXACT"}
                    </span>
                  </div>
                )),
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ConflictsSection({
  conflicts,
  go,
}: {
  conflicts: ConflictView[];
  go: (h: string) => void;
}) {
  if (!conflicts.length) return <p className="muted">（无冲突）</p>;
  return (
    <div>
      {conflicts.map((c, i) => (
        <div key={i} style={{ marginBottom: 12 }}>
          <span className="badge conflict">CONFLICT</span> {c.field}
          <table>
            <tbody>
              {c.observations.map((o, j) => (
                <tr key={j}>
                  <td>
                    {o.raw_value} {o.raw_unit || o.unit}
                  </td>
                  <td>
                    {o.evidence_id ? (
                      <span className="muted">← {o.evidence_id.slice(0, 10)}…</span>
                    ) : null}
                  </td>
                  <td>
                    {o.reaction_id ? (
                      <a onClick={() => go(`#/knowledge/reaction/${o.reaction_id}`)}>
                        {o.reaction_id.slice(0, 12)}…
                      </a>
                    ) : null}
                  </td>
                  <td className="muted">{o.paper_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="muted">{c.note}</div>
        </div>
      ))}
    </div>
  );
}
