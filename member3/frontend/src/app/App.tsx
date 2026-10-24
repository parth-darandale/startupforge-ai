import { useRef, useState } from 'react'
import { IntelligenceWorkspace } from '../../../../member1/frontend/src/modules/intelligence'
import { VisualStudioWorkspace } from '../../../../member2/frontend/src/modules/visualStudio/VisualStudioWorkspace'
import { ReportsWorkspace } from '../modules/reports/ReportsWorkspace'
import { ProjectWorkspace } from '../modules/workspace/ProjectWorkspace'
import './app.css'

type Page = 'intelligence' | 'studio' | 'reports' | 'workspace'

export function App() {
  const [page, setPage] = useState<Page>('intelligence')
  const [shutdownState, setShutdownState] = useState<'idle' | 'stopping' | 'stopped'>('idle')
  const [shutdownError, setShutdownError] = useState<string | null>(null)
  const shutdownPending = useRef(false)

  async function shutdown() {
    if (shutdownPending.current) return
    shutdownPending.current = true
    setShutdownState('stopping')
    setShutdownError(null)
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 15_000)
    try {
      const response = await fetch('/api/v1/system/shutdown', {
        method: 'POST',
        headers: { 'X-StartupForge-Action': 'shutdown' },
        signal: controller.signal,
      })
      const data = await response.json().catch(() => null) as {
        status?: string
        error?: { message?: string }
      } | null
      if (!response.ok || data?.status !== 'shutting_down') {
        throw new Error(data?.error?.message ?? 'The app could not be shut down. Please try again.')
      }
      setShutdownState('stopped')
    } catch (error) {
      setShutdownState('idle')
      setShutdownError(error instanceof Error && error.name !== 'AbortError'
        ? error.message
        : 'The shutdown request did not complete. Please try again.')
      shutdownPending.current = false
    } finally {
      window.clearTimeout(timeout)
    }
  }

  const powerIcon = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
    <path d="M12 3v9M6.35 5.65a8 8 0 1 0 11.3 0" />
  </svg>

  if (shutdownState === 'stopped') return <main className="sf-shutdown-screen" aria-live="polite">
    <div className="sf-shutdown-message">
      <span className="sf-shutdown-mark">{powerIcon}</span>
      <h1>StartupForge is shut down</h1>
      <p>Launch <strong>StartupForge.exe</strong> to start again.</p>
    </div>
  </main>

  return <div className="sf-app-shell">
    <header className="sf-shell-header"><a className="sf-brand" href="#idea" onClick={(event) => { event.preventDefault(); setPage('intelligence') }}>
      <span className="sf-brand-mark">S</span><span>StartupForge</span></a>
      <nav aria-label="StartupForge workspace">
        <button className={page === 'intelligence' ? 'active' : ''} onClick={() => setPage('intelligence')}>Idea intelligence</button>
        <button className={page === 'studio' ? 'active' : ''} onClick={() => setPage('studio')}>Pitch and poster</button>
        <button className={page === 'reports' ? 'active' : ''} onClick={() => setPage('reports')}>Reports</button>
        <button className={page === 'workspace' ? 'active' : ''} onClick={() => setPage('workspace')}>Project workspace</button>
      </nav>
      <button className="sf-shutdown-button" onClick={() => void shutdown()} disabled={shutdownState === 'stopping'} aria-busy={shutdownState === 'stopping'}>
        {powerIcon}<span>{shutdownState === 'stopping' ? 'Stopping…' : 'Shutdown'}</span>
      </button>
    </header>
    {shutdownError && <div className="sf-shutdown-error" role="alert">
      <span>{shutdownError}</span><button onClick={() => void shutdown()}>Retry shutdown</button>
    </div>}
    <main className="sf-shell-main">
      {page === 'intelligence' && <IntelligenceWorkspace />}
      {page === 'studio' && <VisualStudioWorkspace />}
      {page === 'reports' && <ReportsWorkspace />}
      {page === 'workspace' && <ProjectWorkspace onOpenStudio={() => setPage('studio')} onOpenReports={() => setPage('reports')} />}
    </main>
  </div>
}
