import type {
  AnalysisResult, ContextDraft, DebateSession, ErrorInfo, Health, IdeaSpec, Job, JobReceipt,
  ModelCatalog, Project, ProjectContext, ResearchOptions,
} from './types'

export class ApiError extends Error {
  constructor(readonly info: ErrorInfo, readonly status: number) {
    super(info.message)
    this.name = 'ApiError'
  }
}

/** Override apiBase when this feature is mounted in Member 3's application. */
export class IntelligenceApi {
  constructor(readonly apiBase = '/api/v1') {}

  private url(path: string): string {
    if (path.startsWith('/api/v1/')) return `${this.apiBase.replace(/\/$/, '')}${path.slice('/api/v1'.length)}`
    return `${this.apiBase.replace(/\/$/, '')}/${path.replace(/^\//, '')}`
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers)
    if (init.body) headers.set('Content-Type', 'application/json')
    const timeout = AbortSignal.timeout(30_000)
    const signal = init.signal ? AbortSignal.any([init.signal, timeout]) : timeout
    let response: Response
    try {
      response = await fetch(this.url(path), { ...init, headers, signal })
    } catch (error) {
      if (init.signal?.aborted) throw error
      throw new Error(timeout.aborted
        ? 'The server took too long to respond. Check the project history before starting another request.'
        : 'Cannot reach the intelligence service. Start the backend, then try again.')
    }
    const data: unknown = await response.json().catch(() => null)
    if (!response.ok) {
      const envelope = data as { error?: ErrorInfo } | null
      throw new ApiError(envelope?.error ?? {
        code: `HTTP_${response.status}`, message: `The request failed (${response.status}). Please try again.`,
        details: {}, retryable: false, request_id: '',
      }, response.status)
    }
    if (!data) throw new Error('The service returned an empty response.')
    return data as T
  }

  health(signal?: AbortSignal) { return this.request<Health>('health', { signal }) }
  models(signal?: AbortSignal) { return this.request<ModelCatalog>('intelligence/models', { signal }) }
  projects(signal?: AbortSignal) {
    return this.request<{ items: Project[]; next_cursor: string | null }>('projects?limit=100', { signal })
  }
  project(id: string, signal?: AbortSignal) {
    return this.request<Project>(`projects/${encodeURIComponent(id)}`, { signal })
  }
  context(id: string, signal?: AbortSignal) {
    return this.request<ProjectContext>(`projects/${encodeURIComponent(id)}/context`, { signal })
  }
  projectJobs(id: string, signal?: AbortSignal, operation?: string, debateId?: string) {
    const query = new URLSearchParams()
    if (operation) query.set('operation', operation)
    if (debateId) query.set('debate_id', debateId)
    return this.request<{ jobs: Job[]; next_cursor: string | null }>(`projects/${encodeURIComponent(id)}/jobs?${query}`, { signal })
  }
  createProject(displayName: string, ideaText: string, signal?: AbortSignal) {
    return this.request<Project>('projects', {
      method: 'POST', body: JSON.stringify({ display_name: displayName, idea_text: ideaText }), signal,
    })
  }
  updateProject(project: Project, changes: Partial<Pick<Project, 'display_name' | 'draft_idea_text' | 'draft_idea'>>, signal?: AbortSignal) {
    return this.request<Project>(`projects/${encodeURIComponent(project.project_id)}`, {
      method: 'PATCH', body: JSON.stringify({ expected_revision: project.revision, ...changes }), signal,
    })
  }
  extract(projectId: string, ideaText: string, signal?: AbortSignal, modelId?: string | null) {
    return this.request<JobReceipt>('intelligence/extractions', {
      method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ project_id: projectId, idea_text: ideaText, model_id: modelId }), signal,
    })
  }
  analyze(projectId: string, idea: IdeaSpec, maxSources: number, signal?: AbortSignal, sourceUrls: string[] = [], region = '', modelId?: string | null) {
    const researchOptions: ResearchOptions = { max_sources: maxSources, language: 'en', region: region.trim() || null }
    if (sourceUrls.length) researchOptions.source_urls = sourceUrls
    return this.request<JobReceipt>('intelligence/analyses', {
      method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ project_id: projectId, idea, research_options: researchOptions, model_id: modelId }),
      signal,
    })
  }
  analysis(path: string, signal?: AbortSignal) { return this.request<AnalysisResult>(path, { signal }) }
  debate(path: string, signal?: AbortSignal) { return this.request<DebateSession>(path, { signal }) }
  startDebate(projectId: string, analysisId: string, mode: DebateSession['mode'], userStance: DebateSession['user_stance'], maxRounds: number, signal?: AbortSignal, modelId?: string | null) {
    return this.request<JobReceipt>('intelligence/debates', {
      method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ project_id: projectId, analysis_id: analysisId, mode, user_stance: userStance, max_rounds: maxRounds, model_id: modelId }), signal,
    })
  }
  resumeDebate(debateId: string, signal?: AbortSignal) {
    return this.request<JobReceipt>(`intelligence/debates/${encodeURIComponent(debateId)}/resume`, {
      method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() }, signal,
    })
  }
  sendTurn(debateId: string, message: string, signal?: AbortSignal) {
    return this.request<JobReceipt>(`intelligence/debates/${encodeURIComponent(debateId)}/turns`, {
      method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ message }), signal,
    })
  }
  save(project: Project, context: ContextDraft, signal?: AbortSignal) {
    return this.request<ProjectContext>(`projects/${encodeURIComponent(project.project_id)}/contexts`, {
      method: 'POST', body: JSON.stringify({ expected_latest_version: project.latest_context_version, context }), signal,
    })
  }

  async poll(receipt: JobReceipt, signal: AbortSignal, onUpdate: (job: Job) => void | Promise<void>, options?: { timeoutMs: number }): Promise<Job> {
    const deadline = Date.now() + (options?.timeoutMs ?? 600_000)
    while (Date.now() < deadline) {
      signal.throwIfAborted()
      const job = await this.request<Job>(receipt.status_url, { signal })
      await onUpdate(job)
      if (job.status === 'failed') {
        throw new ApiError(job.error ?? {
          code: 'JOB_FAILED', message: 'The analysis job could not complete.', details: {}, retryable: true, request_id: '',
        }, 502)
      }
      if (job.status === 'succeeded' || job.status === 'partial_success') return job
      await new Promise<void>((resolve, reject) => {
        const abort = () => { clearTimeout(timer); reject(signal.reason) }
        const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, 1_000)
        signal.addEventListener('abort', abort, { once: true })
        if (signal.aborted) abort()
      })
    }
    throw new Error(`This job is still taking time. Its ID is ${receipt.job_id}. Reopen this project to check saved results before retrying.`)
  }
}
