import { useEffect, useMemo, useState } from 'react'
import type { Project, ProjectContext } from '../../../../../member1/frontend/src/modules/intelligence/types'
import { WorkspaceApi } from '../../../../../member3/frontend/src/api'
import type { ArtifactContent, ArtifactKind, ArtifactManifest, ModuleTemplate } from '../../../../../member3/frontend/src/api'

type SlideBlock = { type: string; text: string; source_ids: string[]; evidence_ids: string[]; claim_kind?: string | null; claim_id?: string | null; figure_id?: string | null }
type DeckSlide = { slide_id: string; title: string; body_blocks: SlideBlock[]; speaker_notes: string; source_ids: string[]; evidence_ids: string[] }
type PosterSection = { section_id: string; heading: string; body: string; source_ids: string[]; evidence_ids: string[]; claim_kind?: string | null; figure_id?: string | null }
type DeckContent = { title: string; slides: DeckSlide[] }
type PosterContent = { title: string; sections: PosterSection[] }
type EditableContent = DeckContent | PosterContent

export function VisualStudioWorkspace() {
  const api = useMemo(() => new WorkspaceApi(), [])
  const [projects, setProjects] = useState<Project[]>([])
  const [projectId, setProjectId] = useState('')
  const [context, setContext] = useState<ProjectContext | null>(null)
  const [kind, setKind] = useState<ArtifactKind>('pitch_deck')
  const [templates, setTemplates] = useState<ModuleTemplate[]>([])
  const [artifactList, setArtifactList] = useState<ArtifactManifest[]>([])
  const [manifest, setManifest] = useState<ArtifactManifest | null>(null)
  const [record, setRecord] = useState<ArtifactContent<EditableContent> | null>(null)
  const [content, setContent] = useState<EditableContent | null>(null)
  const [slideCount, setSlideCount] = useState(8)
  const [tone, setTone] = useState('professional')
  const [audience, setAudience] = useState('Potential customers and reviewers')
  const [busy, setBusy] = useState(false)
  const [jobLine, setJobLine] = useState('')
  const [error, setError] = useState('')
  const template = templates[0]

  async function refreshArtifacts(id = projectId, targetKind = kind) {
    if (!id) return
    const result = await api.artifacts(id, targetKind)
    setArtifactList(result.items)
  }

  async function openArtifact(id: string) {
    const [nextManifest, nextContent] = await Promise.all([api.artifact(id), api.content<EditableContent>(id)])
    setManifest(nextManifest); setRecord(nextContent); setContent(nextContent.content)
  }

  useEffect(() => {
    api.projects().then((result) => { setProjects(result.items); if (result.items[0]) setProjectId(result.items[0].project_id) })
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : 'Could not load projects.'))
  }, [api])

  useEffect(() => {
    setManifest(null); setRecord(null); setContent(null); setContext(null)
    if (!projectId) return
    Promise.all([api.context(projectId), refreshArtifacts(projectId, kind)]).then(([saved]) => setContext(saved))
      .catch(() => { setContext(null); setArtifactList([]) })
  }, [api, projectId])

  useEffect(() => {
    setManifest(null); setRecord(null); setContent(null)
    if (projectId) refreshArtifacts(projectId, kind).catch((caught: unknown) => setError(caught instanceof Error ? caught.message : 'Could not load artifacts.'))
    const query = kind === 'pitch_deck' ? 'pitch_deck' : 'poster'
    api.templates(`studio/templates?kind=${query}`).then((result) => setTemplates(result.templates))
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : 'Could not load templates.'))
  }, [api, kind])

  async function loadProject() {
    if (!projectId) return
    const [saved, generated] = await Promise.all([api.context(projectId), api.artifacts(projectId, kind)])
    setContext(saved); setArtifactList(generated.items)
  }

  async function generate() {
    if (!context || !template) return
    setBusy(true); setError(''); setJobLine('Submitting generation job…')
    try {
      const path = kind === 'pitch_deck' ? 'studio/pitch-decks' : 'studio/posters'
      const options = kind === 'pitch_deck'
        ? { template_id: template.template_id, slide_count: slideCount, tone }
        : { template_id: template.template_id, audience }
      const receipt = await api.generate(path, context, options)
      const job = await api.poll(receipt, (current) => setJobLine(`${current.stage.replaceAll('_', ' ')} · ${current.progress}%`))
      const artifactId = (job.result as { artifact_ids?: string[] } | null)?.artifact_ids?.[0]
      if (!artifactId) throw new Error('The job completed without returning an artifact.')
      await refreshArtifacts()
      await openArtifact(artifactId)
      setJobLine('Ready to preview and edit.')
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Generation failed.') }
    finally { setBusy(false) }
  }

  function updateSlide(index: number, field: 'title' | 'speaker_notes', value: string) {
    if (!content || !('slides' in content)) return
    setContent({ ...content, slides: content.slides.map((slide, i) => i === index ? { ...slide, [field]: value } : slide) })
  }

  function updateBlock(slideIndex: number, blockIndex: number, value: string) {
    if (!content || !('slides' in content)) return
    setContent({ ...content, slides: content.slides.map((slide, i) => i === slideIndex
      ? { ...slide, body_blocks: slide.body_blocks.map((block, j) => j === blockIndex ? { ...block, text: value } : block) } : slide) })
  }

  function updatePoster(index: number, field: 'heading' | 'body', value: string) {
    if (!content || !('sections' in content)) return
    setContent({ ...content, sections: content.sections.map((section, i) => i === index ? { ...section, [field]: value } : section) })
  }

  async function saveContent() {
    if (!manifest || !record || !content) return
    setBusy(true); setError('')
    try {
      const saved = await api.saveContent(manifest.artifact_id, record.content_revision, content)
      setManifest(saved.manifest)
      const next = await api.content<EditableContent>(manifest.artifact_id)
      setRecord(next); setContent(next.content); setJobLine(`Saved revision ${next.content_revision}. Render an export to download this revision.`)
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not save edits.') }
    finally { setBusy(false) }
  }

  async function renderCurrent() {
    if (!manifest || !record) return
    setBusy(true); setError(''); setJobLine('Rendering current content revision…')
    try {
      const format = manifest.kind === 'pitch_deck' ? 'pptx' : 'txt'
      const receipt = await api.render(manifest.artifact_id, record.content_revision, [format])
      const job = await api.poll(receipt, (current) => setJobLine(`${current.stage.replaceAll('_', ' ')} · ${current.progress}%`))
      await openArtifact(manifest.artifact_id); await refreshArtifacts()
      setJobLine((job.result as { file_ids?: string[] } | null)?.file_ids?.length ? 'Export is ready.' : 'Render complete.')
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'Could not render the export.') }
    finally { setBusy(false) }
  }

  return <section className="sf-module-page">
    <div className="sf-module-heading"><div><span className="sf-eyebrow">MEMBER 2 · PITCH AND VISUAL STUDIO</span><h1>Build presentation outputs</h1></div>
      <label className="sf-project-select">Project<select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
        {projects.length === 0 && <option value="">No saved projects</option>}
        {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.display_name}</option>)}
      </select></label>
    </div>
    <div className="sf-module-tabs" role="tablist" aria-label="Presentation output type">
      <button className={kind === 'pitch_deck' ? 'active' : ''} onClick={() => setKind('pitch_deck')}>Pitch deck</button>
      <button className={kind === 'poster' ? 'active' : ''} onClick={() => setKind('poster')}>Poster text</button>
    </div>
    {error && <div className="sf-module-alert" role="alert">{error}</div>}
    <div className="sf-module-grid">
      <article className="sf-module-card">
        <span className="sf-eyebrow">SAVED CONTEXT</span>
        {context ? <><h2>{context.idea.project_name}</h2><p>Version {context.context_version} · {context.critic.review_status.replaceAll('_', ' ')}</p>
          <p>{context.final_analysis.summary}</p>
          <button className="sf-mini-button" onClick={loadProject}>Reload saved context</button></> :
          <p>Save a reviewed analysis in Idea Intelligence before generating an output.</p>}
      </article>
      <article className="sf-module-card">
        <span className="sf-eyebrow">GENERATOR</span>
        <h2>{template?.name ?? 'Loading template…'}</h2>
        {kind === 'pitch_deck' ? <div className="sf-option-row">
          <label>Slides<select value={slideCount} onChange={(event) => setSlideCount(Number(event.target.value))}>{[5, 6, 7, 8, 9, 10, 11, 12].map((n) => <option key={n} value={n}>{n}</option>)}</select></label>
          <label>Tone<select value={tone} onChange={(event) => setTone(event.target.value)}><option value="professional">Professional</option><option value="bold">Bold</option><option value="academic">Academic</option></select></label>
        </div> : <label className="sf-field">Audience<input value={audience} maxLength={500} onChange={(event) => setAudience(event.target.value)} /></label>}
        <button className="sf-primary-button" disabled={!context || !template || busy} onClick={generate}>{busy ? 'Working…' : kind === 'pitch_deck' ? 'Generate PPTX and preview' : 'Generate poster text'}</button>
        {jobLine && <p className="sf-job-line">{jobLine}</p>}
      </article>
      <article className="sf-module-card sf-artifact-list">
        <span className="sf-eyebrow">SAVED OUTPUTS</span>
        {artifactList.length ? artifactList.map((item) => <button key={item.artifact_id} className={`sf-artifact-item ${manifest?.artifact_id === item.artifact_id ? 'selected' : ''}`} onClick={() => openArtifact(item.artifact_id)}>
          <span>{item.kind === 'pitch_deck' ? 'Pitch deck' : 'Poster'} · context v{item.context_version}</span><small>Revision {item.content_revision}</small></button>) : <p>No outputs for this project yet.</p>}
      </article>
    </div>
    {content && manifest && record && <section className="sf-editor-card">
      <div className="sf-editor-heading"><div><span className="sf-eyebrow">CONTENT PREVIEW AND EDITOR</span><h2>{manifest.kind === 'pitch_deck' ? 'Pitch deck' : 'Poster'} · revision {record.content_revision}</h2><p>Context version {manifest.context_version} · {manifest.context_hash.slice(0, 12)}</p></div>
        <div className="sf-editor-actions"><button className="sf-secondary-button" disabled={busy} onClick={saveContent}>Save edits</button><button className="sf-primary-button" disabled={busy} onClick={renderCurrent}>Render current revision</button></div>
      </div>
      <label className="sf-field">Output title<input value={content.title} onChange={(event) => setContent({ ...content, title: event.target.value })} /></label>
      {'slides' in content ? <div className="sf-content-list">{content.slides.map((slide, index) => <article className="sf-content-card" key={slide.slide_id}>
        <span className="sf-slide-count">SLIDE {String(index + 1).padStart(2, '0')}</span>
        <label className="sf-field">Title<input value={slide.title} onChange={(event) => updateSlide(index, 'title', event.target.value)} /></label>
        {slide.body_blocks.map((block, blockIndex) => <label className="sf-field" key={`${slide.slide_id}-${blockIndex}`}>
          <span className="sf-block-kind">{block.claim_kind ?? block.type}{block.evidence_ids.length ? ` · ${block.evidence_ids.length} evidence` : ''}</span>
          <textarea value={block.text} rows={3} onChange={(event) => updateBlock(index, blockIndex, event.target.value)} />
        </label>)}
        <label className="sf-field">Speaker notes<textarea value={slide.speaker_notes} rows={2} onChange={(event) => updateSlide(index, 'speaker_notes', event.target.value)} /></label>
      </article>)}</div> : <div className="sf-content-list">{content.sections.map((section, index) => <article className="sf-content-card" key={section.section_id}>
        <span className="sf-slide-count">SECTION {String(index + 1).padStart(2, '0')} · {section.claim_kind ?? 'content'}</span>
        <label className="sf-field">Heading<input value={section.heading} onChange={(event) => updatePoster(index, 'heading', event.target.value)} /></label>
        <label className="sf-field">Text<textarea value={section.body} rows={4} onChange={(event) => updatePoster(index, 'body', event.target.value)} /></label>
        <small>{section.evidence_ids.length} evidence references · {section.source_ids.length} sources</small>
      </article>)}</div>}
      <div className="sf-download-list"><strong>Exports</strong>{manifest.files.map((file) => <button key={file.file_id} onClick={() => api.download(file)}>
        Download {file.format.toUpperCase()} · revision {file.content_revision} · {(file.size_bytes / 1024).toFixed(0)} KB</button>)}</div>
    </section>}
  </section>
}
