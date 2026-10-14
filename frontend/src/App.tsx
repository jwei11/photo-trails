import { useState } from 'react'
import { Link, Navigate, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router'
import TripMapPage from './map/TripMapPage.tsx'
import styles from './App.module.css'
import LoginPage from './auth/LoginPage.tsx'
import { signOut, useMe, type User } from './auth/session.ts'
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
          <SignOut user={me.user} />
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

function SignOut({ user }: { user: User }) {
  const navigate = useNavigate()
  const guest = user.kind === 'guest'
  return (
    <span className={styles.account}>
      {guest ? <DemoNotice expiresAt={user.expires_at} /> : user.email}
      <button
        className={styles.signOut}
        onClick={async () => { await signOut(); navigate('/login', { replace: true }) }}
      >
        {guest ? 'End demo' : 'Sign out'}
      </button>
    </span>
  )
}

// A guest's account, and everything in it, is deleted when it expires.
function DemoNotice({ expiresAt }: { expiresAt: string | null }) {
  // Read once, when the notice first shows: an hour count needn't tick.
  const [now] = useState(() => Date.now())
  const hours = expiresAt ? Math.max(1, Math.round((Date.parse(expiresAt) - now) / 3_600_000)) : null
  return <span title="Everything in this demo account is deleted when it ends.">
    Demo account{hours !== null && `, deleted in ${hours} h`}
  </span>
}
