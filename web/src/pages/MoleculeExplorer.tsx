import { useEffect, useState } from "react";
import { api } from "../api";
import MoleculeViewer from "../components/MoleculeViewer";
import ReactionGraphViewSvg from "../components/ReactionGraphView";
import type {
  AgentAnswerView,
  ConformerView,
  LibraryHitView,
  MoleculeInspectionView,
  PubChemResultView,
  ModificationAnalysisView,
  ReactionComparisonView,
  ReactionGraphView,
  RouteReasoningView,
  RouteSearchView,
  SimilarityView,
  TransformationSearchView,
} from "../types";

/** Molecule Explorer (§二/§十): SMILES -> deterministic chemistry.
 *  No AI. Structure, properties, functional groups, scaffold, fragments,
 *  reactions and a molecule-vs-molecule similarity panel. */
export default function MoleculeExplorer({
  smiles,
  go,
}: {
  smiles: string;
  go: (hash: string) => void;
}) {
  const [input, setInput] = useState(smiles || "");
  const [result, setResult] = useState<MoleculeInspectionView | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [compareSmiles, setCompareSmiles] = useState("");
  const [similarity, setSimilarity] = useState<SimilarityView | null>(null);
  const [conformer, setConformer] = useState<ConformerView | null>(null);
  const [conformerStatus, setConformerStatus] = useState<
    "idle" | "generating" | "success" | "failed"
  >("idle");
  const [libraryHits, setLibraryHits] = useState<LibraryHitView[] | null>(null);
  const [target, setTarget] = useState("");
  const [transformations, setTransformations] = useState<TransformationSearchView | null>(null);
  const [selectedForCompare, setSelectedForCompare] = useState<string[]>([]);
  const [comparison, setComparison] = useState<ReactionComparisonView | null>(null);
  const [graph, setGraph] = useState<ReactionGraphView | null>(null);
  const [graphMsg, setGraphMsg] = useState("");
  const [routeTarget, setRouteTarget] = useState("");
  const [routes, setRoutes] = useState<RouteSearchView | null>(null);
  const [routeMsg, setRouteMsg] = useState("");
  const [reasonQuery, setReasonQuery] = useState("");
  const [reasoning, setReasoning] = useState<RouteReasoningView | null>(null);
  const [reasonMsg, setReasonMsg] = useState("");
  const [modQuery, setModQuery] = useState("");
  const [modTarget, setModTarget] = useState("");
  const [modification, setModification] = useState<ModificationAnalysisView | null>(null);
  const [modMsg, setModMsg] = useState("");
  const [agentQuery, setAgentQuery] = useState("");
  const [agentResult, setAgentResult] = useState<AgentAnswerView | null>(null);
  const [agentMsg, setAgentMsg] = useState("");
  const [pubchemResult, setPubchemResult] = useState<PubChemResultView | null>(null);
  const [pubchemStatus, setPubchemStatus] = useState<
    "idle" | "loading" | "success" | "notfound" | "unavailable" | "error"
  >("idle");
  const [pubchemMessage, setPubchemMessage] = useState("");

  const inspect = (value: string) => {
    const v = value.trim();
    if (!v) return;
    setBusy(true);
    setError("");
    setConformer(null);
    setConformerStatus("idle");
    api
      .inspectMolecule(v)
      .then((r) => setResult(r))
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  };

  const generate3d = () => {
    if (!result?.smiles) return;
    setConformerStatus("generating");
    api
      .generateConformer(result.smiles)
      .then((c) => {
        setConformer(c);
        setConformerStatus("success");
      })
      .catch(() => setConformerStatus("failed"));
  };

  useEffect(() => {
    if (smiles) {
      setInput(smiles);
      inspect(smiles);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [smiles]);

  const compare = () => {
    const b = compareSmiles.trim();
    if (!b || !result) return;
    api
      .compareMolecules(result.smiles, b)
      .then(setSimilarity)
      .catch((e) => setError(String(e)));
  };

  const searchLibrary = () => {
    if (!result?.smiles) return;
    api
      .searchSimilarMolecules(result.smiles)
      .then((r) => setLibraryHits(r.hits))
      .catch((e) => setError(String(e)));
  };

  const searchTransformations = () => {
    if (!result?.smiles) return;
    api
      .transformationSearch(result.smiles, target.trim())
      .then(setTransformations)
      .catch((e) => setError(String(e)));
  };

  const toggleCompare = (templateId: string) => {
    setSelectedForCompare((prev) =>
      prev.includes(templateId)
        ? prev.filter((id) => id !== templateId)
        : [...prev, templateId],
    );
  };

  const compareSelected = () => {
    if (selectedForCompare.length < 2) return;
    api
      .compareReactions(selectedForCompare, result?.smiles ?? "", target.trim())
      .then(setComparison)
      .catch((e) => setError(String(e)));
  };

  const buildGraph = async () => {
    if (!result?.smiles) return;
    setGraphMsg("");
    try {
      // the graph is over stored molecules: ensure this one has a record
      const added = await api.addMolecule(result.smiles);
      const g = await api.moleculeGraph(added.molecule.entity_id, 2);
      setGraph(g);
    } catch (e) {
      setGraphMsg(String(e));
    }
  };

  const exploreRoutes = () => {
    if (!result?.smiles || !routeTarget.trim()) return;
    setRouteMsg("");
    api
      .searchRoutes(result.smiles, routeTarget.trim(), 3)
      .then(setRoutes)
      .catch((e) => setRouteMsg(String(e)));
  };

  const reasonRoutes = () => {
    if (!result?.smiles || !routeTarget.trim() || !reasonQuery.trim()) return;
    setReasonMsg("");
    api
      .reasonRoutes(result.smiles, routeTarget.trim(), reasonQuery.trim(), 3)
      .then((r) => setReasoning(r.reasoning))
      .catch((e) => setReasonMsg(String(e)));
  };

  const runAgent = () => {
    if (!result?.smiles || !agentQuery.trim()) return;
    setAgentMsg("");
    api
      .runAgent(agentQuery.trim(), result.smiles)
      .then((r) => setAgentResult(r.answer))
      .catch((e) => setAgentMsg(String(e)));
  };

  const runModification = () => {
    if (!result?.smiles || !modQuery.trim()) return;
    setModMsg("");
    api
      .modifyAnalysis(result.smiles, modQuery.trim(), modTarget.trim())
      .then((r) => setModification(r.analysis))
      .catch((e) => setModMsg(String(e)));
  };

  const searchPubchem = () => {
    if (!result?.smiles) return;
    setPubchemStatus("loading");
    setPubchemMessage("");
    api
      .pubchemLookup(result.smiles)
      .then((r) => {
        setPubchemResult(r);
        setPubchemStatus("success");
      })
      .catch((e) => {
        setPubchemResult(null);
        const message = String(e);
        setPubchemStatus(
          message.startsWith("PROVIDER_NOT_FOUND")
            ? "notfound"
            : message.startsWith("PROVIDER_UNAVAILABLE")
              ? "unavailable"
              : "error",
        );
      });
  };

  const importPubchem = () => {
    if (!result?.smiles) return;
    setPubchemMessage("");
    api
      .pubchemImport(result.smiles)
      .then(() => setPubchemMessage("已导入到本地分子库。"))
      .catch((e) => setPubchemMessage(String(e)));
  };

  const p = result?.properties ?? {};

  return (
    <>
      <section className="panel">
        <h2>Molecule Explorer</h2>
        <div className="row">
          <input
            type="text"
            value={input}
            placeholder="输入 SMILES，例如 CCO"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                go(`#/molecule/${encodeURIComponent(input.trim())}`);
              }
            }}
          />
          <button
            onClick={() => go(`#/molecule/${encodeURIComponent(input.trim())}`)}
            disabled={busy || !input.trim()}
          >
            {busy ? "分析中…" : "分析"}
          </button>
        </div>
        {error && <p className="error">{error}</p>}
        {result && !result.valid && (
          <p className="error">无法解析结构：{result.error}</p>
        )}
      </section>

      {result?.valid && (
        <>
          <div className="grid">
            <section className="panel">
              <h2>Molecule</h2>
              <div className="mol">
                <img src={api.moleculeSvgUrl(result.smiles)} alt="2D structure" />
              </div>
              <dl className="kv" style={{ marginTop: 10 }}>
                <dt>Formula</dt>
                <dd>{String(p.molecular_formula ?? "")}</dd>
                <dt>Weight</dt>
                <dd>{String(p.molecular_weight ?? "")}</dd>
                <dt>Exact Weight</dt>
                <dd>{String(p.exact_molecular_weight ?? "")}</dd>
                <dt>Formal Charge</dt>
                <dd>{String(p.formal_charge ?? "")}</dd>
                <dt>Atom Count</dt>
                <dd>{String(p.atom_count ?? "")}</dd>
                <dt>Heavy Atoms</dt>
                <dd>{String(p.heavy_atom_count ?? "")}</dd>
                <dt>Rings</dt>
                <dd>{String(p.ring_count ?? "")}</dd>
                <dt>Aromatic Rings</dt>
                <dd>{String(p.aromatic_ring_count ?? "")}</dd>
              </dl>
            </section>

            <section className="panel">
              <h2>Structure</h2>
              <dl className="kv">
                <dt>SMILES</dt>
                <dd>{result.structure.input_smiles}</dd>
                <dt>Canonical SMILES</dt>
                <dd>{result.structure.canonical_smiles}</dd>
                <dt>InChI</dt>
                <dd>{result.structure.inchi}</dd>
                <dt>InChIKey</dt>
                <dd>{result.structure.inchikey}</dd>
              </dl>
            </section>
          </div>

          <section className="panel">
            <h2>3D Structure</h2>
            <p className="muted">
              Generated conformer（ETKDG）——由算法生成的一个合理构象，不是实验
              测得的真实三维形状。点击才生成，避免页面初始化时无条件计算。
            </p>
            {conformerStatus === "idle" && (
              <button onClick={generate3d}>Generate 3D</button>
            )}
            {conformerStatus === "generating" && (
              <p className="muted">Generating conformer…</p>
            )}
            {conformerStatus === "failed" && (
              <div>
                <p className="error">3D conformer generation failed.</p>
                <button onClick={generate3d}>Retry</button>
              </div>
            )}
            {conformerStatus === "success" && conformer && (
              <div>
                <MoleculeViewer conformer={conformer} />
                <p className="muted" style={{ marginTop: 8 }}>
                  {conformer.generation_method}
                  {conformer.optimized
                    ? ` · ${conformer.optimization_method} optimized`
                    : " · 未优化"}
                </p>
                <div className="row">
                  <button className="secondary" onClick={generate3d}>
                    Regenerate
                  </button>
                </div>
              </div>
            )}
          </section>

          <div className="grid">
            <section className="panel">
              <h2>Properties</h2>
              <dl className="kv">
                <dt>HBD</dt>
                <dd>{String(p.hbd ?? "")}</dd>
                <dt>HBA</dt>
                <dd>{String(p.hba ?? "")}</dd>
                <dt>TPSA</dt>
                <dd>{String(p.tpsa ?? "")}</dd>
                <dt>LogP</dt>
                <dd>{String(p.logp ?? "")}</dd>
                <dt>Rotatable Bonds</dt>
                <dd>{String(p.rotatable_bonds ?? "")}</dd>
                <dt>Fraction Csp3</dt>
                <dd>{String(p.fraction_csp3 ?? "")}</dd>
                <dt>DBE</dt>
                <dd>{String(p.dbe ?? "")}</dd>
              </dl>
            </section>

            <section className="panel">
              <h2>Scaffold</h2>
              <p className="muted">{result.scaffold.note}</p>
              <dl className="kv">
                <dt>Murcko Scaffold</dt>
                <dd>{result.scaffold.murcko_smiles || "（无环骨架）"}</dd>
                {result.scaffold.ring_systems.length > 0 && (
                  <>
                    <dt>Ring Systems</dt>
                    <dd>{result.scaffold.ring_systems.join(", ")}</dd>
                  </>
                )}
              </dl>
            </section>
          </div>

          <div className="grid">
            <section className="panel">
              <h2>Functional Groups</h2>
              {result.functional_groups.length === 0 ? (
                <p className="muted">（未检测到命名官能团）</p>
              ) : (
                <table>
                  <tbody>
                    {result.functional_groups.map((g) => (
                      <tr key={g.name}>
                        <td>{g.name}</td>
                        <td className="muted">× {g.count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>

            <section className="panel">
              <h2>Fragments</h2>
              {result.fragments.length === 0 ? (
                <p className="muted">（无片段）</p>
              ) : (
                <table>
                  <tbody>
                    {result.fragments.map((f) => (
                      <tr key={f.fragment_id}>
                        <td className="mono">{f.fragment_smiles}</td>
                        <td className="muted">{f.formula}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          </div>

          <section className="panel">
            <h2>Reactions（结构/官能团匹配的基础反应）</h2>
            <p className="muted">
              「可能发生」= 模板底物模式与当前结构匹配，不代表 AI 判断该反应一定能做。
            </p>
            {result.reactions.length === 0 ? (
              <p className="muted">当前结构未匹配任何种子反应模板。</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Reaction</th>
                    <th>Class</th>
                    <th>Transformation</th>
                    <th>Typical Conditions</th>
                  </tr>
                </thead>
                <tbody>
                  {result.reactions.map((m) => (
                    <tr key={m.template.template_id}>
                      <td>
                        <a
                          onClick={() =>
                            go(
                              `#/reactions/${m.template.template_id}/${encodeURIComponent(result.smiles)}`,
                            )
                          }
                        >
                          {m.template.name}
                        </a>
                        <div className="muted">
                          <a
                            onClick={() =>
                              go(
                                `#/reactions/${m.template.template_id}/${encodeURIComponent(result.smiles)}`,
                              )
                            }
                          >
                            View Reaction
                          </a>
                        </div>
                        {m.template.mechanism_class && (
                          <div className="muted">{m.template.mechanism_class}</div>
                        )}
                      </td>
                      <td>
                        <span className="badge">{m.template.class_family}</span>{" "}
                        <span className="muted">{m.template.reaction_class}</span>
                      </td>
                      <td>{m.template.transformation.note}</td>
                      <td className="muted">
                        {m.template.conditions.map((c) => c.value).join("；")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section className="panel">
            <h2>Similarity（与另一个分子比较）</h2>
            <div className="row">
              <input
                type="text"
                value={compareSmiles}
                placeholder="另一个 SMILES，例如 Cc1ccccc1"
                onChange={(e) => setCompareSmiles(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    compare();
                  }
                }}
              />
              <button onClick={compare} disabled={!compareSmiles.trim()}>
                比较
              </button>
            </div>
            {similarity && (
              <dl className="kv" style={{ marginTop: 10 }}>
                <dt>Tanimoto</dt>
                <dd>{similarity.tanimoto}</dd>
                <dt>Dice</dt>
                <dd>{similarity.dice}</dd>
                <dt>Scaffold Relation</dt>
                <dd>{similarity.scaffold_relation}</dd>
                <dt>Common Atoms</dt>
                <dd>{similarity.mcs.common_atoms}</dd>
                <dt>Common Substructure</dt>
                <dd>{similarity.mcs.common_substructure_smiles || "—"}</dd>
                <dt>Note</dt>
                <dd className="muted">{similarity.diagnostics.join("；")}</dd>
              </dl>
            )}
          </section>

          <section className="panel">
            <h2>Similarity — Search Local Library</h2>
            <div className="row">
              <button onClick={searchLibrary}>在当前分子库中搜索相似分子</button>
            </div>
            {libraryHits !== null &&
              (libraryHits.length === 0 ? (
                <p className="muted">分子库中没有候选分子（或库为空）。</p>
              ) : (
                <table style={{ marginTop: 10 }}>
                  <thead>
                    <tr>
                      <th>Molecule</th>
                      <th>Similarity</th>
                      <th>InChIKey</th>
                      <th>Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    {libraryHits.map((h) => (
                      <tr key={h.entity.entity_id}>
                        <td>
                          <a onClick={() => go(`#/molecule/${encodeURIComponent(h.entity.canonical_smiles)}`)}>
                            {h.entity.names[0] ?? h.entity.canonical_smiles}
                          </a>
                        </td>
                        <td>{h.score}</td>
                        <td className="muted mono">{h.entity.inchikey}</td>
                        <td>
                          <span className="badge">{h.entity.identity_source}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ))}
          </section>

          <section className="panel">
            <h2>Transformations — Candidate Reactions</h2>
            <p className="muted">
              substrate → 候选 ReactionTemplate（SMARTS 匹配）。可输入 target 检查
              transformation match（当前库无 product pattern 的模板只能到
              substrate match）。匹配 ≠ 反应一定成功；不联网、不使用 AI。
            </p>
            <div className="row">
              <input
                type="text"
                value={target}
                placeholder="target SMILES（可选），例如 CCCl"
                onChange={(e) => setTarget(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    searchTransformations();
                  }
                }}
              />
              <button onClick={searchTransformations}>Search Transformations</button>
            </div>
            {transformations && (
              <>
                <table style={{ marginTop: 10 }}>
                  <thead>
                    <tr>
                      <th></th>
                      <th>Reaction Template</th>
                      <th>Match</th>
                      <th>Transformation</th>
                      <th>Evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {transformations.candidates.map((c) => (
                      <tr key={c.reaction_template.template_id}>
                        <td>
                          <input
                            type="checkbox"
                            checked={selectedForCompare.includes(c.reaction_template.template_id)}
                            onChange={() => toggleCompare(c.reaction_template.template_id)}
                          />
                        </td>
                        <td>
                          <a
                            onClick={() =>
                              go(
                                `#/reactions/${c.reaction_template.template_id}/${encodeURIComponent(result.smiles)}`,
                              )
                            }
                          >
                            {c.reaction_template.name}
                          </a>
                        </td>
                        <td>
                          <span
                            className={`badge ${c.match_level === "transformation_match" ? "known" : ""}`}
                          >
                            {c.match_level === "transformation_match"
                              ? "Transformation match"
                              : "Substrate match"}
                          </span>
                        </td>
                        <td>{c.transformation.note}</td>
                        <td className="muted">{c.evidence_count}</td>
                      </tr>
                    ))}
                    {transformations.candidates.length === 0 && (
                      <tr>
                        <td colSpan={5} className="muted">
                          No matching reaction templates found.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
                <div className="row" style={{ marginTop: 8 }}>
                  <button
                    className="secondary"
                    onClick={compareSelected}
                    disabled={selectedForCompare.length < 2}
                  >
                    Compare selected ({selectedForCompare.length})
                  </button>
                  <span className="muted">选择至少两个候选以比较已知事实</span>
                </div>
              </>
            )}
          </section>

          {comparison && (
            <section className="panel">
              <h2>Reaction Comparison</h2>
              <p className="muted">
                并排显示已知反应事实；**不**判断最佳反应、**不**预测成功率/产率。
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Dimension</th>
                    {comparison.entries.map((e) => (
                      <th key={e.template_id}>
                        <a onClick={() => go(`#/reactions/${e.template_id}`)}>{e.name}</a>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(
                    [
                      ["Class", (e) => e.class_family],
                      ["Transformation", (e) => e.transformation.note],
                      ["Substrate", (e) =>
                        e.substrate_compatibility
                          ? `${e.substrate_compatibility} (${e.matched_substrate_atoms} atoms)`
                          : "Not evaluated"],
                      ["Reagents", (e) => e.reagents.join("; ") || "Not specified"],
                      ["Conditions", (e) => e.conditions.join("; ") || "Not specified"],
                      ["Mechanism", (e) => e.mechanism_class || "Not specified"],
                      ["Regioselectivity", (e) => e.selectivity.regioselectivity || "Not specified"],
                      ["Stereoselectivity", (e) => e.selectivity.stereoselectivity || "Not specified"],
                      ["Limitations", (e) => e.limitations.join("; ") || "—"],
                      ["Evidence", (e) => `${e.evidence_count}`],
                      ["Literature", (e) => `${e.literature.length} linked`],
                      ["Yield", () => "Not available"],
                    ] as [string, (e: ReactionComparisonView["entries"][number]) => string][]
                  ).map(([label, fn]) => (
                    <tr key={label}>
                      <td className="muted">{label}</td>
                      {comparison.entries.map((e) => (
                        <td key={e.template_id}>{fn(e)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              {comparison.entries.some((e) => e.literature.length > 0) && (
                <div style={{ marginTop: 10 }}>
                  <h3>Linked literature</h3>
                  {comparison.entries.flatMap((e) =>
                    e.literature.map((lit, i) => (
                      <div key={`${e.template_id}-${i}`} className="muted">
                        [{lit.year ?? "—"}] {lit.title || lit.doi}
                        {lit.paper_id && (
                          <>
                            {" "}
                            <a onClick={() => go(`#/literature/${encodeURIComponent(lit.paper_id)}`)}>
                              View Literature
                            </a>
                          </>
                        )}
                      </div>
                    )),
                  )}
                </div>
              )}
            </section>
          )}

          <section className="panel">
            <h2>Reaction Graph</h2>
            <p className="muted">
              Molecule ── SUBSTRATE_OF ── Reaction ── SUPPORTED_BY ── Literature。
              仅表达已有关系（匹配 ≠ 反应会发生）；不猜产物（当前无 product
              pattern）、不做路线规划。
            </p>
            <div className="row">
              <button className="secondary" onClick={buildGraph}>
                Build Reaction Graph
              </button>
            </div>
            {graphMsg && <p className="error">{graphMsg}</p>}
            {graph && (
              <div style={{ marginTop: 10 }}>
                <ReactionGraphViewSvg graph={graph} go={go} />
                <p className="muted">{graph.diagnostics.join("；")}</p>
              </div>
            )}
          </section>

          <section className="panel">
            <h2>Route Explorer</h2>
            <p className="muted">
              仅做**前向**路线探索：start → 已知反应 → 确定产物 → … → target。
              每步产物必须来自明确的 reaction transform（当前库无 transform，
              因此不会有完整路线——这是诚实的，不是错误）。不做逆合成、不猜产物、
              不预测成功率/产率。
            </p>
            <div className="row">
              <input
                type="text"
                value={routeTarget}
                placeholder="target SMILES，例如 CC=O"
                onChange={(e) => setRouteTarget(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    exploreRoutes();
                  }
                }}
              />
              <button onClick={exploreRoutes} disabled={!routeTarget.trim()}>
                Explore Routes
              </button>
            </div>
            {routeMsg && <p className="error">{routeMsg}</p>}
            {routes && (
              <div style={{ marginTop: 10 }}>
                <p className="muted">
                  {routes.routes.length} complete · {routes.partial_routes.length} partial ·{" "}
                  {routes.blocked_paths.length} blocked
                </p>
                {routes.routes.length === 0 && routes.partial_routes.length === 0 &&
                  routes.blocked_paths.length === 0 && (
                    <p className="muted">No deterministic route found.</p>
                  )}
                {[...routes.routes, ...routes.partial_routes, ...routes.blocked_paths].map(
                  (route) => (
                    <div key={route.route_id} className="reaction-card" style={{ marginBottom: 10 }}>
                      <div className="row spread">
                        <strong>Route {route.status}</strong>
                        <span className="muted">
                          start {route.start_smiles} → target {route.target_smiles}
                        </span>
                      </div>
                      {route.steps.map((s) => (
                        <div key={s.step_index} className="muted" style={{ marginTop: 4 }}>
                          {s.step_index}.{" "}
                          <span className="mono">{s.input_smiles}</span> →{" "}
                          <a onClick={() => go(`#/reactions/${s.reaction_template_id}`)}>
                            {s.reaction_name}
                          </a>{" "}
                          → <span className="mono">{s.output_smiles}</span>{" "}
                          <span className="badge">{s.evidence_count} evidence</span>
                        </div>
                      ))}
                      {route.blocked_reason && (
                        <div className="error">
                          {route.blocked_reason}
                          {route.blocked_reaction_id ? ` (${route.blocked_reaction_id})` : ""}
                        </div>
                      )}
                    </div>
                  ),
                )}
              </div>
            )}

            <h3>Route Reasoning</h3>
            <p className="muted">
              AI 仅解释**确定性 explorer 找到的**路线（比较、指出缺口）；不新增
              路线/中间体，不升级 partial/blocked，不预测产率。需要配置 AI provider。
            </p>
            <div className="row">
              <input
                type="text"
                value={reasonQuery}
                placeholder="推理问题，例如 比较这些路线并指出限制"
                onChange={(e) => setReasonQuery(e.target.value)}
              />
              <button
                className="secondary"
                onClick={reasonRoutes}
                disabled={!routeTarget.trim() || !reasonQuery.trim()}
              >
                Reason about routes
              </button>
            </div>
            {reasonMsg && <p className="error">{reasonMsg}</p>}
            {reasoning && (
              <div style={{ marginTop: 10 }}>
                <p className="muted">
                  {reasoning.route_summaries.length} routes analysed ·{" "}
                  {reasoning.model_metadata.provider}/{reasoning.model_metadata.model}
                </p>
                {reasoning.observations.map((o) => (
                  <div key={o.route_id} className="reaction-card" style={{ marginBottom: 10 }}>
                    <div className="row spread">
                      <span className="badge">{o.status}</span>
                      <span className="badge known">{o.grounding_status}</span>
                    </div>
                    <div className="muted mono">{o.route_id}</div>
                    <div>{o.summary}</div>
                    {o.step_notes.map((n, i) => (
                      <div key={i} className="muted">
                        step {n.step_index}{" "}
                        <a onClick={() => go(`#/reactions/${n.reaction_template_id}`)}>
                          {n.reaction_template_id}
                        </a>
                        : {n.note}
                      </div>
                    ))}
                    {o.gaps.length > 0 && (
                      <div className="muted">Gaps: {o.gaps.join("；")}</div>
                    )}
                  </div>
                ))}
                {reasoning.unsupported.length > 0 && (
                  <p className="muted">未 ground：{reasoning.unsupported.join("；")}</p>
                )}
                {reasoning.warnings.length > 0 && (
                  <p className="muted">Warnings: {reasoning.warnings.join("；")}</p>
                )}
              </div>
            )}
          </section>

          <section className="panel">
            <h2>Modification Analysis</h2>
            <p className="muted">
              AI 解释你的修改意图，并把候选**grounded**到 CREA 已有反应事实。
              候选必须引用 Context 中真实存在的 ReactionTemplate；无法 ground 的
              候选不会作为化学事实展示。AI 不创造反应/试剂/条件/文献/产率。
            </p>
            <div className="row">
              <input
                type="text"
                value={modQuery}
                placeholder="修改意图，例如 把羟基转化成氯"
                onChange={(e) => setModQuery(e.target.value)}
              />
              <input
                type="text"
                value={modTarget}
                placeholder="target SMILES（可选）"
                onChange={(e) => setModTarget(e.target.value)}
              />
              <button onClick={runModification} disabled={!modQuery.trim()}>
                Analyze
              </button>
            </div>
            {modMsg && <p className="error">{modMsg}</p>}
            {modification && (
              <div style={{ marginTop: 10 }}>
                {modification.candidates.length === 0 &&
                  modification.unsupported.length === 0 && (
                    <p className="muted">No grounded reaction template found.</p>
                  )}
                {modification.candidates.map((c) => (
                  <div key={c.candidate_id} className="reaction-card" style={{ marginBottom: 10 }}>
                    <div className="row spread">
                      <strong>{c.description}</strong>
                      <span className="badge known">{c.grounding_status}</span>
                    </div>
                    <div className="muted">
                      {c.structural_change.from_feature} → {c.structural_change.to_feature}
                    </div>
                    <div style={{ marginTop: 6 }}>
                      <div className="muted">CREA facts</div>
                      {c.reaction_template_ids.map((tid) => (
                        <div key={tid}>
                          <a onClick={() => go(`#/reactions/${tid}`)}>{tid}</a>{" "}
                          {c.match_status[tid] && (
                            <span className="badge">{c.match_status[tid]}</span>
                          )}
                          {c.evidence_summary && Object.keys(c.evidence_summary).length > 0 && (
                            <span className="muted"> · evidence {JSON.stringify(c.evidence_summary)}</span>
                          )}
                        </div>
                      ))}
                    </div>
                    <div style={{ marginTop: 6 }}>
                      <div className="muted">AI interpretation</div>
                      <div>{c.rationale}</div>
                    </div>
                  </div>
                ))}
                {modification.unsupported.length > 0 && (
                  <p className="muted">
                    未 ground 的候选（不作为事实展示）：{modification.unsupported.join("；")}
                  </p>
                )}
                {modification.warnings.length > 0 && (
                  <p className="muted">Warnings: {modification.warnings.join("；")}</p>
                )}
                <p className="muted">
                  {modification.model_metadata.provider} / {modification.model_metadata.model}
                </p>
              </div>
            )}
          </section>

          <section className="panel">
            <h2>Evidence-aware Agent</h2>
            <p className="muted">
              Agent 通过**只读工具**回答，答案 grounded：fact 必须引用工具真实返回的
              id。AI 可以推理，但不能成为化学事实的来源；数据不足会明说。
            </p>
            <div className="row">
              <input
                type="text"
                value={agentQuery}
                placeholder="例如：这个分子有哪些已知修饰方向？"
                onChange={(e) => setAgentQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    runAgent();
                  }
                }}
              />
              <button onClick={runAgent} disabled={!agentQuery.trim()}>
                Run Agent
              </button>
            </div>
            {agentMsg && <p className="error">{agentMsg}</p>}
            {agentResult && (
              <div style={{ marginTop: 10 }}>
                <p className="muted">
                  {agentResult.status} · {agentResult.stop_reason} ·{" "}
                  {agentResult.model_metadata.provider}
                </p>
                <p>{agentResult.answer.summary}</p>
                {agentResult.answer.claims.length > 0 && (
                  <div>
                    <h3>Claims</h3>
                    {agentResult.answer.claims.map((c, i) => (
                      <div key={i} style={{ marginBottom: 6 }}>
                        <span
                          className={`badge ${c.grounded ? "known" : "unknown"}`}
                        >
                          {c.claim_type}
                        </span>{" "}
                        {c.text}
                        <div className="muted">
                          {c.reaction_ids.map((id) => (
                            <a key={id} onClick={() => go(`#/reactions/${id}`)}>
                              {id}{" "}
                            </a>
                          ))}
                          {c.literature_ids.map((id) => (
                            <a key={id} onClick={() => go(`#/literature/${id}`)}>
                              {id}{" "}
                            </a>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {agentResult.answer.limitations.length > 0 && (
                  <p className="muted">
                    Limitations: {agentResult.answer.limitations.join("；")}
                  </p>
                )}
                <details>
                  <summary className="muted">Agent activity ({agentResult.tool_calls.length})</summary>
                  {agentResult.tool_calls.map((t) => (
                    <div key={t.tool_call_id} className="muted">
                      ✓ {t.tool_name} — {t.output_summary}
                    </div>
                  ))}
                </details>
                {agentResult.warnings.length > 0 && (
                  <p className="muted">Warnings: {agentResult.warnings.join("；")}</p>
                )}
              </div>
            )}
          </section>

          <section className="panel">
            <h2>External Sources — PubChem</h2>
            <p className="muted">
              PubChem 精确结构查询（exact identity lookup），返回的 InChIKey 会与
              本地 Standard InChIKey 验证；查询与导入分开。
            </p>
            <button onClick={searchPubchem} disabled={pubchemStatus === "loading"}>
              {pubchemStatus === "loading" ? "查询中…" : "Search PubChem"}
            </button>
            {pubchemStatus === "success" && pubchemResult && (
              <div style={{ marginTop: 10 }}>
                <dl className="kv">
                  <dt>Status</dt>
                  <dd>
                    {pubchemResult.identity_match ? (
                      <span className="badge known">Identity matched</span>
                    ) : (
                      <span className="badge conflict">Identity mismatch</span>
                    )}
                  </dd>
                  <dt>CID</dt>
                  <dd>{pubchemResult.provider_record_id}</dd>
                  <dt>Name</dt>
                  <dd>{pubchemResult.iupac_name || "—"}</dd>
                  <dt>InChIKey</dt>
                  <dd className="mono">{pubchemResult.standard_inchikey}</dd>
                </dl>
                {pubchemResult.diagnostics.length > 0 && (
                  <p className="muted">{pubchemResult.diagnostics.join("；")}</p>
                )}
                <div className="row">
                  {pubchemResult.source_url && (
                    <a href={pubchemResult.source_url} target="_blank" rel="noreferrer">
                      Open PubChem
                    </a>
                  )}
                  {pubchemResult.identity_match && (
                    <button className="secondary" onClick={importPubchem}>
                      Import to Library
                    </button>
                  )}
                </div>
                {pubchemMessage && <p className="muted">{pubchemMessage}</p>}
              </div>
            )}
            {pubchemStatus === "notfound" && (
              <p className="muted">
                Status: Not found —— PubChem 中没有该 InChIKey 的条目。
              </p>
            )}
            {pubchemStatus === "unavailable" && (
              <p className="muted">
                Status: Unavailable —— PubChem 当前不可用（offline / 网络失败），无法查询。
              </p>
            )}
            {pubchemStatus === "error" && <p className="error">PubChem 查询失败。</p>}
          </section>
        </>
      )}
    </>
  );
}
