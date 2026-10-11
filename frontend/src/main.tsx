import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './dashboard.css'
import './connections.css'
import './dashboard-overrides.css'
import './pages.css'

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
