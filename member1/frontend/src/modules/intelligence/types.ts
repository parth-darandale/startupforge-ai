/** JSON boundary contract, matching Member 1's Pydantic models (schema 1.0). */
export type ClaimKind = 'supported' | 'assumption' | 'unknown'
export type FieldOrigin = 'provided' | 'inferred' | 'unknown'

export interface IdeaSpec {
  original_text: string
  project_name: string
  problem: string
  solution: string
  target_users: string[]
  domain: string
  assumptions: string[]
  constraints: string[]
  field_origins: Record<string, FieldOrigin>
}

export interface ResearchOptions {
  max_sources: number
  language: string
  region: string | null
  source_urls?: string[]
}

export interface Source {
  source_id: string
  title: string
  url: string
  retrieved_at: string
  publisher: string | null
  kind: 'web' | 'fixture'
}

export interface EvidenceItem {
  evidence_id: string
  source_id: string
  excerpt: string
  locator: string | null
  relevance_score: number
}

export interface Claim {
  claim_id: string
  text: string
  kind: ClaimKind
  evidence_ids: string[]
}

export interface AnalysisReport {
  summary: string
  sections: { section_id: string; heading: string; claims: Claim[] }[]
  gaps: string[]
}

export interface CriticIssue {
  issue_id: string
  severity: 'high' | 'medium' | 'low'
  claim_id: string | null
  reason: string
  evidence_ids: string[]
  resolution: string | null
}

export interface CritiqueReport {
  review_status: 'ready' | 'ready_with_gaps' | 'blocked'
  issues: CriticIssue[]
  revision_summary: string
  unresolved_gaps: string[]
}

export interface ContextDraft {
  schema_version: '1.0'
  idea: IdeaSpec
  sources: Source[]
  evidence: EvidenceItem[]
  initial_analysis: AnalysisReport
  final_analysis: AnalysisReport
  critic: CritiqueReport
  warnings: string[]
  execution: {
    mode: 'demo' | 'live'
    model_id?: string | null
    llm_provider: string
    search_provider: string
    retrieval_method: string
    revision_passes: number
  }
  figures?: Figure[]
}

export interface ProjectContext extends ContextDraft {
  project_id: string
  context_version: number
  created_at: string
  content_hash: string
}

export interface AnalysisResult {
  analysis_id: string
  model_id?: string | null
  project_id: string
  context_draft: ContextDraft
  stages: string[]
  warnings: string[]
  created_at: string
  figures?: Figure[]
}

export interface Figure {
  figure_id: string
  display_value: string
  metric: string
  excerpt: string
  evidence_id: string
  source_id: string
  qualification: string
}

export interface DebateTurn {
  turn_id: string
  round_number: number
  speaker: 'user' | 'pro' | 'skeptic' | 'moderator'
  stance: 'for' | 'against' | 'neutral'
  message: string
  evidence_ids: string[]
  source_ids: string[]
  figures: Figure[]
  created_at: string
}

export interface DebateSession {
  debate_id: string
  model_id?: string | null
  project_id: string
  analysis_id: string
  mode: 'user_vs_agent' | 'agent_vs_agent'
  user_stance: 'for' | 'against'
  max_rounds: number
  status: 'active' | 'completed' | 'failed'
  turns: DebateTurn[]
  summary: string | null
  history_compacted?: boolean
  history_note?: string | null
  created_at: string
  updated_at: string
}

export interface Project {
  project_id: string
  display_name: string
  draft_idea_text: string
  draft_idea?: IdeaSpec | null
  revision: number
  latest_context_version: number
  created_at: string
  updated_at: string
}

export interface ErrorInfo {
  code: string
  message: string
  details: Record<string, unknown>
  retryable: boolean
  request_id: string
}

export interface JobReceipt {
  job_id: string
  status_url: string
  operation: string
  project_id: string
}

export interface Job {
  job_id: string
  operation: string
  project_id: string
  status: 'queued' | 'running' | 'succeeded' | 'partial_success' | 'failed'
  stage: string
  progress: number
  created_at: string
  updated_at: string
  result: Record<string, unknown> | null
  result_url: string | null
  error: ErrorInfo | null
}

export interface Health {
  mode: 'demo' | 'live'
  model?: string | null
  schema_version: string
  providers?: Record<string, unknown>
  capabilities?: { debate: boolean; live_configured: boolean }
  configuration_message?: string | null
}

export interface ModelOption {
  model_id: string
  label: string
  description: string
  available: boolean
  experimental: boolean
  unavailable_reason?: string | null
}

export interface ModelCatalog {
  default_model_id: string | null
  mode: 'live' | 'demo'
  models: ModelOption[]
}
