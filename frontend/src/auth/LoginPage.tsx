import { useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import { signInWithLink, useMe } from './session.ts'
import styles from './LoginPage.module.css'

export default function LoginPage() {
  const [params] = useSearchParams()
  const { data: me } = useMe()
  const link = params.get('link')
  const error = params.get('error')
  return (
    <div className={styles.box}>
      <h1>Sign in</h1>
      {error && <p className={styles.error} role="alert">{error}</p>}
      {link ? <LinkSignIn token={link} /> : (
        <>
          {me?.google && (
            // A real navigation, not fetch: the browser goes to Google and comes back.
            <a className={styles.google} href="/api/auth/google/start">Sign in with Google</a>
          )}
          <p className={styles.note}>
            Trip Mapper is invite-only. {me?.google
              ? 'Use the Google account whose email you were invited with.'
              : 'Ask whoever runs this server for a sign-in link.'}
          </p>
        </>
      )}
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
