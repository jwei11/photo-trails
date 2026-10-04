import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { sharesApi, type Precision, type Share } from '../api.ts'
import { formatMoment, plural } from '../format.ts'
import { useEdit } from '../useEdit.ts'
import styles from './SharePanel.module.css'

/** Share links for one trip: what a link would leave out, making one, and turning one off. */
export default function SharePanel({ tripId }: { tripId: string }) {
  const sharing = useQuery({ queryKey: ['shares', tripId], queryFn: () => sharesApi.forTrip(tripId) })
  const edit = useEdit()
  const [precision, setPrecision] = useState<Precision>('city')
  const [exactDates, setExactDates] = useState(false)

  if (sharing.isPending) return <div className={styles.panel}>Loading…</div>
  if (sharing.error) return <div className={styles.panel} role="alert">Couldn't load sharing: {sharing.error.message}</div>
  const s = sharing.data
  const active = s.shares.filter((x) => !x.revoked_at)

  return (
    <div className={styles.panel}>
      <h3>Share this trip</h3>
      <p className={styles.muted}>
        Anyone with a link can see this trip's map and photos, without an account. Filenames,
        cameras, and exact positions are never shown.
      </p>
      <ul className={styles.notes}>
        {!s.has_homes && (
          <li className={styles.warn}>
            You haven't set a home, so photos taken near it can't be left out.{' '}
            <Link to="/settings">Set one in Settings</Link>.
          </li>
        )}
        {s.hidden_near_home > 0 && <li>{plural(s.hidden_near_home, 'photo')} near a home won't be shown.</li>}
        {s.hidden_recent > 0 && <li>{plural(s.hidden_recent, 'photo')} from the last week won't be shown.</li>}
      </ul>

      {!s.can_share ? (
        <p>
          {s.shareable_from
            ? `You can share this trip from ${formatMoment(s.shareable_from)}: a week after it ends, so a link never says you're away right now.`
            : 'This trip has no dated photos to share.'}
        </p>
      ) : (
        <form
          className={styles.form}
          onSubmit={(e) => {
            e.preventDefault()
            edit.mutate(() => sharesApi.create(tripId, precision, exactDates))
          }}
        >
          <fieldset>
            <legend>Show locations</legend>
            <label>
              <input type="radio" name="precision" checked={precision === 'city'} onChange={() => setPrecision('city')} />
              {' '}At the nearest town or city (recommended)
            </label>
            <label>
              <input type="radio" name="precision" checked={precision === 'neighbourhood'}
                onChange={() => setPrecision('neighbourhood')} />
              {' '}To about a kilometre
            </label>
          </fieldset>
          <label>
            <input type="checkbox" checked={exactDates} onChange={(e) => setExactDates(e.target.checked)} />
            {' '}Show exact dates (otherwise the month, and "day 1", "day 2"…)
          </label>
          <button type="submit" className="primary" disabled={edit.isPending}>Create link</button>
        </form>
      )}
      {edit.error && <p role="alert" className={styles.error}>{edit.error.message}</p>}

      {active.length > 0 && (
        <ul className={styles.links} aria-label="Links">
          {active.map((share) => (
            <ShareLink key={share.id} share={share} onRevoke={() => edit.mutate(() => sharesApi.revoke(share.id))}
              disabled={edit.isPending} />
          ))}
        </ul>
      )}
    </div>
  )
}

function ShareLink({ share, onRevoke, disabled }: { share: Share; onRevoke: () => void; disabled: boolean }) {
  const url = `${window.location.origin}${share.path}`
  const [copied, setCopied] = useState(false)
  return (
    <li>
      <input readOnly value={url} aria-label="Share link" onFocus={(e) => e.target.select()} />
      <button type="button" className="small" onClick={async () => {
        await navigator.clipboard.writeText(url)
        setCopied(true)
      }}>
        {copied ? 'Copied' : 'Copy'}
      </button>
      <button type="button" className="small" onClick={onRevoke} disabled={disabled}>Turn off</button>
      <div className={styles.muted}>
        {share.precision === 'city' ? 'Towns and cities' : 'About a kilometre'}
        {' · '}{share.exact_dates ? 'exact dates' : 'day numbers'}
        {' · '}made {formatMoment(share.created_at)}
      </div>
    </li>
  )
}
