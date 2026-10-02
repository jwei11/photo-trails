import { Link, NavLink, Route, Routes } from 'react-router'
import TripMapPage from './map/TripMapPage.tsx'
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
          <Route path="/trips/:tripId" element={<TripMapPage />} />
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
  const { phase, processing, items, hashed, toHash } = useImport()
  if (phase === 'idle' && !processing) return null
  const finished = items.filter((i) => ['done', 'duplicate', 'failed'].includes(i.status)).length
  const label = phase === 'hashing' ? `Checking ${hashed}/${toHash}`
    : phase === 'uploading' ? `Importing ${finished}/${items.length}`
      : `Processing ${finished}/${items.length}`
  return (
    <Link to="/import" className={styles.indicator} aria-live="polite">{label}</Link>
  )
}
