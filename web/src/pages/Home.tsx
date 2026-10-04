import { useEffect, useState } from "react";
import { api } from "../api";
import type { JobView } from "../types";

/** Research Home (§15): one question, one structure, optional PDF. */
export default function Home({ go }: { go: (hash: string) => void }) {
  const [explorerSmiles, setExplorerSmiles] = useState("");
  const [question, setQuestion] = useState("");
  const [kind, setKind] = useState<string>("SMILES");
  const [structure, setStructure] = useState("");
  const [documents, setDocuments] = useState<
    { document_id: string; original_name: string }[]
  >([]);
  const [selectedDoc, setSelectedDoc] = useState<string>("");
  const [jobs, setJobs] = useState<JobView[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const openExplorer = () => {
    const v = explorerSmiles.trim();
    if (v) go(`#/molecule/${encodeURIComponent(v)}`);
  };

  const refresh = () => {
    api.jobs().then((r) => setJobs(r.jobs)).catch(() => {});
    api.documents().then((r) => setDocuments(r.documents)).catch(() => {});
  };
  useEffect(refresh, []);

  const onUpload = async (file: File | undefined) => {
    if (!file) return;
    setError("");
    try {
      const entry = await api.upload(file);
      setSelectedDoc(entry.document_id);
      refresh();
    } catch (exc) {
      setError(String(exc));
    }
  };

  const start = async () => {
    if (!question.trim()) {
      setError("请填写研究问题");
      return;
    }
    // 结构是明确输入，不是可有可无的装饰（§三）：没有结构就明说，
    // 不让用户以为提交了结构而实际走的是无结构分支。
    if (!structure.trim()) {
      const proceed = window.confirm(
        "未输入化学结构。将以“无结构模式”研究（Identity 将为 unresolved）。\n\n" +
          "如果这是明确结构研究（SMILES / InChI / Name），请取消并填写结构。",
      );
      if (!proceed) return;
    }
    setBusy(true);
    setError("");
    try {
      const job = await api.startResearch({
        question: question.trim(),
        structure_kind: structure.trim() ? kind : null,
        structure_value: structure.trim(),
        document_ids: selectedDoc ? [selectedDoc] : [],
      });
      go(`#/research/${job.job_id}`);
    } catch (exc) {
      setError(String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <section className="panel">
        <h2>Molecule Explorer</h2>
        <p className="muted">
          输入一个分子，系统性地展示它的结构、性质、官能团、骨架、片段与已知反应——
          全部由确定性化学计算完成，不需要 AI。
        </p>
        <div className="row">
          <input
            type="text"
            value={explorerSmiles}
            placeholder="SMILES，例如 CCO / c1ccccc1 / C#Cc1ccccc1"
            onChange={(e) => setExplorerSmiles(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                openExplorer();
              }
            }}
          />
          <button onClick={openExplorer} disabled={!explorerSmiles.trim()}>
            打开 Molecule Explorer
          </button>
          <button className="secondary" onClick={() => go("#/library")}>
            Molecule Library
          </button>
          <button className="secondary" onClick={() => go("#/reactions")}>
            Reaction Explorer
          </button>
          <button className="secondary" onClick={() => go("#/literature")}>
            Literature Search
          </button>
          <button className="secondary" onClick={() => go("#/sessions")}>
            Research Sessions
          </button>
        </div>
      </section>

      <section className="panel">
        <h2>CREA — Chemical Research Workbench</h2>
        <p className="muted">
          输入研究问题与化学结构，可选上传自己的 PDF。研究过程中的身份、知识、
          反应、证据、路线、缺口与冲突都可观察；完成后知识进入 KnowledgeBase，
          下次研究自动复用。
        </p>
        <label>研究问题</label>
        <textarea
          value={question}
          placeholder="例如：研究 1,4-diphenyl-1,3-diyne 的文献合成方法"
          onChange={(e) => setQuestion(e.target.value)}
        />
        <div className="grid">
          <div>
            <label>化学结构（SMILES / InChI / Name）</label>
            <div className="row">
              <select value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 110 }}>
                <option value="SMILES">SMILES</option>
                <option value="INCHI">InChI</option>
                <option value="NAME">Name</option>
              </select>
              <input
                type="text"
                value={structure}
                placeholder="C#Cc1ccccc1"
                onChange={(e) => setStructure(e.target.value)}
              />
            </div>
          </div>
          <div>
            <label>上传 PDF（可选）</label>
            <input
              type="file"
              accept="application/pdf,.pdf"
              onChange={(e) => onUpload(e.target.files?.[0])}
            />
            {documents.length > 0 && (
              <>
                <label>或选择已上传文档</label>
                <select
                  value={selectedDoc}
                  onChange={(e) => setSelectedDoc(e.target.value)}
                >
                  <option value="">（不使用文档）</option>
                  {documents.map((d) => (
                    <option key={d.document_id} value={d.document_id}>
                      {d.original_name} — {d.document_id.slice(0, 12)}…
                    </option>
                  ))}
                </select>
              </>
            )}
          </div>
        </div>
        {error && <p className="error">{error}</p>}
        <div className="row" style={{ marginTop: 14 }}>
          <button onClick={start} disabled={busy}>
            启动研究
          </button>
          <span className="muted">研究在后台运行，页面不会被阻塞</span>
        </div>
      </section>

      <div className="grid">
        <section className="panel">
          <h2>Recent Research</h2>
          {jobs.length === 0 && <p className="muted">还没有研究记录</p>}
          <table>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.job_id}>
                  <td>
                    <a onClick={() => go(`#/research/${j.job_id}`)}>{j.question}</a>
                  </td>
                  <td>
                    <span className={`badge ${j.status === "completed" ? "known" : ""}`}>
                      {j.status}
                    </span>
                  </td>
                  <td className="muted">{j.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="panel">
          <h2>Knowledge Base</h2>
          <p className="muted">
            浏览已建立的化学知识：Entity、Reaction、Evidence、Route，
            以及它们之间的 provenance 链。
          </p>
          <button className="secondary" onClick={() => go("#/knowledge/entity/")}>
            打开知识库浏览
          </button>
        </section>
        <section className="panel">
          <h2>Chemistry Workspace</h2>
          <p className="muted">
            与 ChemistryAgent 持续对话——自然语言提问，Agent 调用真实化学工具，
            回答附带 evidence / provenance。
          </p>
          <button className="secondary" onClick={() => go("#/chat")}>
            打开对话工作区
          </button>
        </section>
      </div>
    </>
  );
}
