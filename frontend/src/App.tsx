import { Link, NavLink, Route, Routes } from 'react-router'
import styles from './App.module.css'
import ImportPage from './imports/ImportPage.tsx'
import { useImport } from './imports/useImport.ts'
import SettingsPage from './settings/SettingsPage.tsx'
import TimelinePage from './TimelinePage.tsx'

export default function App() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link to="/" className={styles.title}>Trip Mapper</Link>
        <nav className={styles.nav}>
          <NavLink to="/" end>Timeline</NavLink>
          <NavLink to="/import">Import</NavLink>
          <NavLink to="/settings">Settings</NavLink>
          <ImportIndicator />
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<TimelinePage />} />
          <Route path="/import" element={<ImportPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<p>Page not found.</p>} />
        </Routes>
      </main>
    </div>
  )
}

// Shown on every page while an import runs, since it keeps going when you navigate away.
function ImportIndicator() {
  const { phase, items, hashed, toHash } = useImport()
  if (phase === 'idle') return null
  const finished = items.filter((i) => i.status === 'done' || i.status === 'duplicate').length
  return (
    <Link to="/import" className={styles.indicator} aria-live="polite">
      {phase === 'hashing' ? `Checking ${hashed}/${toHash}` : `Importing ${finished}/${items.length}`}
    </Link>
  )
}
