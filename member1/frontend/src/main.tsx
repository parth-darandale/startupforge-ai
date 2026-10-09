import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { IntelligenceWorkspace } from './modules/intelligence'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <IntelligenceWorkspace />
  </StrictMode>,
)
