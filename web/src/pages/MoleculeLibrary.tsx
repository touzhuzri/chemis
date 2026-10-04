import { useEffect, useState } from "react";
import { api } from "../api";
import type { MoleculeRecordView } from "../types";

/** Molecule Library (Stage 2.2, §十七): local records keyed by Standard
 *  InChIKey. Add, list, filter, open (into the Molecule Explorer), delete. */
export default function MoleculeLibrary({
  go,
}: {
  go: (hash: string) => void;
}) {
  const [molecules, setMolecules] = useState<MoleculeRecordView[]>([]);
  const [input, setInput] = useState("");
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");

  const refresh = () =>
    api
      .listMolecules()
      .then((r) => setMolecules(r.molecules))
      .catch((e) => setError(String(e)));

  useEffect(() => {
    refresh();
  }, []);

  const add = async () => {
    const v = input.trim();
    if (!v) return;
    setError("");
    try {
      await api.addMolecule(v);
      setInput("");
      refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const remove = async (id: string) => {
    setError("");
    try {
      await api.deleteMolecule(id);
      refresh();
    } catch (e) {
      setError(String(e));
    }
  };

  const shown = filter.trim()
    ? molecules.filter((m) => {
        const q = filter.trim().toLowerCase();
        return (
          m.canonical_smiles.toLowerCase().includes(q) ||
          m.inchikey.toLowerCase().includes(q) ||
          (m.molecular_formula ?? "").toLowerCase().includes(q)
        );
      })
    : molecules;

  return (
    <>
      <section className="panel">
        <h2>Molecule Library</h2>
        <p className="muted">
          本地分子库，以 Standard InChIKey 为核心化学身份；重复结构不会创建
          第二条记录。来源 = 本地库，不做大规模外部导入。
        </p>
        <div className="row">
          <input
            type="text"
            value={input}
            placeholder="添加分子（SMILES），例如 CCO"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
            }}
          />
          <button onClick={add} disabled={!input.trim()}>
            添加
          </button>
        </div>
        {error && <p className="error">{error}</p>}
      </section>

      <section className="panel">
        <div className="row spread">
          <h2 style={{ margin: 0 }}>Library（{molecules.length}）</h2>
          <input
            type="text"
            value={filter}
            placeholder="筛选 SMILES / InChIKey / Formula"
            onChange={(e) => setFilter(e.target.value)}
            style={{ maxWidth: 320 }}
          />
        </div>
        {shown.length === 0 ? (
          <p className="muted">（库为空）</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Molecule</th>
                <th>Formula</th>
                <th>InChIKey</th>
                <th>Source</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {shown.map((m) => (
                <tr key={m.entity_id}>
                  <td>
                    <a onClick={() => go(`#/molecule/${encodeURIComponent(m.canonical_smiles)}`)}>
                      {m.names[0] ?? m.canonical_smiles}
                    </a>
                    <div className="muted mono">{m.canonical_smiles}</div>
                  </td>
                  <td>{m.molecular_formula ?? "—"}</td>
                  <td className="muted mono">{m.inchikey}</td>
                  <td>
                    <span className="badge">{m.identity_source || "—"}</span>
                  </td>
                  <td>
                    <button
                      className="secondary"
                      onClick={() => go(`#/molecule/${encodeURIComponent(m.canonical_smiles)}`)}
                    >
                      打开
                    </button>{" "}
                    <button className="cancel" onClick={() => remove(m.entity_id)}>
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
