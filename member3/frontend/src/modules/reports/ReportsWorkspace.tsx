import { useEffect, useMemo, useState } from 'react'
import type { Project, ProjectContext } from '../../../../../member1/frontend/src/modules/intelligence/types'
import { WorkspaceApi } from '../../api'
import type { ArtifactContent, ArtifactFile, ArtifactManifest, ModuleTemplate } from '../../api'
import { formatCreatedAt } from '../../formatDateTime'

type ReportSection = { section_id: string; heading: string; paragraphs: string[]; source_ids: string[]; evidence_ids: string[]; claim_kinds: string[] }
type ReportContent = { title: string; sections: ReportSection[]; bibliography: { source_id: string; title: string; url: string; publisher?: string | null; retrieved_at: string }[] }

const CLAIM_LABELS: Record<string, string> = {
  supported: 'Sourced', assumption: 'Assumption',
  unknown: 'Open question', warning: 'Review note', recommendation: 'Proposed',
}

function newestFilesByFormat(files: ArtifactFile[], revision?: number) {
  const latest = new Map<string, ArtifactFile>()
  for (const file of files) {
    if (revision !== undefined && file.content_revision !== revision) continue
    const existing = latest.get(file.format)
    if (!existing || file.created_at >= existing.created_at) latest.set(file.format, file)
  }
  return [...latest.values()].sort((left, right) => left.format.localeCompare(right.format))
}

export function ReportsWorkspace() {
  const api = useMemo(() => new WorkspaceApi(), [])
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [context, setContext] = useState<ProjectContext | null>(null)
  const [template, setTemplate] = useState<ModuleTemplate | null>(null)
  const [formats, setFormats] = useState<string[]>(['docx', 'pdf'])
  const [artifacts, setArtifacts] = useState<ArtifactManifest[]>([])
  const [manifest, setManifest] = useState<ArtifactManifest | null>(null)
  const [record, setRecord] = useState<ArtifactContent<ReportContent> | null>(null)
  const [content, setContent] = useState<ReportContent | null>(null)
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')
  const [editingTitle, setEditingTitle] = useState(false)
  const [editingSections, setEditingSections] = useState<string[]>([])
  const [dirty, setDirty] = useState(false)

  async function refresh(id = projectId) {
    if (!id) return
    const result = await api.artifacts(id, 'report')
    setArtifacts(result.items)
  }
  async function open(id: string) {
    const [nextManifest, nextContent] = await Promise.all([api.artifact(id), api.content<ReportContent>(id)])
    setManifest(nextManifest); setRecord(nextContent); setContent(nextContent.content)
    setEditingTitle(false); setEditingSections([]); setDirty(false)
  }

  useEffect(() => {
    api.projects().then((result) => { setProjects(result.items); if (result.items[0]) setProjectId(result.items[0].project_id) })
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : 'Could not load projects.'))
    api.templates('reports/templates').then((result) => setTemplate(result.templates[0] ?? null))
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : 'Could not load report templates.'))
  }, [api])

  useEffect(() => {
    setManifest(null); setRecord(null); setContent(null); setContext(null)
    setEditingTitle(false); setEditingSections([]); setDirty(false)
    setError(''); setStatus('')
    if (!projectId) return
    Promise.all([api.context(projectId), api.artifacts(projectId, 'report')]).then(([saved, generated]) => {
      setContext(saved); setArtifacts(generated.items)
    }).catch(() => { setContext(null); setArtifacts([]) })
  }, [api, projectId])

  async function generate() {
    if (!context || !template) return
    setBusy(true); setError(''); setStatus('Submitting report job…')
    try {
      const receipt = await api.generate('reports/generations', context, { template_id: template.template_id, formats })
      const job = await api.poll(receipt, (current) => setStatus(`${current.stage.replaceAll('_', ' ')} · ${current.progress}%`))
      const artifactId = (job.result as { artifact_ids?: string[] } | null)?.artifact_ids?.[0]
      if (!artifactId) throw new Error('The job completed without returning a report artifact.')
      await refresh(); await open(artifactId); setStatus('Report ready to preview and edit.')
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Report generation failed.') }
    finally { setBusy(false) }
  }

  function updateSection(index: number, field: 'heading' | 'paragraphs', value: string, paragraphIndex = 0) {
    if (!content) return
    setDirty(true)
    const sections = content.sections.map((section, i) => {
      if (i !== index) return section
      if (field === 'heading') return { ...section, heading: value }
      return { ...section, paragraphs: section.paragraphs.map((paragraph, p) => p === paragraphIndex ? value : paragraph) }
    })
    setContent({ ...content, sections })
  }

  async function save() {
    if (!manifest || !record || !content) return
    setBusy(true); setError('')
    try {
      const saved = await api.saveContent(manifest.artifact_id, record.content_revision, content)
      setManifest(saved.manifest)
      const next = await api.content<ReportContent>(manifest.artifact_id)
      setRecord(next); setContent(next.content); setDirty(false); setStatus(`Saved report revision ${next.content_revision}.`)
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not save report edits.') }
    finally { setBusy(false) }
  }

  async function render() {
    if (!manifest || !record) return
    setBusy(true); setError(''); setStatus('Rendering selected report revision…')
    try {
      const receipt = await api.render(manifest.artifact_id, record.content_revision, formats)
      await api.poll(receipt, (current) => setStatus(`${current.stage.replaceAll('_', ' ')} · ${current.progress}%`))
      await refresh(); await open(manifest.artifact_id); setStatus('Exports are ready.')
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not render this report.') }
    finally { setBusy(false) }
  }

  const currentExports = manifest && record ? newestFilesByFormat(manifest.files, record.content_revision) : []
  const currentExportIds = new Set(currentExports.map((file) => file.file_id))
  const previousExports = manifest?.files.filter((file) => !currentExportIds.has(file.file_id)) ?? []

  function toggleSection(id: string) {
    setEditingSections((current) => current.includes(id) ? current.filter((section) => section !== id) : [...current, id])
  }

  return <section className="sf-module-page">
    <div className="sf-module-heading"><div><span className="sf-eyebrow">MEMBER 3 · REPORTS AND PROJECT WORKSPACE</span><h1>Prepare a formal report</h1></div>
      <label className="sf-project-select">Project<select disabled={busy} value={projectId} onChange={(event) => setProjectId(event.target.value)}>
        {projects.length === 0 && <option value="">No saved projects</option>}
        {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.display_name}</option>)}
      </select></label>
    </div>
    {error && <div className="sf-module-alert" role="alert">{error}</div>}
    <div className="sf-module-grid sf-report-grid">
      <article className="sf-module-card"><span className="sf-eyebrow">REVIEWED CONTEXT</span>
        {context ? <><h2>{context.idea.project_name}</h2><p>Version {context.context_version} · {context.critic.review_status === 'ready' ? 'review complete' : 'evidence gaps remain'}</p><p>{context.final_analysis.summary}</p></> : <p>Save a reviewed analysis before building a report.</p>}
      </article>
      <article className="sf-module-card"><span className="sf-eyebrow">REPORT TEMPLATE</span><h2>{template?.name ?? 'Loading template…'}</h2>
        <details className="sf-options"><summary>Options <span>{formats.length ? formats.map((format) => format.toUpperCase()).join(' + ') : 'Choose a format'}</span></summary>
        <div className="sf-checkbox-row"><label><input disabled={busy} type="checkbox" checked={formats.includes('docx')} onChange={(event) => setFormats((current) => event.target.checked ? [...current, 'docx'] : current.filter((format) => format !== 'docx'))} /> DOCX</label>
          <label><input disabled={busy} type="checkbox" checked={formats.includes('pdf')} onChange={(event) => setFormats((current) => event.target.checked ? [...current, 'pdf'] : current.filter((format) => format !== 'pdf'))} /> PDF</label></div>
        </details>
        <button className="sf-primary-button" disabled={!context || !template || !formats.length || busy} onClick={generate}>{busy ? 'Working…' : 'Generate report'}</button>
        {status && <p className="sf-job-line">{status}</p>}
      </article>
      <article className="sf-module-card sf-artifact-list"><span className="sf-eyebrow">SAVED REPORTS</span>
        {artifacts.length ? artifacts.map((item) => {
          const latestFiles = newestFilesByFormat(item.files, item.content_revision)
          return <button className={`sf-artifact-item ${manifest?.artifact_id === item.artifact_id ? 'selected' : ''}`} key={item.artifact_id} onClick={() => open(item.artifact_id)}>
            <span>Context v{item.context_version}</span><small>Revision {item.content_revision} · {latestFiles.length ? latestFiles.map((file) => file.format.toUpperCase()).join(', ') : 'no current exports'}</small>
            <time dateTime={item.created_at}>Created {formatCreatedAt(item.created_at)}</time></button>
        }) : <p>No reports for this project yet.</p>}
      </article>
    </div>
    {content && manifest && record && <section className="sf-editor-card">
      <div className="sf-editor-heading"><div><span className="sf-eyebrow">REPORT PREVIEW</span><h2>Revision {record.content_revision}</h2><p>Context version {manifest.context_version}{dirty ? ' · Unsaved edits' : ''}</p></div>
        <div className="sf-editor-actions">{dirty && <button className="sf-secondary-button" disabled={busy} onClick={save}>Save edits</button>}<button className="sf-primary-button" disabled={busy || dirty || !formats.length} onClick={render}>Render current revision</button></div>
      </div>
      <div className="sf-preview-title"><h3>{content.title}</h3><button className="sf-mini-button" disabled={busy} aria-expanded={editingTitle} onClick={() => setEditingTitle(!editingTitle)}>{editingTitle ? 'Done' : 'Edit title'}</button></div>
      {editingTitle && <label className="sf-field">Report title<input disabled={busy} value={content.title} onChange={(event) => { setContent({ ...content, title: event.target.value }); setDirty(true) }} /></label>}
      <div className="sf-content-list">{content.sections.map((section, index) => {
        const editing = editingSections.includes(section.section_id)
        return <article className="sf-content-card" key={section.section_id}>
          <div className="sf-content-card-heading"><span className="sf-slide-count">SECTION {String(index + 1).padStart(2, '0')}</span><button className="sf-mini-button" disabled={busy} aria-expanded={editing} aria-label={`${editing ? 'Done editing' : 'Edit'} report section ${index + 1}`} onClick={() => toggleSection(section.section_id)}>{editing ? 'Done' : 'Edit'}</button></div>
          {editing ? <>
            <label className="sf-field">Heading<input disabled={busy} value={section.heading} onChange={(event) => updateSection(index, 'heading', event.target.value)} /></label>
            {section.paragraphs.map((paragraph, paragraphIndex) => <label className="sf-field" key={`${section.section_id}-${paragraphIndex}`}>Paragraph {paragraphIndex + 1}<textarea disabled={busy} rows={3} value={paragraph} onChange={(event) => updateSection(index, 'paragraphs', event.target.value, paragraphIndex)} /></label>)}
            <small>{section.source_ids.length} sources · {section.evidence_ids.length} evidence references</small>
          </> : <><h3 className="sf-preview-heading">{section.heading}</h3>
            {[...new Set(section.claim_kinds)].filter((kind) => CLAIM_LABELS[kind]).map((kind) => <span className="sf-claim-label" key={kind}>{CLAIM_LABELS[kind]}</span>)}
            {section.paragraphs.map((paragraph, paragraphIndex) => <p className="sf-preview-paragraph" key={`${section.section_id}-${paragraphIndex}`}>{paragraph}</p>)}</>}
        </article>
      })}</div>
      <div className="sf-download-list"><strong>Revision {record.content_revision} exports</strong>
        {currentExports.length ? currentExports.map((file) => <button key={file.file_id} onClick={() => api.download(file)}>
          Download {file.format.toUpperCase()} · {(file.size_bytes / 1024).toFixed(0)} KB</button>) : <span>Render this revision to create its downloads.</span>}
        {previousExports.length > 0 && <details className="sf-prior-downloads"><summary>Earlier exports ({previousExports.length})</summary>
          <div>{previousExports.map((file) => <button key={file.file_id} onClick={() => api.download(file)}>
            Download {file.format.toUpperCase()} · revision {file.content_revision} · {(file.size_bytes / 1024).toFixed(0)} KB</button>)}</div>
        </details>}
      </div>
    </section>}
  </section>
}
