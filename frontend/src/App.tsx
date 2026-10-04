import { Link, Navigate, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router'
import TripMapPage from './map/TripMapPage.tsx'
import styles from './App.module.css'
import LoginPage from './auth/LoginPage.tsx'
import { signOut, useMe } from './auth/session.ts'
import SharedTripPage from './shared/SharedTripPage.tsx'
import ImportPage from './imports/ImportPage.tsx'
import { useImport } from './imports/useImport.ts'
import SettingsPage from './settings/SettingsPage.tsx'
import TimelinePage from './TimelinePage.tsx'

export default function App() {
  const { data: me, isPending, isError } = useMe()
  const { pathname } = useLocation()
  // A shared trip is for anyone with the link, signed in or not.
  if (pathname.startsWith('/s/')) {
    return (
      <div className={styles.page}>
        <header className={styles.header}><span className={styles.title}>Trip Mapper</span></header>
        <main>
          <Routes><Route path="/s/:token" element={<SharedTripPage />} /></Routes>
        </main>
      </div>
    )
  }
  if (isPending) return null
  if (isError) return <p className={styles.page}>Couldn't reach the server. Reload to try again.</p>
  // Signed out: the login page, whatever the URL.
  if (!me.user) {
    return (
      <div className={styles.page}>
        {pathname === '/login' ? <LoginPage /> : <Navigate to="/login" replace />}
      </div>
    )
  }
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link to="/" className={styles.title}>Trip Mapper</Link>
        <nav className={styles.nav}>
          <NavLink to="/" end>Timeline</NavLink>
          <NavLink to="/import">Import</NavLink>
          <NavLink to="/settings">Settings</NavLink>
          <ImportIndicator />
          <SignOut email={me.user.email} />
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/login" element={<Navigate to="/" replace />} />
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

function SignOut({ email }: { email: string }) {
  const navigate = useNavigate()
  return (
    <span className={styles.account}>
      {email}
      <button
        className={styles.signOut}
        onClick={async () => { await signOut(); navigate('/login', { replace: true }) }}
      >
        Sign out
      </button>
    </span>
  )
}
