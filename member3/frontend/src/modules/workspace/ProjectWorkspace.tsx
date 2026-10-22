import { useEffect, useMemo, useState } from 'react'
import type { Project } from '../../../../../member1/frontend/src/modules/intelligence/types'
import { WorkspaceApi } from '../../api'
import type { ArtifactKind, ArtifactManifest } from '../../api'

export function ProjectWorkspace({ onOpenStudio, onOpenReports }: { onOpenStudio: () => void; onOpenReports: () => void }) {
  const api = useMemo(() => new WorkspaceApi(), [])
  const [projects, setProjects] = useState<Project[]>([])
  const [selected, setSelected] = useState('')
  const [artifacts, setArtifacts] = useState<ArtifactManifest[]>([])
  const [outputs, setOutputs] = useState<ArtifactKind[]>(['pitch_deck', 'poster', 'report'])
  const [busy, setBusy] = useState(false)
  const [runStatus, setRunStatus] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    api.projects().then((result) => { setProjects(result.items); setSelected(result.items[0]?.project_id ?? '') })
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : 'Could not load projects.'))
  }, [api])
  useEffect(() => {
    if (!selected) { setArtifacts([]); return }
    api.artifacts(selected).then((result) => setArtifacts(result.items))
      .catch((caught: unknown) => setError(caught instanceof Error ? caught.message : 'Could not load saved outputs.'))
  }, [api, selected])
  const project = projects.find((item) => item.project_id === selected)
  const contextVersions = project?.latest_context_version ?? 0
  const byKind: [ArtifactKind, string][] = [['pitch_deck', 'Pitch decks'], ['poster', 'Posters'], ['report', 'Reports']]
  async function runAll() {
    if (!project || !outputs.length) return
    setBusy(true); setError(''); setRunStatus('Submitting full workflow…')
    try {
      const receipt = await api.run(project, outputs)
      const job = await api.poll(receipt, (current) => setRunStatus(`${current.stage.replaceAll('_', ' ')} · ${current.progress}%`))
      const result = job.result as { successful_outputs?: { kind: string }[]; failed_outputs?: { kind: string; error?: string }[] } | null
      await Promise.all([api.projects().then((response) => setProjects(response.items)), api.artifacts(project.project_id).then((response) => setArtifacts(response.items))])
      const succeeded = result?.successful_outputs?.map((item) => item.kind.replaceAll('_', ' ')).join(', ') || 'none'
      const failed = result?.failed_outputs?.map((item) => item.kind.replaceAll('_', ' ')).join(', ')
      setRunStatus(failed ? `Partial result. Ready: ${succeeded}. Needs attention: ${failed}.` : `All requested outputs are ready: ${succeeded}.`)
    } catch (caught) { setError(caught instanceof Error ? caught.message : 'The full workflow could not complete.') }
    finally { setBusy(false) }
  }
  return <section className="sf-module-page">
    <div className="sf-module-heading"><div><span className="sf-eyebrow">MEMBER 3 · PROJECT WORKSPACE</span><h1>Saved projects and outputs</h1></div>
      <label className="sf-project-select">Project<select value={selected} onChange={(event) => setSelected(event.target.value)}>
        {projects.length === 0 && <option value="">No saved projects</option>}{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.display_name}</option>)}
      </select></label>
    </div>
    {error && <div className="sf-module-alert" role="alert">{error}</div>}
    {project ? <div className="sf-module-grid">
      <article className="sf-module-card"><span className="sf-eyebrow">PROJECT</span><h2>{project.display_name}</h2>
        <p>{project.draft_idea_text}</p><div className="sf-workspace-stat"><span>Context versions</span><strong>{contextVersions}</strong></div>
        <div className="sf-workspace-stat"><span>Last updated</span><strong>{new Date(project.updated_at).toLocaleString()}</strong></div>
      </article>
      <article className="sf-module-card"><span className="sf-eyebrow">WORKFLOW</span><h2>Continue this project</h2>
        <p>Generate visual outputs or prepare the formal report from the latest saved context.</p>
        <div className="sf-workspace-actions"><button className="sf-secondary-button" onClick={onOpenStudio}>Pitch and poster</button><button className="sf-secondary-button" onClick={onOpenReports}>Reports</button></div>
        <div className="sf-combined-options"><span>Run a new reviewed analysis and generate:</span>{byKind.map(([kind, label]) => <label key={kind}><input type="checkbox" checked={outputs.includes(kind)} onChange={(event) => setOutputs((current) => event.target.checked ? [...current, kind] : current.filter((item) => item !== kind))} /> {label}</label>)}</div>
        <button className="sf-primary-button" disabled={busy || !outputs.length} onClick={runAll}>{busy ? 'Running…' : 'Run full workflow'}</button>
        {runStatus && <p className="sf-job-line">{runStatus}</p>}
      </article>
    </div> : <article className="sf-module-card"><p>Create and save a project in Idea Intelligence to start the workspace.</p></article>}
    <section className="sf-artifacts-board"><div className="sf-editor-heading"><div><span className="sf-eyebrow">ARTIFACT LIBRARY</span><h2>Versioned output files</h2></div><span>{artifacts.length} saved</span></div>
      {byKind.map(([kind, label]) => {
        const group = artifacts.filter((item) => item.kind === kind)
        return <article className="sf-artifact-group" key={kind}><h3>{label}</h3>{group.length ? group.map((artifact) => <div className="sf-artifact-row" key={artifact.artifact_id}>
          <div><strong>{artifact.kind === 'pitch_deck' ? 'Pitch deck' : artifact.kind === 'poster' ? 'Poster' : 'Report'}</strong><span>Context v{artifact.context_version} · content revision {artifact.content_revision}</span></div>
          <div className="sf-artifact-downloads">{artifact.files.map((file) => <button key={file.file_id} onClick={() => api.download(file)}>{file.format.toUpperCase()}</button>)}</div>
        </div>) : <p className="sf-muted">No {label.toLowerCase()} generated for this project.</p>}</article>
      })}
    </section>
  </section>
}
