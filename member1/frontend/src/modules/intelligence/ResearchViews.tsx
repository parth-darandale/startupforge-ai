import type { AnalysisReport, ContextDraft, Figure, Source } from './types'

function safeLink(url: string): string | null {
  try {
    const parsed = new URL(url)
    return ['https:', 'http:'].includes(parsed.protocol) ? parsed.href : null
  } catch { return null }
}

function ExternalIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M14 3h7v7m0-7-11 11M9 3H3v18h18v-6" /></svg>
}

function SourceLabel({ source }: { source?: Source }) {
  if (!source) return <span className="sf-muted">Source unavailable</span>
  const href = source.kind === 'web' ? safeLink(source.url) : null
  return href
    ? <a href={href} target="_blank" rel="noopener noreferrer">{source.title} <ExternalIcon /></a>
    : <span>{source.title}{source.kind === 'fixture' && <small className="sf-fixture-label"> · Demo source</small>}</span>
}

export function Citations({ ids, context }: { ids: string[]; context: ContextDraft }) {
  if (!ids.length) return null
  const uniqueIds = [...new Set(ids)]
  return <details className="sf-citations">
    <summary>{uniqueIds.length === 1 ? 'Supporting excerpt' : `${uniqueIds.length} supporting excerpts`}</summary>
    {uniqueIds.map((id) => {
      const evidence = context.evidence.find((item) => item.evidence_id === id)
      const source = context.sources.find((item) => item.source_id === evidence?.source_id)
      return <div className="sf-citation" key={id}>
        <SourceLabel source={source} />
        {evidence && <blockquote>{evidence.excerpt}</blockquote>}
        <small>{id}{evidence?.locator ? ` · ${evidence.locator}` : ''}</small>
      </div>
    })}
  </details>
}

export function AnalysisView({ report, context }: { report: AnalysisReport; context: ContextDraft }) {
  return <div className="sf-analysis">
    <div className="sf-report-summary"><p>{report.summary}</p></div>
    {report.sections.map((section) => <section className="sf-analysis-section" key={section.section_id}>
      <h3>{section.heading}</h3>
      {section.claims.length === 0 && <p className="sf-muted">No findings.</p>}
      {section.claims.map((claim) => <div className="sf-claim" key={claim.claim_id}>
        <span className={`sf-claim-type sf-claim-type-${claim.kind}`}>{claim.kind === 'supported' ? (context.execution.mode === 'demo' ? 'Demo-supported' : claim.text.startsWith('Source excerpt:') ? 'Source quote' : 'Supported') : claim.kind === 'assumption' ? 'Assumption' : 'Unknown'}</span>
        <p>{claim.text}</p>
        <Citations ids={claim.evidence_ids} context={context} />
      </div>)}
    </section>)}
    {!!report.gaps.length && <div className="sf-gap-box"><h3>Open questions</h3><ul>{report.gaps.map((gap, index) => <li key={index}>{gap}</li>)}</ul></div>}
  </div>
}

export function FiguresView({ figures, context }: { figures: Figure[]; context: ContextDraft }) {
  if (!figures.length) return <div className="sf-empty"><h3>No sourced numbers found</h3><p>The available excerpts contain no suitable statistics.</p></div>
  return <>
    <p className="sf-muted sf-panel-intro">Quoted numbers are not startup market estimates.{context.execution.mode === 'demo' ? ' These are demonstration figures, not live market evidence.' : ' Check the source and qualifications.'}</p>
    <div className="sf-figures">{figures.map((figure) => <article className="sf-figure" key={figure.figure_id}>
      <span className="sf-figure-number">{figure.display_value}</span>
      {!figure.excerpt.startsWith(figure.metric) && <h3>{figure.metric}</h3>}
      <blockquote>{figure.excerpt}</blockquote>
      <SourceLabel source={context.sources.find((source) => source.source_id === figure.source_id)} />
      <small className="sf-qualification">{figure.qualification}</small>
      <Citations ids={[figure.evidence_id]} context={context} />
    </article>)}</div>
  </>
}

export function ResearchView({ context }: { context: ContextDraft }) {
  return <div className="sf-source-list">
    {context.execution.mode === 'demo' && <p className="sf-muted sf-panel-intro">Demo sources and excerpts; not live research.</p>}
    {!context.sources.length && <p>No sources found.</p>}
    {context.sources.map((source) => {
      const excerpts = context.evidence.filter((evidence) => evidence.source_id === source.source_id)
      return <article className="sf-source-card" key={source.source_id}>
        <div className="sf-source-heading"><div><h3><SourceLabel source={source} /></h3><small>{source.publisher || 'Publisher unspecified'} · {source.kind === 'fixture' ? 'Demo source' : `Retrieved ${new Date(source.retrieved_at).toLocaleDateString()}`}</small></div></div>
        {excerpts.map((evidence) => <div className="sf-excerpt" key={evidence.evidence_id}>
          <blockquote>{evidence.excerpt}</blockquote>
          <small className="sf-muted">{evidence.evidence_id}{evidence.locator ? ` · ${evidence.locator}` : ''}</small>
        </div>)}
      </article>
    })}
  </div>
}

export function CriticView({ context }: { context: ContextDraft }) {
  const critic = context.critic
  return <div className="sf-critic">
    <div className="sf-report-summary"><h3>{critic.review_status.replaceAll('_', ' ')}</h3><p>{critic.revision_summary || 'Review complete.'}</p></div>
    {critic.issues.length === 0 && <p className="sf-muted">No issues recorded.</p>}
    {critic.issues.map((issue) => <article className="sf-critic-issue" key={issue.issue_id}>
      <span className={`sf-claim-type sf-severity-${issue.severity}`}>{issue.severity} priority</span>
      <p>{issue.reason}</p>
      {issue.resolution && <div className="sf-resolution"><span>{issue.resolution}</span></div>}
      <Citations ids={issue.evidence_ids} context={context} />
      {issue.claim_id && <details className="sf-citations"><summary>Claim reference</summary><small>{issue.claim_id}</small></details>}
    </article>)}
    {!!critic.unresolved_gaps.length && <div className="sf-gap-box"><h3>Evidence gaps</h3><ul>{critic.unresolved_gaps.map((gap, index) => <li key={index}>{gap}</li>)}</ul></div>}
  </div>
}
