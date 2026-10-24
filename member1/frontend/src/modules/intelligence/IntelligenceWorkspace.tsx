import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent, ReactNode } from 'react'
import { ApiError, IntelligenceApi } from './api'
import type {
  AnalysisResult, ContextDraft, DebateSession, Health,
  IdeaSpec, Job, ModelCatalog, Project, ProjectContext,
} from './types'
import { AnalysisView, Citations, CriticView, FiguresView, ResearchView } from './ResearchViews'
import './intelligence.css'

function exampleIdea(project_name: string, original_text: string, problem: string, solution: string,
  target_users: string[], domain: string, assumptions: string[], constraints: string[]): IdeaSpec {
  return { project_name, original_text, problem, solution, target_users, domain, assumptions, constraints,
    field_origins: { project_name: 'provided', problem: 'provided', solution: 'provided',
      target_users: 'provided', domain: 'inferred', assumptions: 'provided', constraints: 'provided' } }
}

const examples: { label: string; text: string; idea: IdeaSpec; markers: string[] }[] = [
  {
    label: 'Campus meals',
    text: 'Campus Meals is a subscription service for college students who struggle to find affordable, nutritious meals. Partner with local kitchens to offer weekly meal plans, dietary preferences, and scheduled campus pickup. Start with one campus and a small budget. Assume students will pay for convenience.',
    markers: ['campus meals', 'local kitchens', 'weekly meal plans'],
    idea: exampleIdea('Campus meals', 'Campus Meals is a subscription service for college students who struggle to find affordable, nutritious meals. Partner with local kitchens to offer weekly meal plans, dietary preferences, and scheduled campus pickup. Start with one campus and a small budget. Assume students will pay for convenience.', 'College students struggle to find affordable, nutritious meals.', 'Partner with local kitchens to offer weekly meal plans with dietary preferences and scheduled campus pickup.', ['College students'], 'Food services', ['Students will pay for convenience.'], ['Start with one campus.', 'Use a small budget.']),
  },
  {
    label: 'Smarter recycling',
    text: 'LoopLocal helps apartment residents sort household waste through a mobile guide and scheduled collection by local recyclers. The problem is confusing sorting rules and unreliable recycling pickup. Target urban apartment communities in India. Start in one neighborhood with a small team; assume community managers will support the service.',
    markers: ['looplocal', 'apartment residents', 'recycling pickup'],
    idea: exampleIdea('Smarter recycling', 'LoopLocal helps apartment residents sort household waste through a mobile guide and scheduled collection by local recyclers. The problem is confusing sorting rules and unreliable recycling pickup. Target urban apartment communities in India. Start in one neighborhood with a small team; assume community managers will support the service.', 'Apartment residents face confusing waste-sorting rules and unreliable recycling pickup.', 'Provide a mobile waste-sorting guide and scheduled collection by local recyclers.', ['Apartment residents in urban communities in India'], 'Waste management and recycling', ['Community managers will support the service.'], ['Start in one neighborhood.', 'Use a small team.']),
  },
  {
    label: 'Student study groups',
    text: 'StudyCircle matches college students into small peer study groups based on subjects, available time, and learning goals. Students often struggle to find consistent study partners. The solution is a scheduling app with group reminders and shared learning goals. Begin with one college, keeping student privacy and low operating cost as constraints.',
    markers: ['studycircle', 'college students', 'study partners'],
    idea: exampleIdea('Student study groups', 'StudyCircle matches college students into small peer study groups based on subjects, available time, and learning goals. Students often struggle to find consistent study partners. The solution is a scheduling app with group reminders and shared learning goals. Begin with one college, keeping student privacy and low operating cost as constraints.', 'College students struggle to find consistent study partners.', 'Match students into peer study groups by subject, availability, and learning goals, with scheduling, group reminders, and shared goals.', ['College students'], 'Education', [], ['Begin with one college.', 'Protect student privacy.', 'Keep operating costs low.']),
  },
]

function normalizeIdeaText(value: string) {
  return value.normalize('NFKC').toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, '')
}

function sampleForText(text: string) {
  const normalized = normalizeIdeaText(text)
  return examples.find((example) => normalizeIdeaText(example.text) === normalized)
}

function sampleForProject(project: Project) {
  return sampleForText(project.draft_idea_text) ?? examples.find((example) =>
    example.label.toLocaleLowerCase() === project.display_name.trim().toLocaleLowerCase()
      && example.markers.every((marker) => normalizeIdeaText(project.draft_idea_text).includes(normalizeIdeaText(marker))))
}

type Panel = 'reviewed' | 'sources' | 'figures' | 'critic' | 'original'
type Busy = 'extract' | 'analyze' | 'save' | 'load' | 'debate' | 'resume' | 'turn' | null
const panelLabels: Record<Panel, string> = {
  reviewed: 'Reviewed analysis', sources: 'Sources & evidence', figures: 'Statistics', critic: 'Critic review', original: 'Original analysis',
}

function Icon({ name, size = 19 }: { name: 'spark' | 'plus' | 'arrow' | 'check' | 'book' | 'chat' | 'download' | 'copy' | 'external'; size?: number }) {
  const paths: Record<string, ReactNode> = {
    spark: <><path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z" /><path d="m19 3 .6 1.4L21 5l-1.4.6L19 7l-.6-1.4L17 5l1.4-.6Z" /></>,
    plus: <><path d="M12 5v14M5 12h14" /></>,
    arrow: <><path d="M4 12h16m-6-6 6 6-6 6" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    book: <><path d="M12 5v15M3 5c3-1 6-1 9 1 3-2 6-2 9-1v14c-3-1-6-1-9 1-3-2-6-2-9-1Z" /></>,
    chat: <path d="M21 11a8 8 0 0 1-8 8H8l-5 3V11a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8Z" />,
    download: <><path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5" /></>,
    copy: <><rect x="8" y="8" width="13" height="13" rx="2" /><path d="M16 8V3H3v13h5" /></>,
    external: <><path d="M14 3h7v7m0-7-11 11M9 3H3v18h18v-6" /></>,
  }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>
}

function messageFrom(error: unknown): string {
  if (error instanceof ApiError) {
    const extra = error.info.request_id ? ` Reference: ${error.info.request_id}.` : ''
    const fields = error.info.details.fields as { path: string; message: string }[] | undefined
    const validation = fields?.length ? ` ${fields.map((field) => `${field.path.replace(/^body\./, '')}: ${field.message}`).join('; ')}.` : ''
    return `${error.info.message}${validation}${extra}`
  }
  return error instanceof Error ? error.message : 'Something went wrong. Please try again.'
}

function parseSourceUrls(text: string): { urls: string[]; error: string | null } {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  if (lines.length > 8) return { urls: [], error: 'Add at most 8 source URLs, one per line.' }
  const urls: string[] = []
  for (const [index, line] of lines.entries()) {
    try {
      const parsed = new URL(line)
      if (!/^https?:\/\//i.test(line) || !['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw new Error('Invalid URL')
      urls.push(parsed.href)
    } catch {
      return { urls: [], error: `Source ${index + 1}: enter a complete HTTP or HTTPS URL without a username or password.` }
    }
  }
  return { urls, error: null }
}

function Origin({ idea, field }: { idea: IdeaSpec; field: string }) {
  const origin = idea.field_origins[field]
  return origin && origin !== 'provided' ? <span className={`sf-origin sf-origin-${origin}`}>{origin === 'inferred' ? 'Inferred' : 'Needs clarification'}</span> : null
}

function debatePollTimeout(rounds: number) {
  // Allow the original turn plus its bounded citation corrections and summary.
  return Math.max(600_000, (rounds * 2 * 3 + 1) * 180_000 + 60_000)
}

export interface IntelligenceWorkspaceProps {
  apiBase?: string
  onContextSaved?: (context: ProjectContext) => void
}

/** A feature UI that works standalone or inside the final shared app. */
export function IntelligenceWorkspace({ apiBase = '/api/v1', onContextSaved }: IntelligenceWorkspaceProps) {
  const api = useMemo(() => new IntelligenceApi(apiBase), [apiBase])
  const uid = useId()
  const activeRequest = useRef<AbortController | null>(null)
  const busyRef = useRef(false)
  const workspaceRef = useRef<HTMLDivElement>(null)
  const mobileHistoryRef = useRef<HTMLDetailsElement>(null)
  const activeProjectRef = useRef<Project | null>(null)
  const [health, setHealth] = useState<Health | null>(null)
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog | null>(null)
  const [selectedModel, setSelectedModel] = useState<string | null>(null)
  const [modelCatalogError, setModelCatalogError] = useState(false)
  const [projects, setProjects] = useState<Project[]>([])
  const [project, setProject] = useState<Project | null>(null)
  const [rawIdea, setRawIdea] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [idea, setIdea] = useState<IdeaSpec | null>(null)
  const [maxSources, setMaxSources] = useState(5)
  const [sourceUrlsText, setSourceUrlsText] = useState('')
  const [researchRegion, setResearchRegion] = useState('')
  const [analysis, setAnalysis] = useState<AnalysisResult | null>(null)
  const [context, setContext] = useState<ContextDraft | null>(null)
  const [savedContext, setSavedContext] = useState<ProjectContext | null>(null)
  const [busy, setBusy] = useState<Busy>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [panel, setPanel] = useState<Panel>('reviewed')
  const [debateMode, setDebateMode] = useState<DebateSession['mode']>('user_vs_agent')
  const [userStance, setUserStance] = useState<DebateSession['user_stance']>('for')
  const [maxRounds, setMaxRounds] = useState('10')
  const [debate, setDebate] = useState<DebateSession | null>(null)
  const [turnText, setTurnText] = useState('')
  const isDemo = health?.mode === 'demo'
  const sourceUrlInput = useMemo(() => parseSourceUrls(sourceUrlsText), [sourceUrlsText])
  const roundCount = Number(maxRounds)
  const roundsValid = maxRounds.trim() !== '' && Number.isInteger(roundCount) && roundCount >= 1 && roundCount <= 100
  const figures = context?.figures ?? analysis?.figures ?? []
  const claims = context?.final_analysis.sections.flatMap((section) => section.claims) ?? []
  const selectedModelOption = modelCatalog?.models.find((item) => item.model_id === selectedModel)
  const selectedModelLabel = selectedModelOption?.label ?? 'Configured default'
  const modelUnavailable = modelCatalog?.mode === 'live'
    && (!selectedModelOption || !selectedModelOption.available)
  const researchModelId = analysis?.model_id ?? context?.execution.model_id

  function modelLabel(modelId: string) {
    return modelCatalog?.models.find((item) => item.model_id === modelId)?.label ?? modelId
  }

  function setActiveProject(next: Project | null) {
    activeProjectRef.current = next
    setProject(next)
  }

  useEffect(() => {
    const controller = new AbortController()
    Promise.allSettled([api.health(controller.signal), api.projects(controller.signal), api.models(controller.signal)]).then(([service, history, catalog]) => {
      if (controller.signal.aborted) return
      if (service.status === 'fulfilled') setHealth(service.value)
      else setError(messageFrom(service.reason))
      if (history.status === 'fulfilled') setProjects(history.value.items)
      if (catalog.status === 'fulfilled') {
        setModelCatalog(catalog.value)
        const configured = catalog.value.models.find((item) => item.model_id === catalog.value.default_model_id)
        setSelectedModel(configured?.model_id ?? null)
        setModelCatalogError(false)
      } else setModelCatalogError(true)
    })
    return () => { controller.abort(); activeRequest.current?.abort() }
  }, [api])

  async function refreshProjects(signal?: AbortSignal) {
    const history = await api.projects(signal)
    setProjects(history.items)
  }

  function clearResult() {
    setAnalysis(null); setContext(null); setSavedContext(null); setDebate(null); setJob(null); setTurnText('')
  }

  function newIdea() {
    if (busy) return
    if (mobileHistoryRef.current) mobileHistoryRef.current.open = false
    setActiveProject(null); setIdea(null); setRawIdea(''); setDisplayName(''); setSourceUrlsText(''); setResearchRegion(''); clearResult(); setError(null); setNotice(null)
    workspaceRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  async function operation(kind: Exclude<Busy, null>, action: (signal: AbortSignal) => Promise<void>) {
    if (busyRef.current) return
    busyRef.current = true
    activeRequest.current?.abort()
    const controller = new AbortController()
    activeRequest.current = controller
    setBusy(kind); setError(null); setNotice(null); setJob(null)
    try { await action(controller.signal) }
    catch (caught) { if (!controller.signal.aborted) setError(messageFrom(caught)) }
    finally { busyRef.current = false; if (!controller.signal.aborted) setBusy(null) }
  }

  async function extract(event: FormEvent) {
    event.preventDefault()
    if (busy || rawIdea.trim().length < 10) return
    await operation('extract', async (signal) => {
      clearResult(); setIdea(null)
      const hasProvidedName = !!displayName.trim()
      const projectName = displayName.trim() || rawIdea.trim().split(/\s+/).slice(0, 6).join(' ')
      const activeProject = activeProjectRef.current ?? project
      let created = activeProject
        ? await api.updateProject(activeProject, { display_name: projectName, draft_idea_text: rawIdea.trim() }, signal)
        : await api.createProject(projectName, rawIdea.trim(), signal)
      setActiveProject(created)
      const example = sampleForText(rawIdea.trim())
      let extracted: IdeaSpec
      if (example) {
        extracted = { ...example.idea, project_name: created.display_name }
      } else {
        const receipt = await api.extract(created.project_id, rawIdea.trim(), signal, selectedModel)
        const completed = await api.poll(receipt, signal, setJob)
        if (!completed.result) throw new Error('The extraction completed without structured idea fields.')
        extracted = { ...(completed.result as unknown as IdeaSpec), project_name: created.display_name,
          field_origins: { ...((completed.result as unknown as IdeaSpec).field_origins), project_name: hasProvidedName ? 'provided' : 'inferred' } }
      }
      created = await api.updateProject(created, { draft_idea: extracted }, signal)
      setActiveProject(created)
      setIdea(extracted)
      await refreshProjects(signal)
      setNotice(example ? 'Sample idea fields are filled in. Review them before researching.' : 'Review the inferred fields before researching.')
    })
  }

  function updateIdea(field: keyof IdeaSpec, value: string | string[]) {
    if (!idea) return
    setIdea({ ...idea, [field]: value, field_origins: { ...idea.field_origins, [field]: 'provided' } })
    clearResult()
  }

  async function analyze(event: FormEvent) {
    event.preventDefault()
    if (busy || !project || !idea || modelUnavailable) return
    if (!isDemo && sourceUrlInput.error) { setError(sourceUrlInput.error); return }
    await operation('analyze', async (signal) => {
      clearResult()
      const preparedIdea = { ...idea,
        target_users: idea.target_users.map((item) => item.trim()).filter(Boolean),
        assumptions: idea.assumptions.map((item) => item.trim()).filter(Boolean),
        constraints: idea.constraints.map((item) => item.trim()).filter(Boolean),
      }
      if (preparedIdea.target_users.length > 20 || preparedIdea.assumptions.length > 30 || preparedIdea.constraints.length > 30) {
        throw new Error('Use at most 20 target user groups, 30 assumptions, and 30 constraints. Put one entry on each line.')
      }
      setIdea(preparedIdea)
      const latestProject = JSON.stringify(project.draft_idea) === JSON.stringify(preparedIdea)
        ? project : await api.updateProject(project, { draft_idea: preparedIdea }, signal)
      setActiveProject(latestProject)
      const receipt = await api.analyze(latestProject.project_id, preparedIdea, maxSources, signal, isDemo ? [] : sourceUrlInput.urls, researchRegion, selectedModel)
      const completed = await api.poll(receipt, signal, setJob)
      if (!completed.result_url) throw new Error('The analysis completed without a result link.')
      const result = await api.analysis(completed.result_url, signal)
      setAnalysis(result); setContext(result.context_draft); setPanel('reviewed')
      setNotice('Research complete. Review the evidence, save the context, or optionally start a debate.')
    })
  }

  async function saveContext() {
    if (busy || !project || !context) return
    await operation('save', async (signal) => {
      try {
        const saved = await api.save(project, context, signal)
        setSavedContext(saved)
        setActiveProject({ ...project, latest_context_version: saved.context_version })
        onContextSaved?.(saved)
        await refreshProjects(signal)
        setNotice(`Version ${saved.context_version} saved.`)
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 409) {
          const refreshed = await api.project(project.project_id, signal)
          setActiveProject(refreshed)
          throw new Error(`This project now has context version ${refreshed.latest_context_version}. Your draft is preserved. Review it, then click Save context again to commit a new version.`)
        }
        throw caught
      }
    })
  }

  async function loadProject(selected: Project) {
    if (busy) return
    if (mobileHistoryRef.current) mobileHistoryRef.current.open = false
    await operation('load', async (signal) => {
      clearResult(); setIdea(null); setSourceUrlsText(''); setResearchRegion('')
      let loaded = await api.project(selected.project_id, signal)
      let restoredIdea: IdeaSpec | null = loaded.draft_idea ?? null
      let restoreNotice = ''
      if (!restoredIdea && loaded.latest_context_version > 0) {
        try { restoredIdea = (await api.context(loaded.project_id, signal)).idea }
        catch { /* The project remains usable even if its saved context cannot be read. */ }
      }
      if (!restoredIdea) {
        const example = sampleForProject(loaded)
        if (example) {
          restoredIdea = { ...example.idea, project_name: loaded.display_name }
          restoreNotice = 'Restored the complete fields for this sample idea.'
        } else {
          try {
            const receipt = await api.extract(loaded.project_id, loaded.draft_idea_text, signal, selectedModel)
            const completed = await api.poll(receipt, signal, setJob)
            if (completed.result) {
              const extracted = completed.result as unknown as IdeaSpec
              restoredIdea = { ...extracted, project_name: loaded.display_name,
                field_origins: { ...extracted.field_origins, project_name: 'provided' } }
              restoreNotice = 'Restored this project and filled its idea fields.'
            }
          } catch {
            restoreNotice = 'This older draft has no saved structured fields yet. Choose “Structure my idea” when the AI service is available.'
          }
        }
      }
      if (restoredIdea && JSON.stringify(loaded.draft_idea) !== JSON.stringify(restoredIdea)) {
        loaded = await api.updateProject(loaded, { draft_idea: restoredIdea }, signal)
      }
      setActiveProject(loaded); setDisplayName(loaded.display_name); setRawIdea(loaded.draft_idea_text)
      setIdea(restoredIdea)
      if (loaded.latest_context_version > 0) {
        const saved = await api.context(loaded.project_id, signal)
        setSavedContext(saved); setContext(saved); setIdea(saved.idea); setPanel('reviewed')
        setNotice(`Opened context version ${saved.context_version}.`)
      } else if (restoreNotice) setNotice(restoreNotice)
      else if (!restoredIdea) setNotice('This project has no saved research yet. Structure its idea to continue.')
      // Resume the latest completed research without requiring users to generate it again.
      const [analysisHistory, debateHistory, resumedDebateHistory] = await Promise.all([
        api.projectJobs(loaded.project_id, signal, 'analyze_idea'),
        api.projectJobs(loaded.project_id, signal, 'create_debate'),
        api.projectJobs(loaded.project_id, signal, 'resume_debate'),
      ])
      const analysisJobs = analysisHistory.jobs
        .sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at))
      const latest = analysisJobs[0]
      if (latest?.status === 'succeeded' && latest.result_url) {
        const result = await api.analysis(latest.result_url, signal)
        const latestIdea = result.context_draft.idea
        if (JSON.stringify(loaded.draft_idea) !== JSON.stringify(latestIdea)) {
          loaded = await api.updateProject(loaded, { draft_idea: latestIdea }, signal)
          setActiveProject(loaded)
        }
        setAnalysis(result); setContext(result.context_draft); setIdea(result.context_draft.idea)
        const committed = loaded.latest_context_version > 0 ? await api.context(loaded.project_id, signal) : null
        const matching = committed && committed.sources.map((source) => source.source_id).join() === result.context_draft.sources.map((source) => source.source_id).join()
          && JSON.stringify(committed.final_analysis) === JSON.stringify(result.context_draft.final_analysis)
          && JSON.stringify(committed.idea) === JSON.stringify(result.context_draft.idea)
        setSavedContext(matching ? committed : null)
        setNotice(matching ? `Opened saved research · version ${committed.context_version}.` : 'Opened your latest research draft.')
      } else if (latest?.status === 'failed') {
        setError(`Your most recent research job failed: ${latest.error?.message || 'The job could not complete.'} ${loaded.latest_context_version ? 'Your saved context is still available.' : 'You can run research again.'}`)
      } else if (latest && ['queued', 'running'].includes(latest.status)) {
        setJob(latest)
        setNotice('A previous research job is still running. Reopen this project shortly to retrieve its results.')
      }
      const debateJobs = [...debateHistory.jobs, ...resumedDebateHistory.jobs].filter((item) => item.result_url)
        .sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at))
      if (latest?.status === 'succeeded') {
        const analysisId = latest.result?.analysis_id ?? latest.result_url?.split('/').at(-1)
        for (const latestDebate of debateJobs) {
          const sessionUrl = latestDebate.result_url!
          const session = await api.debate(sessionUrl, signal)
          if (session.analysis_id !== analysisId) continue
          setDebate(session)
          // A competing tab may have a newer failed resume while the original
          // job is still generating. Reconnect to the active job for this session.
          let pendingJob = debateJobs.find((item) => item.result_url === sessionUrl
            && ['queued', 'running'].includes(item.status)) ?? null
          if (!pendingJob && session.mode === 'user_vs_agent' && session.status === 'active') {
            const replies = await api.projectJobs(loaded.project_id, signal, 'debate_turn', session.debate_id)
            pendingJob = replies.jobs.find((item) => ['queued', 'running'].includes(item.status)) ?? null
          }
          if (pendingJob) {
            setNotice('Reconnected to your debate. Completed arguments are shown as it continues.')
            const receipt = { job_id: pendingJob.job_id, project_id: loaded.project_id,
              operation: pendingJob.operation, status_url: `/api/v1/jobs/${pendingJob.job_id}` }
            const completed = await api.poll(receipt, signal, async (update) => {
              setJob(update)
              setDebate(await api.debate(update.result_url || sessionUrl, signal))
            }, { timeoutMs: pendingJob.operation !== 'debate_turn' && session.mode === 'agent_vs_agent'
              ? debatePollTimeout(session.max_rounds) : 780_000 })
            if (completed.result_url) setDebate(await api.debate(completed.result_url, signal))
            setNotice('Your debate is up to date.')
          }
          break
        }
      }
    })
  }

  function downloadContext() {
    if (!savedContext) return
    const url = URL.createObjectURL(new Blob([JSON.stringify(savedContext, null, 2)], { type: 'application/json' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${savedContext.idea.project_name.replace(/[^a-zA-Z0-9_-]/g, '_')}-context-v${savedContext.context_version}.json`
    anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1_000)
  }

  async function copyContext() {
    if (!savedContext) return
    try { await navigator.clipboard.writeText(JSON.stringify(savedContext, null, 2)); setNotice('Saved project context copied.') }
    catch { setError('Clipboard access is unavailable. Use Download JSON to share the saved context.') }
  }

  async function startDebate() {
    if (busy || !project || !analysis || !roundsValid || modelUnavailable) return
    await operation('debate', async (signal) => {
      setDebate(null); setTurnText('')
      const receipt = await api.startDebate(project.project_id, analysis.analysis_id, debateMode, userStance, roundCount, signal, selectedModel)
      const completed = await api.poll(receipt, signal, async (update) => {
        setJob(update)
        if (update.result_url) setDebate(await api.debate(update.result_url, signal))
      }, debateMode === 'agent_vs_agent' ? { timeoutMs: debatePollTimeout(roundCount) } : undefined)
      if (!completed.result_url) throw new Error('The debate completed without a session link.')
      setDebate(await api.debate(completed.result_url, signal))
    })
  }

  async function resumeDebate() {
    if (busy || !debate || debate.status !== 'failed') return
    const session = debate
    await operation('resume', async (signal) => {
      const receipt = await api.resumeDebate(session.debate_id, signal)
      const sessionUrl = `intelligence/debates/${session.debate_id}`
      const completed = await api.poll(receipt, signal, async (update) => {
        setJob(update)
        setDebate(await api.debate(update.result_url || sessionUrl, signal))
      }, session.mode === 'agent_vs_agent' ? { timeoutMs: debatePollTimeout(session.max_rounds) } : undefined)
      setDebate(await api.debate(completed.result_url || sessionUrl, signal))
      setNotice('Your debate is up to date.')
    })
  }

  async function sendTurn(event: FormEvent) {
    event.preventDefault()
    if (busy || !debate || !turnText.trim()) return
    await operation('turn', async (signal) => {
      const receipt = await api.sendTurn(debate.debate_id, turnText.trim(), signal)
      const completed = await api.poll(receipt, signal, setJob, { timeoutMs: 780_000 })
      const updated = await api.debate(completed.result_url || `intelligence/debates/${debate.debate_id}`, signal)
      setDebate(updated); setTurnText('')
    })
  }

  function navigateTabs(event: KeyboardEvent<HTMLButtonElement>, current: Panel) {
    if (event.altKey || event.ctrlKey || event.metaKey) return
    const tabs = Object.keys(panelLabels) as Panel[]
    const index = tabs.indexOf(current)
    let target: number | null = null
    if (event.key === 'ArrowRight') target = (index + 1) % tabs.length
    if (event.key === 'ArrowLeft') target = (index - 1 + tabs.length) % tabs.length
    if (event.key === 'Home') target = 0
    if (event.key === 'End') target = tabs.length - 1
    if (target !== null) {
      event.preventDefault()
      setPanel(tabs[target])
      event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[target]?.focus()
    }
  }

  return <div className="sf-app" ref={workspaceRef}>
    <a className="sf-skip" href={`#${uid}-main`}>Skip to workspace</a>
    <aside className="sf-sidebar">
      <div className="sf-brand"><span className="sf-brand-mark"><Icon name="spark" size={25} /></span><span>StartupForge<span className="sf-brand-ai"> AI</span></span></div>
      <div className="sf-sidebar-active"><Icon name="chat" /><span>Idea Intelligence</span></div>
      <button className="sf-new-project" onClick={newIdea} disabled={!!busy}><Icon name="plus" /><span>New idea</span></button>
      <div className="sf-history-title"><span>Recent projects</span><span>{projects.length}</span></div>
      <nav aria-label="Recent projects" className="sf-history">
        {projects.length === 0 && <p>Your projects will appear here.</p>}
        {projects.map((item) => <button key={item.project_id} onClick={() => loadProject(item)} disabled={!!busy} aria-current={project?.project_id === item.project_id ? 'page' : undefined} className={project?.project_id === item.project_id ? 'sf-history-selected' : ''}>
          <Icon name="book" size={16} /><span><strong>{item.display_name}</strong><small>{item.latest_context_version ? `Saved context v${item.latest_context_version}` : 'Draft'}</small></span>
        </button>)}
      </nav>
    </aside>
    <div className="sf-workspace">
      <header className="sf-topbar"><strong>Idea Intelligence</strong><span className={`sf-service ${health && (isDemo || health.capabilities?.live_configured) ? 'sf-service-online' : ''}`}><span />{health ? (isDemo ? 'Demo mode' : health.capabilities?.live_configured ? 'Live AI configured' : 'Live setup required') : 'Connecting'}</span></header>
      <main id={`${uid}-main`} className="sf-main">
        {health?.configuration_message && <div className="sf-alert sf-alert-error" role="alert"><strong>Live research needs configuration.</strong><p>{health.configuration_message}</p></div>}
        <details className="sf-mobile-history" ref={mobileHistoryRef}>
          <summary><Icon name="book" size={16} /><span>Recent projects</span><span className="sf-mobile-history-count">{projects.length}</span><span aria-hidden="true">⌄</span></summary>
          <nav aria-label="Recent projects on small screens">
            {projects.length === 0 && <p>Your projects will appear here.</p>}
            {projects.map((item) => <button key={item.project_id} type="button" onClick={() => loadProject(item)} disabled={!!busy} aria-current={project?.project_id === item.project_id ? 'page' : undefined}>
              <span><strong>{item.display_name}</strong><small>{item.latest_context_version ? `Saved context v${item.latest_context_version}` : 'Draft'}</small></span><Icon name="arrow" size={15} />
            </button>)}
          </nav>
        </details>
        {isDemo && <div className="sf-demo-banner"><p><strong>Demo mode.</strong> Sources, statistics and debates use sample data.</p></div>}
        {health?.mode === 'live' && context?.execution.mode === 'demo' && <div className="sf-alert sf-alert-notice"><p>This project's research uses sample data. New debate responses use live AI.</p></div>}
        <div className="sf-hero"><h1>Pressure-test your<br className="sf-desktop-break" /> startup idea.</h1></div>
        <ol className="sf-stepper" aria-label="Your workflow">
          <li className={idea ? 'sf-step-complete' : 'sf-step-current'}><span>{idea ? <Icon name="check" size={14} /> : '1'}</span><strong>Define</strong></li>
          <li className={context ? 'sf-step-complete' : idea ? 'sf-step-current' : ''}><span>{context ? <Icon name="check" size={14} /> : '2'}</span><strong>Research</strong></li>
          <li className={debate ? 'sf-step-complete' : context ? 'sf-step-current' : ''}><span>{debate ? <Icon name="check" size={14} /> : '3'}</span><strong>Debate (optional)</strong></li>
        </ol>
        {error && <div className="sf-alert sf-alert-error" role="alert"><p>{error}</p><button onClick={() => setError(null)} aria-label="Dismiss error">×</button></div>}
        {notice && <div className="sf-alert sf-alert-notice" role="status"><Icon name="check" size={17} /><p>{notice}</p><button onClick={() => setNotice(null)} aria-label="Dismiss notification">×</button></div>}
        {busy && <div className="sf-progress-box" role="status" aria-live="polite"><div className="sf-progress-heading"><span className="sf-spinner" /><strong>{busy === 'extract' ? 'Structuring your idea' : busy === 'analyze' ? 'Researching and reviewing' : busy === 'debate' ? 'Preparing your debate' : busy === 'resume' ? 'Resuming your debate' : busy === 'turn' ? 'Considering your argument' : busy === 'save' ? 'Saving the reviewed context' : 'Opening your project'}</strong><span>{job ? `${job.progress}%` : 'Please wait'}</span></div>{job && <><progress value={job.progress} max={100} aria-label="Job progress" /><p>{job.stage.replaceAll('_', ' ')}{job.status === 'queued' ? ' · Queued' : ''}</p></>}</div>}
        {!idea && <div className="sf-entry-grid"><section className="sf-card sf-idea-entry"><div className="sf-card-heading"><h2>Start with an idea</h2></div>
          <form onSubmit={extract}>
            <label className="sf-field-label" htmlFor={`${uid}-idea`}>What do you want to build?</label>
            <textarea id={`${uid}-idea`} value={rawIdea} onChange={(event) => setRawIdea(event.target.value)} placeholder="Describe the problem, your solution, who it helps, and any constraints." minLength={10} maxLength={12000} rows={7} required disabled={!!busy} />
            <div className="sf-input-help"><span>At least 10 characters</span><span>{rawIdea.length.toLocaleString()} / 12,000</span></div>
            <div className="sf-example-row"><span>Try an example</span>{examples.map((example) => <button key={example.label} type="button" className="sf-example" disabled={!!busy} onClick={() => { setRawIdea(example.text); setDisplayName(example.label) }}>{example.label}</button>)}</div>
            <div className="sf-form-footer"><button className="sf-button sf-button-primary" type="submit" disabled={!!busy || rawIdea.trim().length < 10}>Structure my idea <Icon name="arrow" size={17} /></button></div>
          </form>
        </section></div>}
        {idea && <section className="sf-card sf-definition"><details open={!context}>
          <summary><div><span className="sf-eyebrow">Your idea</span><h2>{idea.project_name}</h2></div><span className="sf-edit-label">Review & edit <span aria-hidden="true">⌄</span></span></summary>
          <form onSubmit={analyze} onInvalidCapture={(event) => {
            let details = (event.target as HTMLElement).closest('details')
            while (details) { details.open = true; details = details.parentElement?.closest('details') ?? null }
          }}>
            <div className="sf-fields-grid">
              <div><label className="sf-field-label" htmlFor={`${uid}-problem`}>Problem</label><textarea id={`${uid}-problem`} value={idea.problem} maxLength={3000} rows={4} required disabled={!!busy} onChange={(event) => updateIdea('problem', event.target.value)} /><Origin idea={idea} field="problem" /></div>
              <div><label className="sf-field-label" htmlFor={`${uid}-solution`}>Proposed solution</label><textarea id={`${uid}-solution`} value={idea.solution} maxLength={3000} rows={4} required disabled={!!busy} onChange={(event) => updateIdea('solution', event.target.value)} /><Origin idea={idea} field="solution" /></div>
              <div className="sf-full-width"><label className="sf-field-label" htmlFor={`${uid}-target_users`}>Target users <span>one per line</span></label><textarea id={`${uid}-target_users`} value={idea.target_users.join('\n')} rows={2} disabled={!!busy} onChange={(event) => updateIdea('target_users', event.target.value.split('\n'))} /><Origin idea={idea} field="target_users" /></div>
            </div>
            <details className="sf-optional-fields"><summary>Details and research options</summary>
              <div className="sf-model-setting">
                <label className="sf-field-label" htmlFor={`${uid}-model`}>AI model</label>
                <select id={`${uid}-model`} value={selectedModel ?? ''} onChange={(event) => setSelectedModel(event.target.value || null)} disabled={!!busy || !modelCatalog || modelCatalog.mode === 'demo'} aria-describedby={`${uid}-model-help`}>
                  {(!modelCatalog || modelCatalog.mode === 'demo' || !selectedModel) && <option value="">{modelCatalog?.mode === 'demo' ? 'Demo (sample data)' : modelCatalogError ? 'Configured default' : modelCatalog ? modelCatalog.models.some((item) => item.available) ? 'Choose a model' : 'No model available' : 'Loading models…'}</option>}
                  {modelCatalog?.mode !== 'demo' && modelCatalog?.models.map((item) => <option key={item.model_id} value={item.model_id} disabled={!item.available} title={item.unavailable_reason ?? item.description}>{item.label}{item.experimental ? ' — experimental' : item.model_id === modelCatalog.default_model_id ? ' — default' : ''}{!item.available ? ' (unavailable)' : ''}</option>)}
                </select>
                <p className="sf-model-help" id={`${uid}-model-help`}>{modelCatalogError ? 'Model list unavailable. New jobs use the configured default.' : modelCatalog?.mode === 'demo' ? 'Demo mode uses sample responses.' : modelUnavailable ? modelCatalog.models.some((item) => item.available) ? `${selectedModelOption?.label ?? 'The configured model'} is unavailable. Choose another installed model.` : 'No installed model is available. Start the local model service or import a model.' : `${selectedModelOption?.description ?? 'Choose a model for new research and debates.'} Saved results keep their original model.`}</p>
              </div>
              <div className="sf-fields-grid">
                <div><label className="sf-field-label" htmlFor={`${uid}-project-name`}>Project name</label><input id={`${uid}-project-name`} value={idea.project_name} maxLength={200} required disabled={!!busy} onChange={(event) => updateIdea('project_name', event.target.value)} /><Origin idea={idea} field="project_name" /></div>
                <div><label className="sf-field-label" htmlFor={`${uid}-domain`}>Domain</label><input id={`${uid}-domain`} value={idea.domain} maxLength={200} required disabled={!!busy} onChange={(event) => updateIdea('domain', event.target.value)} /><Origin idea={idea} field="domain" /></div>
                {(['assumptions', 'constraints'] as const).map((field) => <div key={field}><label className="sf-field-label" htmlFor={`${uid}-${field}`}>{field === 'assumptions' ? 'Assumptions' : 'Constraints'} <span>one per line</span></label><textarea id={`${uid}-${field}`} value={idea[field].join('\n')} rows={3} disabled={!!busy} onChange={(event) => updateIdea(field, event.target.value.split('\n'))} /><Origin idea={idea} field={field} /></div>)}
              </div>
            <label className="sf-field-label" htmlFor={`${uid}-research-region`}>Market region <span>optional</span></label>
            <input id={`${uid}-research-region`} value={researchRegion} onChange={(event) => setResearchRegion(event.target.value)} maxLength={100} disabled={!!busy || isDemo} placeholder="e.g. India" />
            <label className="sf-field-label" htmlFor={`${uid}-source-urls`}>Report or article URLs <span>optional · one per line</span></label>
            <textarea id={`${uid}-source-urls`} value={sourceUrlsText} onChange={(event) => setSourceUrlsText(event.target.value)} rows={3} disabled={!!busy || isDemo} placeholder="https://example.org/public-report" aria-invalid={!isDemo && !!sourceUrlInput.error} aria-describedby={`${uid}-source-url-help${!isDemo && sourceUrlInput.error ? ` ${uid}-source-url-error` : ''}`} />
            <div className="sf-input-help" id={`${uid}-source-url-help`}><span>{isDemo ? 'Demo uses sample sources.' : health?.providers?.discovery === 'bing_news' ? 'News search by default; up to 8 supplied URLs replace it.' : 'General web search by default; up to 8 supplied URLs replace it.'}</span></div>
            {!isDemo && sourceUrlInput.error && <p className="sf-field-error" id={`${uid}-source-url-error`} role="alert">{sourceUrlInput.error}</p>}
            <label className="sf-field-label" htmlFor={`${uid}-source-count`}>Maximum research sources</label><select id={`${uid}-source-count`} value={maxSources} onChange={(event) => setMaxSources(Number(event.target.value))} disabled={!!busy}>{Array.from({ length: 8 }, (_, index) => <option key={index + 1} value={index + 1}>{index + 1} {index === 0 ? 'source' : 'sources'}</option>)}</select>
            <details className="sf-original-input"><summary>View original idea</summary><p>{idea.original_text}</p></details>
            </details>
            {!isDemo && sourceUrlInput.error && <p className="sf-field-error" role="alert">Check the source URL under Details and research options.</p>}
            <div className="sf-research-controls"><button className="sf-button sf-button-primary" type="submit" disabled={!!busy || modelUnavailable || (!isDemo && !!sourceUrlInput.error)}>{context ? 'Research again' : 'Research my idea'} <Icon name="arrow" size={17} /></button></div>
          </form>
        </details></section>}
        {context && <>
          <details id={`${uid}-research-review`} className="sf-research-results" open key={analysis?.analysis_id ?? `${project?.project_id}-${savedContext?.context_version}`}>
          <summary className="sf-result-heading"><div><h2>Research results</h2>{researchModelId && <span className="sf-model-attribution">Model: {modelLabel(researchModelId)}</span>}</div><span className="sf-research-toggle"><span className={`sf-review-status sf-review-${context.critic.review_status === 'ready' ? 'ready' : 'ready_with_gaps'}`}>{context.critic.review_status === 'ready' ? 'Review complete' : 'Evidence gaps remain'}</span><svg className="sf-research-chevron" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg></span></summary>
          <div className="sf-metrics"><div><span>{context.sources.length}</span><p>{context.execution.mode === 'demo' ? 'Fixture sources' : 'Research sources'}</p></div><div><span>{context.evidence.length}</span><p>Traceable excerpts</p></div><div><span>{figures.length}</span><p>Sourced statistics</p></div><div><span>{claims.filter((claim) => claim.kind !== 'supported').length}</span><p>Assumptions & unknowns</p></div></div>
          <section className="sf-card sf-research-card"><div className="sf-card-heading"><h2>Research & review</h2></div>
            {!!context.warnings.length && <details className="sf-warning-notes"><summary>{context.warnings.length} research {context.warnings.length === 1 ? 'note' : 'notes'}</summary><ul>{context.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></details>}
            <div className="sf-tabs" role="tablist" aria-label="Research panels">{(Object.keys(panelLabels) as Panel[]).map((item) => <button key={item} id={`${uid}-tab-${item}`} type="button" role="tab" aria-selected={panel === item} aria-controls={`${uid}-panel`} tabIndex={panel === item ? 0 : -1} onClick={() => setPanel(item)} onKeyDown={(event) => navigateTabs(event, item)}>{panelLabels[item]}{item === 'figures' && <span>{figures.length}</span>}</button>)}</div>
            <div className="sf-panel" id={`${uid}-panel`} role="tabpanel" aria-labelledby={`${uid}-tab-${panel}`} tabIndex={0}>
              {panel === 'reviewed' && <AnalysisView report={context.final_analysis} context={context} />}
              {panel === 'original' && <AnalysisView report={context.initial_analysis} context={context} />}
              {panel === 'sources' && <ResearchView context={context} />}
              {panel === 'figures' && <FiguresView figures={figures} context={context} />}
              {panel === 'critic' && <CriticView context={context} />}
            </div>
          </section>
          </details>
          <section className="sf-card sf-debate-card" aria-labelledby={`${uid}-debate-title`}>
            <div className="sf-card-heading"><div><span className="sf-eyebrow sf-accent">Optional debate room</span><h2 id={`${uid}-debate-title`}>How strong is the idea?</h2></div><button type="button" className="sf-text-button" onClick={() => document.getElementById(`${uid}-save-context`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })} disabled={!!busy}>Skip debate</button></div>
            {!debate && <div className="sf-debate-setup">
              <div className="sf-mode-grid">
                <button type="button" className={`sf-mode-option ${debateMode === 'user_vs_agent' ? 'sf-mode-selected' : ''}`} aria-pressed={debateMode === 'user_vs_agent'} onClick={() => setDebateMode('user_vs_agent')} disabled={!!busy}><strong>You vs. AI</strong><span>Take a side against the AI.</span></button>
                <button type="button" className={`sf-mode-option ${debateMode === 'agent_vs_agent' ? 'sf-mode-selected' : ''}`} aria-pressed={debateMode === 'agent_vs_agent'} onClick={() => setDebateMode('agent_vs_agent')} disabled={!!busy}><strong>Pro vs. Skeptic</strong><span>Watch two agents debate.</span></button>
              </div>
              <div className="sf-debate-controls">
                {debateMode === 'user_vs_agent' && <fieldset className="sf-stance"><legend>Your position</legend><button type="button" className={userStance === 'for' ? 'sf-stance-selected' : ''} aria-pressed={userStance === 'for'} disabled={!!busy} onClick={() => setUserStance('for')}>For the idea</button><button type="button" className={userStance === 'against' ? 'sf-stance-selected' : ''} aria-pressed={userStance === 'against'} disabled={!!busy} onClick={() => setUserStance('against')}>Against the idea</button></fieldset>}
                <div><label className="sf-field-label" htmlFor={`${uid}-rounds`}>Debate rounds</label><input id={`${uid}-rounds`} type="number" min={1} max={100} step={1} inputMode="numeric" value={maxRounds} onChange={(event) => setMaxRounds(event.target.value)} disabled={!!busy} aria-invalid={!roundsValid} aria-describedby={`${uid}-rounds-help`} /><span className={roundsValid ? 'sf-round-help' : 'sf-field-error'} id={`${uid}-rounds-help`}>{roundsValid ? '1–100 rounds' : 'Enter a whole number from 1 to 100.'}</span></div>
                <button className="sf-button sf-button-primary" onClick={startDebate} disabled={!!busy || modelUnavailable || !analysis || !roundsValid}>{debateMode === 'user_vs_agent' ? 'Enter the debate' : 'Watch the debate'} <Icon name="arrow" size={17} /></button>
              </div>
              {!isDemo && <p className="sf-muted">Model: {selectedModelLabel}. Change it under Details and research options.</p>}
              {!analysis && <p className="sf-muted">Research this idea again to start a new debate.</p>}
            </div>}
            {debate && <div className="sf-debate-session"><div className="sf-debate-session-header"><div><span>{debate.mode === 'user_vs_agent' ? 'You vs. AI' : 'Pro vs. Skeptic'}</span><span className="sf-muted">{debate.max_rounds} {debate.max_rounds === 1 ? 'round' : 'rounds'} · {debate.status === 'completed' ? 'Complete' : debate.status === 'failed' ? 'Interrupted' : 'In progress'}</span>{debate.model_id && <span className="sf-muted">Model: {modelLabel(debate.model_id)}</span>}</div><button className="sf-text-button" onClick={() => { setDebate(null); setTurnText('') }} disabled={!!busy}>New debate</button></div>
              <div className="sf-transcript" aria-label="Debate transcript" aria-live="polite">
                {debate.turns.length === 0 && <p className="sf-muted">Preparing the opening argument.</p>}
                {debate.turns.map((turn) => <article className={`sf-debate-turn sf-turn-${turn.speaker}`} key={turn.turn_id}>
                  <div className="sf-turn-avatar" aria-hidden="true">{turn.speaker === 'user' ? 'Y' : turn.speaker === 'pro' ? '+' : turn.speaker === 'skeptic' ? '?' : 'M'}</div><div className="sf-turn-content"><div className="sf-turn-heading"><strong>{turn.speaker === 'user' ? 'You' : turn.speaker === 'pro' ? 'Pro' : turn.speaker === 'skeptic' ? 'Skeptic' : 'Moderator'}</strong><span>{turn.stance === 'for' ? 'For the idea' : turn.stance === 'against' ? 'Against the idea' : 'Review'}</span><small>{turn.round_number === 0 ? 'Opening argument' : `Round ${turn.round_number}`}</small></div><p>{turn.message}</p><Citations ids={turn.evidence_ids} context={context} />{!!turn.figures?.length && <details className="sf-citations"><summary>Statistics cited in this argument</summary><FiguresView figures={turn.figures} context={context} /></details>}</div>
                </article>)}
              </div>
              {debate.history_compacted && <details className="sf-citations"><summary>Long debate context</summary><p>{debate.history_note || 'The agents use recent arguments in full and shorter excerpts from earlier rounds. Your complete transcript remains available above.'}</p></details>}
              {debate.summary && <div className="sf-debate-summary"><h3>Debate takeaways</h3><p>{debate.summary}</p></div>}
              {debate.mode === 'user_vs_agent' && debate.status === 'active' && <form onSubmit={sendTurn} className="sf-turn-form"><label className="sf-field-label" htmlFor={`${uid}-argument`}>Your argument <span>{debate.user_stance === 'for' ? 'Defend the idea' : 'Challenge the idea'}</span></label><textarea id={`${uid}-argument`} value={turnText} onChange={(event) => setTurnText(event.target.value)} maxLength={5000} rows={3} required placeholder="Make your case or respond to the AI." disabled={!!busy} /><div className="sf-turn-footer"><span>{turnText.length.toLocaleString()} / 5,000</span><button className="sf-button sf-button-primary" type="submit" disabled={!!busy || !turnText.trim()}>Send argument <Icon name="arrow" size={17} /></button></div></form>}
              {debate.status === 'failed' && <div className="sf-gap-box"><p>The debate was interrupted. Resume from the last completed argument.</p><button type="button" className="sf-button sf-button-secondary" onClick={resumeDebate} disabled={!!busy}>Resume debate <Icon name="arrow" size={17} /></button></div>}
            </div>}
          </section>
          <section id={`${uid}-save-context`} className="sf-share-card"><div><h2>Save project context</h2>{savedContext && <span className="sf-saved-label"><Icon name="check" size={16} /> Version {savedContext.context_version} saved</span>}</div><div className="sf-share-actions">{!savedContext && <button className="sf-button sf-button-primary" onClick={saveContext} disabled={!!busy}>Save context</button>}<button className="sf-button sf-button-secondary" onClick={downloadContext} disabled={!savedContext || !!busy}><Icon name="download" size={16} /> Download JSON</button><button className="sf-button sf-button-secondary" onClick={copyContext} disabled={!savedContext || !!busy}><Icon name="copy" size={16} /> Copy JSON</button></div></section>
        </>}
      </main>
    </div>
  </div>
}
