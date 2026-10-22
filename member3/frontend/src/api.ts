import type { Job, JobReceipt, Project, ProjectContext } from '../../../member1/frontend/src/modules/intelligence/types'

export type ArtifactKind = 'pitch_deck' | 'poster' | 'report'
export type ModuleTemplate = {
  template_id: string
  name: string
  kind: ArtifactKind
  supported_formats: string[]
  supported_options: Record<string, unknown>
}
export type ArtifactFile = {
  file_id: string
  artifact_id: string
  context_version: number
  content_revision: number
  format: string
  media_type: string
  filename: string
  size_bytes: number
  created_at: string
  download_url: string
}
export type ArtifactManifest = {
  artifact_id: string
  project_id: string
  context_version: number
  context_hash: string
  kind: ArtifactKind
  content_revision: number
  producing_job_id: string | null
  created_at: string
  updated_at: string
  files: ArtifactFile[]
}
export type ArtifactContent<T = Record<string, unknown>> = {
  artifact_id: string
  kind: ArtifactKind
  content_revision: number
  created_at: string
  content: T
}

export class WorkspaceApi {
  constructor(private readonly base = '/api/v1') {}

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers)
    if (init.body) headers.set('Content-Type', 'application/json')
    const base = this.base.replace(/\/$/, '')
    const basePath = base.replace(/^\/+/, '')
    const route = path.replace(/^\/+/, '')
    const relativePath = route === basePath ? '' : route.startsWith(`${basePath}/`) ? route.slice(basePath.length + 1) : route
    const response = await fetch(`${base}/${relativePath}`, { ...init, headers })
    if (!response.ok) {
      const data = await response.json().catch(() => null) as { error?: { message?: string } } | null
      throw new Error(data?.error?.message ?? `The request failed (${response.status}).`)
    }
    return response.json() as Promise<T>
  }

  projects() { return this.request<{ items: Project[]; next_cursor: string | null }>('projects?limit=100') }
  project(id: string) { return this.request<Project>(`projects/${encodeURIComponent(id)}`) }
  context(id: string) { return this.request<ProjectContext>(`projects/${encodeURIComponent(id)}/context`) }
  templates(path: string) { return this.request<{ templates: ModuleTemplate[] }>(path) }
  generate(path: string, context: ProjectContext, options: Record<string, unknown>) {
    return this.request<JobReceipt>(path, { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ context, options }) })
  }
  run(project: Project, outputs: ArtifactKind[]) {
    return this.request<JobReceipt>(`projects/${encodeURIComponent(project.project_id)}/runs`, {
      method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ expected_latest_version: project.latest_context_version,
        idea_text: project.draft_idea_text, outputs }),
    })
  }
  job(path: string) { return this.request<Job>(path) }
  artifacts(projectId: string, kind?: ArtifactKind) {
    const suffix = kind ? `&kind=${encodeURIComponent(kind)}` : ''
    return this.request<{ items: ArtifactManifest[]; next_cursor: string | null }>(
      `projects/${encodeURIComponent(projectId)}/artifacts?limit=100${suffix}`)
  }
  artifact(id: string) { return this.request<ArtifactManifest>(`artifacts/${encodeURIComponent(id)}`) }
  content<T>(id: string) { return this.request<ArtifactContent<T>>(`artifacts/${encodeURIComponent(id)}/content`) }
  saveContent(id: string, revision: number, content: unknown) {
    return this.request<{ artifact_id: string; content_revision: number; validation_findings: unknown[]; manifest: ArtifactManifest }>(
      `artifacts/${encodeURIComponent(id)}/content`, { method: 'PUT',
        body: JSON.stringify({ base_revision: revision, content }) })
  }
  render(id: string, revision: number, formats: string[]) {
    return this.request<JobReceipt>(`artifacts/${encodeURIComponent(id)}/renders`, { method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify({ content_revision: revision, formats }) })
  }

  async poll(receipt: JobReceipt, onProgress: (job: Job) => void): Promise<Job> {
    const end = Date.now() + 120_000
    while (Date.now() < end) {
      const job = await this.job(receipt.status_url)
      onProgress(job)
      if (job.status === 'succeeded' || job.status === 'partial_success') return job
      if (job.status === 'failed') throw new Error(job.error?.message ?? 'The generation job failed.')
      await new Promise((resolve) => setTimeout(resolve, 700))
    }
    throw new Error(`The job is still running. Its ID is ${receipt.job_id}.`)
  }

  async download(file: ArtifactFile) {
    const response = await fetch(file.download_url)
    if (!response.ok) throw new Error(`The download failed (${response.status}).`)
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url; link.download = file.filename; link.click()
    URL.revokeObjectURL(url)
  }
}
