import { useEffect, useState } from "react";
import { api } from "../api";
import type {
  FinalAuditView,
  ResearchStudyDetailView,
  ResearchStudyView,
  TaskWorkflowDetailView,
  TaskWorkflowView,
} from "../types";

/** Research Sessions (Stage 7.1): the container for one line of research.
 *  Question + references + timeline + artifacts. This is a process record,
 *  not a second chemistry database. */
export default function ResearchSessions({
  id,
  go,
}: {
  id: string;
  go: (hash: string) => void;
}) {
  const [sessions, setSessions] = useState<ResearchStudyView[]>([]);
  const [detail, setDetail] = useState<ResearchStudyDetailView | null>(null);
  const [title, setTitle] = useState("");
  const [question, setQuestion] = useState("");
  const [note, setNote] = useState("");
  const [refKind, setRefKind] = useState("reaction");
  const [refId, setRefId] = useState("");
  const [error, setError] = useState("");
  const [workflows, setWorkflows] = useState<TaskWorkflowView[]>([]);
  const [workflow, setWorkflow] = useState<TaskWorkflowDetailView | null>(null);
  const [template, setTemplate] = useState("molecule_modification");
  const [wfParams, setWfParams] = useState("");
  const [report, setReport] = useState<string>("");
  const [reportMsg, setReportMsg] = useState("");
  const [audit, setAudit] = useState<FinalAuditView | null>(null);
  const [auditMsg, setAuditMsg] = useState("");

  const refreshList = () =>
    api
      .listSessions()
      .then((r) => setSessions(r.sessions))
      .catch((e) => setError(String(e)));
  const refreshDetail = (sid: string) =>
    api.sessionDetail(sid).then(setDetail).catch((e) => setError(String(e)));
  const refreshWorkflows = (sid: string) =>
    api.listWorkflows(sid).then((r) => setWorkflows(r.workflows)).catch(() => setWorkflows([]));

  const createWorkflow = async () => {
    if (!id) return;
    setError("");
    let params: Record<string, unknown> = {};
    try {
      if (wfParams.trim()) params = JSON.parse(wfParams);
    } catch {
      setError("params 必须是 JSON，例如 {\"substrate\":\"CCO\"}");
      return;
    }
    try {
      const wf = await api.createWorkflow(id, { template, params });
      refreshWorkflows(id);
      setWorkflow(await api.workflowDetail(wf.workflow_id));
    } catch (e) {
      setError(String(e));
    }
  };

  const openWorkflow = (wid: string) =>
    api.workflowDetail(wid).then(setWorkflow).catch((e) => setError(String(e)));

  const workflowAct = async (action: string) => {
    if (!workflow) return;
    try {
      await api.workflowAction(workflow.workflow.workflow_id, action);
      setWorkflow(await api.workflowDetail(workflow.workflow.workflow_id));
    } catch (e) {
      setError(String(e));
    }
  };

  const generateReport = async () => {
    if (!id) return;
    setReportMsg("");
    try {
      const r = await api.generateReport(id);
      setReport(r.markdown);
    } catch (e) {
      setReportMsg(String(e));
    }
  };

  const runAudit = async () => {
    if (!id) return;
    setAuditMsg("");
    try {
      setAudit(await api.runAudit(id));
    } catch (e) {
      setAuditMsg(String(e));
    }
  };

  const runTask = async (taskId: string) => {
    if (!workflow) return;
    try {
      await api.executeWorkflowTask(workflow.workflow.workflow_id, taskId);
      setWorkflow(await api.workflowDetail(workflow.workflow.workflow_id));
    } catch (e) {
      setError(String(e));
    }
  };

  const skipTask = async (taskId: string) => {
    if (!workflow) return;
    try {
      await api.skipWorkflowTask(workflow.workflow.workflow_id, taskId, "user skipped");
      setWorkflow(await api.workflowDetail(workflow.workflow.workflow_id));
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    refreshList();
  }, []);
  useEffect(() => {
    if (id) {
      refreshDetail(id);
      refreshWorkflows(id);
      setWorkflow(null);
    } else {
      setDetail(null);
    }
  }, [id]);

  const create = async () => {
    if (!title.trim()) return;
    try {
      const s = await api.createSession({ title: title.trim(), question });
      setTitle("");
      setQuestion("");
      refreshList();
      go(`#/sessions/${s.study_id}`);
    } catch (e) {
      setError(String(e));
    }
  };

  const setStatus = async (status: string) => {
    if (!id) return;
    try {
      await api.updateSession(id, { status });
      refreshDetail(id);
      refreshList();
    } catch (e) {
      setError(String(e));
    }
  };

  const addRef = async () => {
    if (!id || !refId.trim()) return;
    try {
      await api.addSessionReference(id, refKind, refId.trim());
      setRefId("");
      refreshDetail(id);
    } catch (e) {
      setError(String(e));
    }
  };

  const removeRef = async (referenceId: string) => {
    if (!id) return;
    try {
      await api.removeSessionReference(id, referenceId);
      refreshDetail(id);
    } catch (e) {
      setError(String(e));
    }
  };

  const addNote = async () => {
    if (!id || !note.trim()) return;
    try {
      await api.addSessionNote(id, note.trim());
      setNote("");
      refreshDetail(id);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="chat-layout">
      <aside className="panel chat-side">
        <h2 style={{ marginTop: 0 }}>Research Sessions</h2>
        <input
          type="text"
          value={title}
          placeholder="新研究 session 标题"
          onChange={(e) => setTitle(e.target.value)}
        />
        <input
          type="text"
          value={question}
          placeholder="主问题（可选）"
          onChange={(e) => setQuestion(e.target.value)}
          style={{ marginTop: 6 }}
        />
        <button onClick={create} disabled={!title.trim()} style={{ marginTop: 6 }}>
          + New Research Session
        </button>
        <ul className="ws-list">
          {sessions.length === 0 && <li className="muted">（还没有 session）</li>}
          {sessions.map((s) => (
            <li key={s.study_id} className={s.study_id === id ? "active" : ""}>
              <a onClick={() => go(`#/sessions/${s.study_id}`)}>{s.title}</a>
              <div className="muted">{s.status}</div>
            </li>
          ))}
        </ul>
      </aside>

      <section className="panel chat-main">
        {error && <p className="error">{error}</p>}
        {!detail && <p className="muted">选择或新建一个 Research Session。</p>}
        {detail && (
          <>
            <div className="row spread">
              <div>
                <h2 style={{ margin: 0 }}>{detail.study.title}</h2>
                <p className="muted" style={{ margin: "4px 0" }}>
                  <span className="badge">{detail.study.status}</span>{" "}
                  {detail.study.question}
                </p>
              </div>
              <div className="row">
                <button className="secondary" onClick={() => setStatus("paused")}>
                  Pause
                </button>
                <button className="secondary" onClick={() => setStatus("completed")}>
                  Complete
                </button>
                <button className="secondary" onClick={() => setStatus("archived")}>
                  Archive
                </button>
              </div>
            </div>

            <h3>References</h3>
            {detail.references.length === 0 ? (
              <p className="muted">（暂无引用）</p>
            ) : (
              <ul>
                {detail.references.map((r) => (
                  <li key={r.reference_id}>
                    <span className="badge">{r.kind}</span>{" "}
                    {r.kind === "molecule" ? (
                      <span className="mono">{r.object_id}</span>
                    ) : r.kind === "reaction" ? (
                      <a onClick={() => go(`#/reactions/${r.object_id}`)}>{r.object_id}</a>
                    ) : r.kind === "literature" ? (
                      <a onClick={() => go(`#/literature/${r.object_id}`)}>{r.object_id}</a>
                    ) : (
                      <span className="mono">{r.object_id}</span>
                    )}{" "}
                    <button className="secondary" onClick={() => removeRef(r.reference_id)}>
                      移除引用
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <div className="row">
              <select value={refKind} onChange={(e) => setRefKind(e.target.value)}>
                <option value="reaction">reaction</option>
                <option value="molecule">molecule</option>
                <option value="literature">literature</option>
                <option value="evidence">evidence</option>
              </select>
              <input
                type="text"
                value={refId}
                placeholder="对象 id（必须已存在）"
                onChange={(e) => setRefId(e.target.value)}
              />
              <button className="secondary" onClick={addRef} disabled={!refId.trim()}>
                Add reference
              </button>
            </div>

            <h3>Research Timeline</h3>
            {detail.events.length === 0 ? (
              <p className="muted">（无事件）</p>
            ) : (
              <ul className="events">
                {detail.events.map((e) => (
                  <li key={e.event_id}>
                    <span className="at">{e.at}</span>
                    <span className="badge">{e.event_type}</span> {e.summary || e.note}
                  </li>
                ))}
              </ul>
            )}
            <div className="row">
              <input
                type="text"
                value={note}
                placeholder="添加笔记…"
                onChange={(e) => setNote(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addNote();
                  }
                }}
              />
              <button className="secondary" onClick={addNote} disabled={!note.trim()}>
                Add note
              </button>
            </div>

            <h3>Artifacts</h3>
            {detail.artifacts.length === 0 ? (
              <p className="muted">（暂无 artifact）</p>
            ) : (
              <ul>
                {detail.artifacts.map((a) => (
                  <li key={a.artifact_id}>
                    <span className="badge">{a.artifact_type}</span> {a.label}
                  </li>
                ))}
              </ul>
            )}

            <h3>Research Report</h3>
            <p className="muted">
              只读整理该 session 已有结果（引用/证据/路线/已保存分析）；不重跑
              化学、检索、Agent 或 LLM；未 经 7.4 审计，不声称已验证。
            </p>
            <div className="row">
              <button className="secondary" onClick={generateReport}>
                Generate Report
              </button>
            </div>
            {reportMsg && <p className="error">{reportMsg}</p>}
            {report && <div className="report">{report}</div>}

            <h3>Final Audit</h3>
            <p className="muted">
              确定性的结构/引用/证据/AI grounding 一致性审计（离线、不修数据、
              不调用 AI）。审计**不是**科学验证。
            </p>
            <div className="row">
              <button className="secondary" onClick={runAudit}>
                Run Final Audit
              </button>
            </div>
            {auditMsg && <p className="error">{auditMsg}</p>}
            {audit && (
              <div style={{ marginTop: 8 }}>
                <p>
                  Status:{" "}
                  <span
                    className={`badge ${
                      audit.status === "fail"
                        ? "conflict"
                        : audit.status === "pass"
                          ? "known"
                          : "partially_known"
                    }`}
                  >
                    {audit.status}
                  </span>{" "}
                  <span className="muted">
                    {audit.summary.passed}/{audit.summary.total_checks} checks ·{" "}
                    {audit.summary.warnings} warning · {audit.summary.failures} failure
                  </span>
                </p>
                <ul>
                  {audit.audit.checks.map((c) => (
                    <li key={c.check_id}>
                      {c.status === "pass" ? "✓" : c.status === "warn" ? "⚠" : "✗"}{" "}
                      {c.title}
                      {c.summary && <span className="muted"> — {c.summary}</span>}
                    </li>
                  ))}
                </ul>
                <p className="muted">{audit.audit.statement}</p>
              </div>
            )}

            <h3>Workflows</h3>
            <div className="row">
              <select value={template} onChange={(e) => setTemplate(e.target.value)}>
                <option value="molecule_modification">molecule_modification</option>
                <option value="route_study">route_study</option>
                <option value="evidence_review">evidence_review</option>
              </select>
              <input
                type="text"
                value={wfParams}
                placeholder='params JSON，例如 {"substrate":"CCO","reaction_template_id":"..."}'
                onChange={(e) => setWfParams(e.target.value)}
              />
              <button className="secondary" onClick={createWorkflow}>
                + New Workflow
              </button>
            </div>
            <ul>
              {workflows.map((w) => (
                <li key={w.workflow_id}>
                  <a onClick={() => openWorkflow(w.workflow_id)}>{w.title}</a>{" "}
                  <span className="badge">{w.status}</span>
                </li>
              ))}
              {workflows.length === 0 && <li className="muted">（暂无 workflow）</li>}
            </ul>

            {workflow && (
              <div>
                <div className="row spread">
                  <strong>{workflow.workflow.title}</strong>
                  <span className="badge">{workflow.workflow.status}</span>
                </div>
                <div className="row" style={{ marginTop: 6 }}>
                  <button className="secondary" onClick={() => workflowAct("start")}>Start</button>
                  <button className="secondary" onClick={() => workflowAct("pause")}>Pause</button>
                  <button className="secondary" onClick={() => workflowAct("resume")}>Resume</button>
                  <button className="cancel" onClick={() => workflowAct("cancel")}>Cancel</button>
                </div>
                <table style={{ marginTop: 8 }}>
                  <thead>
                    <tr>
                      <th>Task</th>
                      <th>Type</th>
                      <th>Status</th>
                      <th>Artifacts</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {workflow.tasks.map((t) => (
                      <tr key={t.task_id}>
                        <td>{t.title}</td>
                        <td className="muted">{t.task_type}</td>
                        <td>
                          <span
                            className={`badge ${t.status === "completed" ? "known" : t.status === "failed" ? "conflict" : ""}`}
                          >
                            {t.status}
                          </span>
                          {t.error && <div className="muted">{t.error}</div>}
                        </td>
                        <td className="muted">{t.output_artifact_ids.length}</td>
                        <td>
                          <button
                            className="secondary"
                            onClick={() => runTask(t.task_id)}
                            disabled={workflow.workflow.status !== "running" || t.status === "completed"}
                          >
                            Execute
                          </button>{" "}
                          <button
                            className="secondary"
                            onClick={() => skipTask(t.task_id)}
                            disabled={t.status === "completed"}
                          >
                            Skip
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
