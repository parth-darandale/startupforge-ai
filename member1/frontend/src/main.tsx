import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from '../../../member3/frontend/src/app/App'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
