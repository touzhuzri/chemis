import { useEffect, useState } from "react";
import { api } from "../api";
import type { ToolCallView, WorkspaceMessageView, WorkspaceView } from "../types";

/** Chemistry Workspace (§12-§18): a conversation with the ChemistryAgent.
 *  Left = workspace list, right = chat. Evidence/provenance stay visible
 *  (§16), loading is explicit (§18), and error states are not flattened (§17). */
export default function ChatPage({
  id,
  go,
}: {
  id: string;
  go: (hash: string) => void;
}) {
  const [workspaces, setWorkspaces] = useState<WorkspaceView[]>([]);
  const [messages, setMessages] = useState<WorkspaceMessageView[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const refreshWorkspaces = () =>
    api.workspaces().then((r) => setWorkspaces(r.workspaces)).catch((e) => setError(String(e)));
  const refreshMessages = (wid: string) =>
    api.workspaceMessages(wid).then((r) => setMessages(r.messages)).catch((e) => setError(String(e)));

  useEffect(() => {
    refreshWorkspaces();
  }, []);
  useEffect(() => {
    if (id) {
      setMessages([]);
      refreshMessages(id);
    } else {
      setMessages([]);
    }
  }, [id]);

  const create = async () => {
    setError("");
    try {
      const ws = await api.createWorkspace("");
      refreshWorkspaces();
      go(`#/chat/${ws.workspace_id}`);
    } catch (e) {
      setError(String(e));
    }
  };

  const send = async () => {
    const content = input.trim();
    if (!content || !id || busy) return;
    setBusy(true);
    setError("");
    setInput("");
    // optimistic user bubble; the authoritative copy arrives on refresh
    setMessages((prev) => [
      ...prev,
      {
        message_id: `pending-${Date.now()}`,
        workspace_id: id,
        role: "user",
        content,
        created_at: "",
        tool_calls: [],
        evidence: [],
        provenance: [],
        diagnostics: [],
      },
    ]);
    try {
      await api.sendMessage(id, content);
      await refreshMessages(id);
    } catch (e) {
      setError(String(e));
      refreshMessages(id);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="chat-layout">
      <aside className="panel chat-side">
        <div className="row spread">
          <h2 style={{ margin: 0 }}>工作区</h2>
          <button className="secondary" onClick={create}>
            + 新建工作区
          </button>
        </div>
        <ul className="ws-list">
          {workspaces.length === 0 && <li className="muted">（还没有工作区）</li>}
          {workspaces.map((w) => (
            <li key={w.workspace_id} className={w.workspace_id === id ? "active" : ""}>
              <a onClick={() => go(`#/chat/${w.workspace_id}`)}>{w.title || "新建工作区"}</a>
            </li>
          ))}
        </ul>
      </aside>

      <section className="panel chat-main">
        <h2>Chemistry Agent</h2>
        {!id && (
          <p className="muted">
            选择或新建一个工作区，与 ChemistryAgent 对话——回答附带
            evidence / provenance。
          </p>
        )}
        <div className="chat-messages">
          {messages.map((m, i) => (
            <Message key={m.message_id || i} m={m} />
          ))}
          {busy && <div className="muted chat-typing">正在思考…</div>}
        </div>
        {error && <p className="error">{error}</p>}
        <div className="chat-input">
          <input
            type="text"
            value={input}
            disabled={!id || busy}
            placeholder="输入化学问题，例如：苯乙炔有哪些已知反应？"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
          />
          <button onClick={send} disabled={!id || busy || !input.trim()}>
            发送
          </button>
        </div>
      </section>
    </div>
  );
}

function Message({ m }: { m: WorkspaceMessageView }) {
  const isUser = m.role === "user";
  const hasDetails =
    m.tool_calls.length > 0 ||
    m.evidence.length > 0 ||
    m.provenance.length > 0 ||
    m.diagnostics.length > 0;

  return (
    <div className={`chat-msg ${isUser ? "user" : "assistant"}`}>
      <div className="chat-role">{isUser ? "用户" : "CREA"}</div>
      <div className="chat-content">{m.content || "（无内容）"}</div>
      {!isUser && hasDetails && (
        <details className="chat-details">
          <summary>工具调用 / Evidence / Provenance</summary>
          {m.diagnostics.length > 0 && (
            <div className="note">{m.diagnostics.join("；")}</div>
          )}
          {m.tool_calls.map((t, i) => (
            <ToolCall key={i} t={t} />
          ))}
          {m.evidence.length > 0 && (
            <div className="chat-tool">
              <span className="muted">Evidence：</span>
              {m.evidence.map((e, i) => (
                <span key={i} className="badge known">
                  {String((e as { evidence_id?: string }).evidence_id ?? JSON.stringify(e))}
                </span>
              ))}
            </div>
          )}
        </details>
      )}
    </div>
  );
}

function ToolCall({ t }: { t: ToolCallView }) {
  return (
    <div className="chat-tool">
      <span
        className={`badge ${
          t.status === "unavailable" ? "conflict" : t.status === "ok" ? "known" : ""
        }`}
      >
        {t.tool}
      </span>{" "}
      <span className="muted">{t.status}</span>
      {t.status === "unavailable" && (
        <div className="muted">当前数据源不可用，无法完成查询。</div>
      )}
      {t.status === "error" && t.error && <div className="muted">error: {t.error}</div>}
      {t.tool === "explore_chemical_space" && <ExplorationView data={t.data} />}
      {t.tool === "find_chemical_transformations" && (
        <TransformationView data={t.data} />
      )}
    </div>
  );
}

/** A transformation-evidence result as a candidate -> reaction -> target card
 *  (§19). Uses no green/red feasibility colouring -- evidence status only. */
function TransformationView({ data }: { data: unknown }) {
  const d = data as {
    status?: string;
    matched_reactions?: string[];
    transformations?: {
      transformation_id?: string;
      direction?: string;
      evidence_level?: string;
      reaction?: { reaction_id?: string };
    }[];
    diagnostics?: string[];
  };
  if (d?.status === "transformation_search_unavailable") {
    return <div className="muted">Transformation search unavailable.（无法查询）</div>;
  }
  if (d?.status === "no_transformation_evidence") {
    return (
      <div className="muted">
        No direct transformation evidence found.（当前数据源没有找到直接转化证据——不等于该转化不存在）
      </div>
    );
  }
  if (d?.status === "related_transformation") {
    return (
      <div className="muted">
        Related reaction history（{d.matched_reactions?.join(", ") ?? "无"}），无直接转化。
      </div>
    );
  }
  if (!d?.transformations?.length) return null;
  return (
    <div className="explore">
      {d.transformations.map((t, i) => (
        <div key={i} className="explore-candidate">
          <span className="badge known">Direct transformation</span>{" "}
          <span className="muted">
            {t.reaction?.reaction_id} · {t.direction} · {t.evidence_level}
          </span>
          <div className="muted">
            候选 ──{t.reaction?.reaction_id}──→ 目标（已记录反应证据，非可行性判断）
          </div>
        </div>
      ))}
    </div>
  );
}

/** A chemical-space exploration result as candidate cards (§21/§36). */
function ExplorationView({ data }: { data: unknown }) {
  const d = data as {
    target?: { canonical_smiles?: string; entity_id?: string };
    candidates?: {
      entity?: { canonical_smiles?: string; entity_id?: string };
      relationship?: string;
      similarity?: number | null;
      scaffold_relation?: string;
      reaction_evidence?: string;
    }[];
    source?: string;
  };
  if (!d?.candidates?.length) {
    return <div className="muted">没有找到候选结构。</div>;
  }
  return (
    <div className="explore">
      <div className="muted">
        目标 {d.target?.canonical_smiles ?? d.target?.entity_id ?? ""} · 来源
        {" "}
        {d.source ?? "?"}
      </div>
      {d.candidates.map((c, i) => (
        <div key={i} className="explore-candidate">
          <span className="badge">{c.relationship}</span>{" "}
          <span className="mono">{c.entity?.canonical_smiles ?? c.entity?.entity_id}</span>
          <div className="muted">
            similarity {c.similarity ?? "—"} · scaffold {c.scaffold_relation} ·
            reaction {c.reaction_evidence}
          </div>
        </div>
      ))}
    </div>
  );
}
