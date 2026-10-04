// Typed fetch wrappers. Frontend -> HTTP API -> Application -> engine (§39).
// The frontend never sees a database, RDKit or an LLM.

import type {
  AgentAnswerView,
  ConformerView,
  EntityView,
  EvidenceView,
  FinalAuditView,
  JobView,
  KnowledgeStateView,
  LibraryHitView,
  LiteratureAccessView,
  ModificationAnalysisView,
  LiteraturePaperView,
  LiteratureSearchView,
  MoleculeInspectionView,
  MoleculeRecordView,
  ProvenanceChain,
  PubChemResultView,
  ReactionComparisonView,
  ReactionEvidenceView,
  ReactionGraphView,
  ReactionTemplateView,
  ResearchStudyDetailView,
  ResearchStudyView,
  RouteReasoningView,
  RouteSearchView,
  StudyEventView,
  StudyReferenceView,
  StudyReportView,
  TaskWorkflowDetailView,
  TaskWorkflowView,
  WorkflowTaskView,
  ReactionView,
  ResearchEvent,
  RouteView,
  SendMessageResponse,
  SessionDetail,
  SimilarityView,
  TransformationSearchView,
  WorkspaceMessageView,
  WorkspaceView,
} from "./types";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = (body as { error?: { code?: string; message?: string } }).error;
    throw new Error(err ? `${err.code}: ${err.message}` : `HTTP ${res.status}`);
  }
  return body as T;
}

export const api = {
  health: () => call<{ status: string; offline: boolean }>("/api/health"),

  startResearch: (payload: {
    question: string;
    structure_kind: string | null;
    structure_value: string;
    document_ids: string[];
  }) =>
    call<JobView>("/api/research", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),

  jobs: () => call<{ jobs: JobView[] }>("/api/research"),
  job: (id: string) => call<JobView>(`/api/research/${id}`),
  events: (id: string) =>
    call<{ events: ResearchEvent[] }>(`/api/research/${id}/events`),
  report: (id: string) =>
    call<{ status: string; report_markdown: string }>(
      `/api/research/${id}/report`,
    ),
  cancel: (id: string) =>
    call<JobView>(`/api/research/${id}/cancel`, { method: "POST" }),

  sessions: () =>
    call<{ sessions: { session_id: string; question: string; status: string; created_at: string }[] }>(
      "/api/sessions",
    ),
  session: (id: string) => call<SessionDetail>(`/api/sessions/${id}`),

  entity: (id: string) => call<EntityView>(`/api/entities/${id}`),
  entityNavigation: (id: string) =>
    call<ProvenanceChain>(`/api/entities/${id}/navigation`),
  reaction: (id: string) => call<ReactionView>(`/api/reactions/${id}`),
  reactionNavigation: (id: string) =>
    call<ProvenanceChain>(`/api/reactions/${id}/navigation`),
  reactionEvidence: (id: string) =>
    call<{ evidence: EvidenceView[] }>(`/api/reactions/${id}/evidence`),
  reactionRecords: (id: string) =>
    call<{ records: Record<string, unknown>[] }>(
      `/api/reactions/${id}/records`,
    ),
  evidence: (id: string) => call<EvidenceView>(`/api/evidence/${id}`),
  route: (id: string) => call<RouteView>(`/api/routes/${id}`),

  knowledgeState: (params: { entity_id?: string; name?: string }) => {
    const q = new URLSearchParams();
    if (params.entity_id) q.set("entity_id", params.entity_id);
    if (params.name) q.set("name", params.name);
    return call<KnowledgeStateView>(`/api/knowledge/state?${q}`);
  },
  search: (params: Record<string, string>) => {
    const q = new URLSearchParams(params);
    return call<{ results: { kind: string; id: string; label: string; match: string }[] }>(
      `/api/knowledge/search?${q}`,
    );
  },

  documents: () =>
    call<{ documents: { document_id: string; original_name: string; size_bytes: number }[] }>(
      "/api/documents",
    ),
  upload: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return call<{ document_id: string; original_name: string }>(
      "/api/documents",
      { method: "POST", body: form },
    );
  },

  moleculeSvgUrl: (smiles: string) =>
    `/api/molecule/svg?smiles=${encodeURIComponent(smiles)}`,

  inspectMolecule: (smiles: string) =>
    call<MoleculeInspectionView>(
      `/api/molecule/inspect?smiles=${encodeURIComponent(smiles)}`,
    ),
  compareMolecules: (a: string, b: string) =>
    call<SimilarityView>(
      `/api/molecule/compare?smiles_a=${encodeURIComponent(a)}` +
        `&smiles_b=${encodeURIComponent(b)}`,
    ),

  generateConformer: (smiles: string, optimize = false) =>
    call<ConformerView>(
      `/api/molecule/3d?smiles=${encodeURIComponent(smiles)}` +
        (optimize ? "&optimize=true" : ""),
    ),

  // -- Molecule Library (Stage 2.2) ----------------------------------------
  addMolecule: (smiles: string, sourceType = "USER_PROVIDED") =>
    call<{ molecule: MoleculeRecordView; is_new: boolean }>("/api/molecules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ smiles, source_type: sourceType }),
    }),
  listMolecules: () =>
    call<{ molecules: MoleculeRecordView[]; count: number }>("/api/molecules"),
  getMolecule: (id: string) => call<MoleculeRecordView>(`/api/molecules/${id}`),
  deleteMolecule: (id: string) =>
    call<{ deleted: boolean }>(`/api/molecules/${id}`, { method: "DELETE" }),
  searchSimilarMolecules: (smiles: string) =>
    call<{ hits: LibraryHitView[] }>(
      `/api/molecules/search/similar?smiles=${encodeURIComponent(smiles)}`,
    ),
  searchSubstructureMolecules: (smiles: string) =>
    call<{ hits: LibraryHitView[] }>(
      `/api/molecules/search/substructure?smiles=${encodeURIComponent(smiles)}`,
    ),

  // -- PubChem exact lookup (Stage 2.3) -------------------------------------
  pubchemLookup: (smiles: string) =>
    call<PubChemResultView>(
      `/api/providers/pubchem/lookup?smiles=${encodeURIComponent(smiles)}`,
    ),
  pubchemImport: (smiles: string) =>
    call<{ molecule: MoleculeRecordView; is_new: boolean }>(
      "/api/providers/pubchem/import",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ smiles }),
      },
    ),

  // -- Reaction Explorer (Stage 3.2) ----------------------------------------
  listReactions: (params: { class_family?: string; q?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.class_family) q.set("class_family", params.class_family);
    if (params.q) q.set("q", params.q);
    const suffix = q.toString() ? `?${q}` : "";
    return call<{ reactions: ReactionTemplateView[]; count: number }>(
      `/api/reaction-templates${suffix}`,
    );
  },
  getReaction: (templateId: string) =>
    call<ReactionTemplateView>(
      `/api/reaction-templates/${encodeURIComponent(templateId)}`,
    ),
  reactionTemplateEvidence: (templateId: string) =>
    call<{ evidence: ReactionEvidenceView[]; count: number }>(
      `/api/reaction-templates/${encodeURIComponent(templateId)}/evidence`,
    ),
  linkLiteratureEvidence: (
    templateId: string,
    body: {
      paper_id?: string;
      doi?: string;
      evidence_type?: string;
      support_scope: string;
      locator?: Record<string, unknown>;
      note?: string;
    },
  ) =>
    call<{ evidence: { evidence_id: string }; is_new: boolean }>(
      `/api/reaction-templates/${encodeURIComponent(templateId)}/evidence/literature`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  literatureReactionEvidence: (paperId: string) =>
    call<{ evidence: ReactionEvidenceView[]; count: number }>(
      `/api/literature/${encodeURIComponent(paperId)}/reaction-evidence`,
    ),

  // -- transformation search (Stage 5.1) ------------------------------------
  transformationSearch: (substrate: string, target = "") =>
    call<TransformationSearchView>(
      `/api/transformations/search?substrate=${encodeURIComponent(substrate)}` +
        (target ? `&target=${encodeURIComponent(target)}` : ""),
    ),

  // -- reaction comparison (Stage 5.2) --------------------------------------
  compareReactions: (ids: string[], substrate = "", target = "") =>
    call<ReactionComparisonView>("/api/reactions/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reaction_template_ids: ids,
        substrate,
        target,
      }),
    }),

  // -- reaction graph (Stage 5.3) -------------------------------------------
  moleculeGraph: (entityId: string, depth = 2) =>
    call<ReactionGraphView>(
      `/api/reaction-graph/molecule/${encodeURIComponent(entityId)}?depth=${depth}`,
    ),

  // -- route explorer (Stage 5.4) -------------------------------------------
  searchRoutes: (start: string, target: string, maxSteps = 3) =>
    call<RouteSearchView>("/api/routes/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start_molecule: start,
        target_molecule: target,
        max_steps: maxSteps,
      }),
    }),

  // -- research sessions (Stage 7.1) ----------------------------------------
  listSessions: (status?: string) =>
    call<{ sessions: ResearchStudyView[]; count: number }>(
      `/api/research/sessions${status ? `?status=${status}` : ""}`,
    ),
  createSession: (body: { title?: string; question?: string }) =>
    call<ResearchStudyView>("/api/research/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  sessionDetail: (id: string) =>
    call<ResearchStudyDetailView>(`/api/research/sessions/${id}`),
  updateSession: (id: string, body: { title?: string; status?: string }) =>
    call<ResearchStudyView>(`/api/research/sessions/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  addSessionReference: (id: string, kind: string, objectId: string) =>
    call<StudyReferenceView>(`/api/research/sessions/${id}/references`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, object_id: objectId }),
    }),
  removeSessionReference: (id: string, referenceId: string) =>
    call<{ removed: boolean }>(
      `/api/research/sessions/${id}/references/${referenceId}`,
      { method: "DELETE" },
    ),
  addSessionNote: (id: string, note: string) =>
    call<StudyEventView>(`/api/research/sessions/${id}/events`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event_type: "note_added", note }),
    }),

  // -- final audit (Stage 7.4) ----------------------------------------------
  runAudit: (studyId: string, reportArtifactId = "") =>
    call<FinalAuditView>(`/api/research/sessions/${studyId}/audit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ report_artifact_id: reportArtifactId }),
    }),
  latestAudit: (studyId: string) =>
    call<FinalAuditView>(`/api/research/sessions/${studyId}/audit`),

  // -- research report (Stage 7.3) ------------------------------------------
  generateReport: (studyId: string) =>
    call<StudyReportView>(`/api/research/sessions/${studyId}/report`, {
      method: "POST",
    }),
  latestReport: (studyId: string) =>
    call<StudyReportView>(`/api/research/sessions/${studyId}/report`),

  // -- research workflows (Stage 7.2) --------------------------------------
  listWorkflows: (studyId: string) =>
    call<{ workflows: TaskWorkflowView[]; count: number }>(
      `/api/research/sessions/${studyId}/workflows`,
    ),
  createWorkflow: (studyId: string, body: Record<string, unknown>) =>
    call<TaskWorkflowView>(`/api/research/sessions/${studyId}/workflows`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  workflowDetail: (workflowId: string) =>
    call<TaskWorkflowDetailView>(`/api/research/workflows/${workflowId}`),
  workflowAction: (workflowId: string, action: string) =>
    call<TaskWorkflowView>(`/api/research/workflows/${workflowId}/${action}`, {
      method: "POST",
    }),
  executeWorkflowTask: (workflowId: string, taskId: string) =>
    call<WorkflowTaskView>(
      `/api/research/workflows/${workflowId}/tasks/${taskId}/execute`,
      { method: "POST" },
    ),
  skipWorkflowTask: (workflowId: string, taskId: string, reason: string) =>
    call<WorkflowTaskView>(
      `/api/research/workflows/${workflowId}/tasks/${taskId}/skip`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason }),
      },
    ),

  // -- evidence-aware agent (Stage 6.4) -------------------------------------
  runAgent: (question: string, moleculeSmiles = "", moleculeId = "") =>
    call<{ answer: AgentAnswerView }>("/api/ai/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        molecule_smiles: moleculeSmiles,
        molecule_id: moleculeId,
      }),
    }),

  // -- route reasoning (Stage 6.3) ------------------------------------------
  reasonRoutes: (start: string, target: string, query: string, maxSteps = 3) =>
    call<{ reasoning: RouteReasoningView }>("/api/ai/route-reasoning", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start_molecule: start,
        target_molecule: target,
        query,
        max_steps: maxSteps,
      }),
    }),

  // -- modification analysis (Stage 6.2) ------------------------------------
  modifyAnalysis: (moleculeSmiles: string, query: string, targetSmiles = "") =>
    call<{ analysis: ModificationAnalysisView }>(
      "/api/ai/modification-analysis",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          molecule_smiles: moleculeSmiles,
          query,
          target_smiles: targetSmiles,
        }),
      },
    ),

  // -- literature (Stage 4.2) ----------------------------------------------
  literatureSearch: (q: string, limit = 20) =>
    call<LiteratureSearchView>(
      `/api/literature/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  literatureDoi: (doi: string) =>
    call<{
      found: boolean;
      paper: LiteraturePaperView | null;
      warnings: string[];
    }>(`/api/literature/doi/${encodeURIComponent(doi)}`),
  literatureAccess: (doi: string) =>
    call<LiteratureAccessView>(
      `/api/literature/doi/${encodeURIComponent(doi)}/access`,
    ),

  // -- Chemistry Workspace (Stage F) ---------------------------------------
  workspaces: () =>
    call<{ workspaces: WorkspaceView[] }>("/api/workspaces"),
  createWorkspace: (title: string) =>
    call<WorkspaceView>("/api/workspaces", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }),
  workspaceMessages: (id: string) =>
    call<{ messages: WorkspaceMessageView[] }>(
      `/api/workspaces/${id}/messages`,
    ),
  sendMessage: (id: string, content: string) =>
    call<SendMessageResponse>(`/api/workspaces/${id}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    }),
};
