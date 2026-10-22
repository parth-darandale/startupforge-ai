import { useState } from 'react'
import { IntelligenceWorkspace } from '../../../../member1/frontend/src/modules/intelligence'
import { VisualStudioWorkspace } from '../../../../member2/frontend/src/modules/visualStudio/VisualStudioWorkspace'
import { ReportsWorkspace } from '../modules/reports/ReportsWorkspace'
import { ProjectWorkspace } from '../modules/workspace/ProjectWorkspace'
import './app.css'

type Page = 'intelligence' | 'studio' | 'reports' | 'workspace'

export function App() {
  const [page, setPage] = useState<Page>('intelligence')
  return <div className="sf-app-shell">
    <header className="sf-shell-header"><a className="sf-brand" href="#idea" onClick={(event) => { event.preventDefault(); setPage('intelligence') }}>
      <span className="sf-brand-mark">S</span><span>StartupForge</span></a>
      <nav aria-label="StartupForge workspace">
        <button className={page === 'intelligence' ? 'active' : ''} onClick={() => setPage('intelligence')}>Idea intelligence</button>
        <button className={page === 'studio' ? 'active' : ''} onClick={() => setPage('studio')}>Pitch and poster</button>
        <button className={page === 'reports' ? 'active' : ''} onClick={() => setPage('reports')}>Reports</button>
        <button className={page === 'workspace' ? 'active' : ''} onClick={() => setPage('workspace')}>Project workspace</button>
      </nav>
    </header>
    <main className="sf-shell-main">
      {page === 'intelligence' && <IntelligenceWorkspace />}
      {page === 'studio' && <VisualStudioWorkspace />}
      {page === 'reports' && <ReportsWorkspace />}
      {page === 'workspace' && <ProjectWorkspace onOpenStudio={() => setPage('studio')} onOpenReports={() => setPage('reports')} />}
    </main>
  </div>
}
