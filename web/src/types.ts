// API DTO shapes (mirror of crea.app.dto). Ids over objects (§60).

export interface JobView {
  job_id: string;
  status: string;
  question: string;
  phase: string;
  session_id: string;
  error: string;
  cancel_requested: boolean;
  created_at: string;
  started_at: string;
  finished_at: string;
  structure_kind: string | null;
  structure_value: string;
  document_ids: string[];
}

export interface FacetView {
  facet: string;
  state: string;
  detail: string;
  object_ids: string[];
}

export interface GapView {
  facet: string;
  reason: string;
  suggested_action: string;
}

export interface ConflictView {
  field: string;
  kind: string;
  note: string;
  observations: {
    value: number | string | null;
    raw_value: string;
    unit: string;
    raw_unit: string;
    evidence_id: string;
    paper_id: string;
    reaction_id: string;
  }[];
}

export interface KnowledgeStateView {
  entity_id: string | null;
  facets: FacetView[];
  gaps: GapView[];
  conflicts: ConflictView[];
  note: string;
}

export interface EntityView {
  entity_id: string;
  identity_key: string;
  identity_basis: string;
  canonical_smiles: string;
  inchi: string;
  inchikey: string;
  molecular_formula: string | null;
  molecular_weight: number | null;
  names: string[];
  external_identifiers: { namespace: string; identifier: string; source: string }[];
  provenance: Record<string, unknown> | null;
  created_at: string;
}

export interface ReactionView {
  reaction_id: string;
  reaction_key: string;
  status: string;
  record_id: string;
  paper_id: string;
  doi: string;
  source_id: string;
  document_id: string;
  components: {
    role: string;
    name: string;
    entity_id: string | null;
    canonical_smiles: string;
    mention_id: string | null;
  }[];
  evidence_ids: string[];
  unresolved_evidence_refs: string[];
  notes: string[];
}

export interface EvidenceView {
  evidence_id: string;
  evidence_type: string;
  strength: string;
  source: string;
  source_id: string;
  retrieved_at: string;
  raw_quote: string;
  paper_id: string | null;
  doi: string | null;
}

export interface RouteView {
  route_id: string;
  target_smiles: string;
  score: number | null;
  score_source: string;
  source: string;
  steps: {
    step_id: string;
    step_index: number;
    status: string;
    support_level: string;
    reactant_smiles: string[];
    product_smiles: string[];
    linked_reactions: {
      link_id: string;
      reaction_id: string;
      match_type: string;
      screening_only: boolean;
    }[];
  }[];
}

export interface SessionDetail {
  session_id: string;
  question: string;
  status: string;
  created_at: string;
  phases: string[];
  entity_ids: string[];
  mention_ids: string[];
  reaction_ids: string[];
  evidence_ids: string[];
  route_ids: string[];
  source_ids: string[];
  identity: {
    status: string;
    reason: string;
    input_kind: string;
    input_value: string;
    canonical_smiles: string;
    inchi: string;
    inchikey: string;
    names: string[];
    entity_id: string | null;
  } | null;
  analysis: string[];
  knowledge_before: KnowledgeStateView | null;
  knowledge_gaps: GapView[];
  knowledge_reused: Record<string, string[]>;
  knowledge_added: Record<string, string[]>;
  conflicts: ConflictView[];
  report_markdown: string;
  job_id: string;
}

export interface ResearchEvent {
  type: string;
  at: string;
  message: string;
  entity_ids: string[];
  reaction_ids: string[];
  evidence_ids: string[];
  document_ids: string[];
}

export interface ProvenanceChain {
  kind: string;
  id: string;
  label: string;
  links: Record<string, string[]>;
}

// -- Chemistry Workspace (Stage F) ------------------------------------------

export interface WorkspaceView {
  workspace_id: string;
  title: string;
  created_at: string;
}

export interface ToolCallView {
  tool: string;
  status: string;
  data: unknown;
  error_type: string | null;
  error: string | null;
  diagnostics: string[];
  provenance: Record<string, unknown> | null;
}

export interface WorkspaceMessageView {
  message_id: string;
  workspace_id: string;
  role: string; // "user" | "assistant"
  content: string;
  created_at: string;
  tool_calls: ToolCallView[];
  evidence: Record<string, unknown>[];
  provenance: Record<string, unknown>[];
  diagnostics: string[];
}

export interface SendMessageResponse {
  message: { id: string; role: string; content: string };
  tool_calls: ToolCallView[];
  evidence: Record<string, unknown>[];
  provenance: Record<string, unknown>[];
  diagnostics: string[];
}

// -- Molecule Explorer (Phase 1) --------------------------------------------

export interface FunctionalGroupView {
  name: string;
  smarts: string;
  count: number;
  atom_indices: number[][];
}

export interface ReactionTemplateView {
  template_id: string;
  name: string;
  class_family: string;
  reaction_class: string;
  transformation: {
    input_features: string[];
    output_features: string[];
    note: string;
  };
  substrate_patterns: string[];
  product_patterns: string[];
  product_pattern_note: string;
  reagents: { name: string; role: string }[];
  conditions: { kind: string; value: string }[];
  mechanism_class: string;
  selectivity: {
    regioselectivity: string;
    stereoselectivity: string;
    chemoselectivity: string;
    note: string;
  } | null;
  limitations: { text: string; evidence_status: string }[];
  source_type: string;
  evidence_status: string;
}

export interface ReactionMatchView {
  template: ReactionTemplateView;
  matched: boolean;
  matched_atoms: number[][];
  note: string;
}

export interface TransformationCandidateView {
  reaction_template: ReactionTemplateView;
  match_level: string; // "transformation_match" | "substrate_match"
  transformation: { input_features: string[]; output_features: string[]; note: string };
  evidence_count: number;
  evidence_summary: Record<string, number>;
  notes: string[];
}

export interface TransformationSearchView {
  substrate: string;
  target: string;
  candidates: TransformationCandidateView[];
  diagnostics: string[];
  count: number;
}

export interface ReactionComparisonEntryView {
  template_id: string;
  name: string;
  class_family: string;
  reaction_class: string;
  transformation: { input_features: string[]; output_features: string[]; note: string };
  substrate_compatibility: string;
  matched_substrate_atoms: number;
  reagents: string[];
  conditions: string[];
  mechanism_class: string;
  selectivity: Record<string, string>;
  limitations: string[];
  evidence_count: number;
  evidence_summary: Record<string, number>;
  literature: {
    evidence_type: string;
    source_type: string;
    support_scope: string;
    paper_id: string;
    title: string;
    year: number | null;
    journal: string;
    doi: string;
  }[];
  yield_available: boolean;
}

export interface ReactionComparisonView {
  substrate: string;
  target: string;
  entries: ReactionComparisonEntryView[];
  diagnostics: string[];
}

export interface GraphNodeView {
  id: string;
  type: "molecule" | "reaction" | "literature";
  label: string;
  metadata: Record<string, unknown>;
}

export interface GraphEdgeView {
  source: string;
  target: string;
  type: string;
  metadata: Record<string, unknown>;
}

export interface ReactionGraphView {
  root: string;
  nodes: GraphNodeView[];
  edges: GraphEdgeView[];
  depth: number;
  diagnostics: string[];
}

export interface ExploredRouteStepView {
  step_index: number;
  reaction_template_id: string;
  reaction_name: string;
  reaction_class: string;
  input_molecule_id: string;
  input_smiles: string;
  output_molecule_id: string;
  output_smiles: string;
  match_level: string;
  evidence_count: number;
  evidence_summary: Record<string, number>;
}

export interface ExploredRouteView {
  route_id: string;
  status: "complete" | "partial" | "blocked";
  start_smiles: string;
  target_smiles: string;
  steps: ExploredRouteStepView[];
  blocked_reason: string;
  blocked_reaction_id: string;
}

export interface ModificationCandidateView {
  candidate_id: string;
  description: string;
  structural_change: { from_feature: string; to_feature: string; scope: string };
  reaction_template_ids: string[];
  match_status: Record<string, string>;
  evidence_ids: string[];
  literature_ids: string[];
  evidence_summary: Record<string, number>;
  rationale: string;
  grounding_status: string;
}

export interface ModificationAnalysisView {
  analysis_id: string;
  context_version: string;
  input_molecule: string;
  input_molecule_id: string;
  user_query: string;
  candidates: ModificationCandidateView[];
  unsupported: string[];
  limitations: string[];
  warnings: string[];
  model_metadata: {
    provider: string;
    model: string;
    temperature: number | null;
    request_id: string;
  };
}

export interface StudyReferenceView {
  reference_id: string;
  kind: string;
  object_id: string;
  label: string;
  note: string;
}

export interface StudyEventView {
  event_id: string;
  event_type: string;
  at: string;
  summary: string;
  reference_id: string;
  artifact_id: string;
  note: string;
}

export interface StudyArtifactView {
  artifact_id: string;
  artifact_type: string;
  created_at: string;
  label: string;
  content: Record<string, unknown>;
}

export interface ResearchStudyView {
  study_id: string;
  title: string;
  question: string;
  status: string;
  created_at: string;
  updated_at: string;
  start_molecule_id: string;
  target_molecule_id: string;
}

export interface ResearchStudyDetailView {
  study: ResearchStudyView;
  references: StudyReferenceView[];
  events: StudyEventView[];
  artifacts: StudyArtifactView[];
}

export interface FinalAuditView {
  artifact_id: string;
  audit_version: string;
  status: string;
  summary: { total_checks: number; passed: number; warnings: number; failures: number };
  audit: {
    audit_id: string;
    study_id: string;
    report_artifact_id: string;
    status: string;
    statement: string;
    checks: { check_id: string; title: string; status: string; summary: string }[];
    warnings: string[];
    failures: string[];
  };
}

export interface StudyReportView {
  artifact_id: string;
  report_version: string;
  status: string;
  report: {
    report_id: string;
    study_id: string;
    title: string;
    question: string;
    status: string;
    generated_at: string;
    sections: { section_id: string; title: string; summary: string; lines: string[] }[];
    references: { ref_id: string; title: string; doi: string; year: number | null }[];
    warnings: { code: string; message: string }[];
  };
  markdown: string;
}

export interface WorkflowTaskView {
  task_id: string;
  workflow_id: string;
  task_type: string;
  status: string;
  title: string;
  depends_on: string[];
  output_artifact_ids: string[];
  attempt_count: number;
  error: string;
}

export interface TaskWorkflowView {
  workflow_id: string;
  study_id: string;
  template: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface TaskWorkflowDetailView {
  workflow: TaskWorkflowView;
  tasks: WorkflowTaskView[];
}

export interface AgentClaimView {
  text: string;
  claim_type: string;
  not_experimentally_verified: boolean;
  reaction_ids: string[];
  evidence_ids: string[];
  literature_ids: string[];
  route_ids: string[];
  molecule_ids: string[];
  rationale: string;
  grounded: boolean;
}

export interface AgentAnswerView {
  run_id: string;
  question: string;
  status: string;
  stop_reason: string;
  answer: {
    summary: string;
    claims: AgentClaimView[];
    limitations: string[];
    sources: { kind: string; id: string }[];
  };
  tool_calls: {
    tool_call_id: string;
    tool_name: string;
    status: string;
    output_summary: string;
    output_ids: string[];
  }[];
  iterations: number;
  warnings: string[];
  model_metadata: { provider: string; model: string };
}

export interface RouteReasoningView {
  reasoning_id: string;
  context_version: string;
  start_smiles: string;
  target_smiles: string;
  route_summaries: { route_id: string; status: string; step_count: number; blocked_reason: string }[];
  observations: {
    route_id: string;
    status: string;
    summary: string;
    step_notes: { step_index: number; reaction_template_id: string; note: string }[];
    gaps: string[];
    grounding_status: string;
  }[];
  comparisons: string[];
  gaps: string[];
  unsupported: string[];
  limitations: string[];
  warnings: string[];
  model_metadata: { provider: string; model: string; temperature: number | null; request_id: string };
}

export interface RouteSearchView {
  start_smiles: string;
  target_smiles: string;
  max_steps: number;
  routes: ExploredRouteView[];
  partial_routes: ExploredRouteView[];
  blocked_paths: ExploredRouteView[];
  count: number;
  diagnostics: string[];
}

export interface LiteraturePaperView {
  paper_id: string;
  title: string | null;
  authors: { given: string; family: string; name: string }[];
  journal: string | null;
  year: number | null;
  identifiers: { doi: string | null; openalex_id: string | null };
  abstracts: { text: string; source: string }[];
  access: { oa_status: string; permission: string; fulltext_url: string | null };
  sources: string[];
  url: string | null;
}

export interface AccessAssessmentView {
  permission: string;
  permission_basis: string;
  oa_status: string;
  access_status: string;
  fulltext_url: string | null;
  url_type: string;
  policy_version: string;
  reasons: string[];
  access_evidence: { kind: string; detail: string; source: string }[];
}

export interface LiteratureAccessView {
  doi: string;
  found: boolean;
  assessment: AccessAssessmentView | null;
  reconciliations: Record<string, unknown>[];
  warnings: string[];
}

export interface LiteratureSearchView {
  outcome: string;
  papers: LiteraturePaperView[];
  provider_runs: { name: string; status: string; records_found: number }[];
  warnings: string[];
}

export interface ReactionEvidenceView {
  evidence_id: string;
  reaction_template_id: string;
  evidence_type: string;
  source: {
    source_type: string;
    source_id: string;
    title: string;
    url: string;
    locator: Record<string, unknown>;
    retrieved_at: string;
  };
  support_scope: string;
  status: string;
  note: string;
}

export interface MoleculeInspectionView {
  smiles: string;
  valid: boolean;
  error: string;
  identity: { canonical_smiles: string; inchi: string; inchikey: string; mol_id: string };
  structure: {
    input_smiles: string;
    canonical_smiles: string;
    inchi: string;
    inchikey: string;
  };
  properties: Record<string, number | string>;
  functional_groups: FunctionalGroupView[];
  scaffold: { murcko_smiles: string; ring_systems: string[]; note: string };
  fragments: {
    fragment_id: string;
    fragment_smiles: string;
    formula: string;
    n_attachment_points: number;
    heavy_atom_count: number;
    fg_labels: string[];
  }[];
  reactions: ReactionMatchView[];
  provenance: Record<string, unknown>;
  diagnostics: string[];
}

export interface SimilarityView {
  smiles_a: string;
  smiles_b: string;
  tanimoto: number;
  dice: number;
  scaffold_relation: string;
  mcs: {
    found: boolean;
    common_atoms: number;
    common_bonds: number;
    common_substructure_smiles: string;
    coverage_a: number;
    coverage_b: number;
  };
  diagnostics: string[];
}

// -- 3D Conformer (Stage 2.1) -----------------------------------------------

export interface ConformerAtomView {
  atom_index: number;
  element: string;
  atomic_number: number;
  x: number;
  y: number;
  z: number;
}

export interface ConformerBondView {
  a: number;
  b: number;
  order: string;
}

export interface ConformerView {
  canonical_smiles: string;
  formula: string;
  conformer_id: number;
  generation_method: string;
  optimized: boolean;
  optimization_method: string;
  charge: number;
  atoms: ConformerAtomView[];
  bonds: ConformerBondView[];
  warnings: string[];
}

// -- Molecule Library (Stage 2.2) -------------------------------------------

export interface MoleculeRecordView {
  entity_id: string;
  identity_key: string;
  identity_key_version: string;
  identity_basis: string;
  canonical_smiles: string;
  inchi: string;
  inchikey: string;
  molecular_formula: string | null;
  molecular_weight: number | null;
  names: string[];
  external_identifiers: { namespace: string; identifier: string; source: string }[];
  identity_source: string;
  source_id: string;
  created_at: string;
}

export interface LibraryHitView {
  entity: MoleculeRecordView;
  score: number | null;
  note: string;
}

export interface PubChemResultView {
  provider: string;
  provider_record_id: string;
  standard_inchi: string;
  standard_inchikey: string;
  canonical_smiles: string;
  molecular_formula: string | null;
  molecular_weight: number | null;
  iupac_name: string;
  external_identifiers: Record<string, unknown>;
  source_url: string;
  retrieved_at: string;
  identity_match: boolean;
  requested_inchikey: string;
  diagnostics: string[];
}
