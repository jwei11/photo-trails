import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { signInWithLink, startDemo, useMe } from './session.ts'
import styles from './LoginPage.module.css'

export default function LoginPage() {
  const [params] = useSearchParams()
  const { data: me } = useMe()
  const link = params.get('link')
  const error = params.get('error')
  return (
    <div className={styles.box}>
      <h1>Trip Mapper</h1>
      <p className={styles.intro}>
        Drop in the photos from a trip: it reads their times and GPS, works out where you went
        and when, and puts each trip on a map you can share.
      </p>
      {error && <p className={styles.error} role="alert">{error}</p>}
      {link ? <LinkSignIn token={link} /> : (
        <>
          {me?.demo && <TryDemo />}
          {me?.google && (
            // A real navigation, not fetch: the browser goes to Google and comes back.
            <a className={styles.google} href="/api/auth/google/start">
              {me.signup ? 'Sign in or sign up with Google' : 'Sign in with Google'}
            </a>
          )}
          <p className={styles.note}>
            {me?.signup
              ? 'New accounts can hold up to 200 photos, while places last.'
              : me?.google
                ? 'Accounts are invite-only: use the Google account whose email you were invited with.'
                : 'Accounts are invite-only: ask whoever runs this server for a sign-in link.'}
          </p>
        </>
      )}
    </div>
  )
}

function TryDemo() {
  const navigate = useNavigate()
  const [failed, setFailed] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  async function go() {
    setBusy(true)
    try {
      const tripId = await startDemo()
      navigate(tripId ? `/trips/${tripId}` : '/', { replace: true })
    } catch (e) {
      setFailed((e as Error).message)
      setBusy(false)
    }
  }
  return (
    <div className={styles.demo}>
      {failed && <p className={styles.error} role="alert">{failed}</p>}
      <button className="primary" onClick={go} disabled={busy}>Try the demo</button>
      <p className={styles.note}>
        No account needed: you get your own copy of a sample trip to explore, edit, and share
        for 24 hours, and can import up to 30 photos of your own.
      </p>
    </div>
  )
}

// Opening a link only shows this button; signing in waits for the click, so a mail
// scanner or chat preview that fetches the link can't use it up.
function LinkSignIn({ token }: { token: string }) {
  const navigate = useNavigate()
  const [failed, setFailed] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  async function go() {
    setBusy(true)
    try {
      await signInWithLink(token)
      navigate('/', { replace: true })
    } catch (e) {
      setFailed((e as Error).message)
      setBusy(false)
    }
  }
  return (
    <>
      {failed && <p className={styles.error} role="alert">{failed}</p>}
      <button className={styles.google} onClick={go} disabled={busy}>Sign in with this link</button>
    </>
  )
}
