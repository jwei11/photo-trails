import { useState, type FormEvent } from 'react'
import { tripEdits, type Photo, type TimelineTrip } from './api.ts'
import DayList from './DayList.tsx'
import { countryDays, plural, tripDates, tripLabel, tripTitle } from './format.ts'
import PhotoGrid from './PhotoGrid.tsx'
import styles from './TripSection.module.css'
import { useEdit } from './useEdit.ts'

type Props = { trip: TimelineTrip; next?: TimelineTrip }

export default function TripSection({ trip, next }: Props) {
  // One edit at a time per trip: while one runs, every button in this trip is disabled.
  const edit = useEdit()
  const busy = edit.isPending
  const [renaming, setRenaming] = useState(false)

  // Dates go in the stats line whenever the title isn't already the dates.
  const dates = tripDates(trip)
  const stats = [
    tripTitle(trip) !== dates ? dates : null,
    countryDays(trip) || null,
    plural(trip.day_count, 'day'),
    plural(trip.photo_count, 'photo'),
    trip.distance_km ? `${Math.round(trip.distance_km).toLocaleString()} km` : null,
  ].filter(Boolean).join(' · ')

  function notATrip() {
    if (confirm("Take all these photos out of trips? They won't be regrouped automatically.")) {
      edit.mutate(() => tripEdits.dissolve(trip.id))
    }
  }

  // Splitting at the first photo would leave an empty trip, so it has no Split button.
  // "First" means the earliest instant, since that's what split compares. It's usually the
  // first photo of the first local day, but not after crossing the date line eastward,
  // where local dates go backwards (Tokyo Jul 2 morning, then Honolulu Jul 1 evening).
  const instant = (p: Photo) => Date.parse(p.shot_at!) // photos in days are always dated
  const firstId = trip.days
    .flatMap((day) => day.photos)
    .reduce<Photo | undefined>((first, p) => (!first || instant(p) < instant(first) ? p : first), undefined)?.id
  const photoActions = (p: Photo) => (
    <>
      {p.shot_at && p.id !== firstId && (
        <button className="small" disabled={busy} title="Start a new trip from this photo"
          onClick={() => edit.mutate(() => tripEdits.split(trip.id, p.shot_at!))}>
          Split here
        </button>
      )}
      <button className="small" disabled={busy} title="Take this photo out of the trip"
        onClick={() => edit.mutate(() => tripEdits.movePhoto(p.id, null))}>
        Remove
      </button>
    </>
  )

  return (
    <section className={styles.trip} aria-label={tripTitle(trip)}>
      <div className={styles.head}>
        <div>
          {renaming ? (
            <RenameForm
              initial={trip.name ?? ''}
              placeholder={tripDates(trip) ?? ''}
              busy={busy}
              onCancel={() => setRenaming(false)}
              onSave={(name) => edit.mutate(() => tripEdits.rename(trip.id, name), { onSuccess: () => setRenaming(false) })}
            />
          ) : (
            <h2>
              {tripTitle(trip)} {trip.is_locked && <span className={styles.badge}>edited</span>}
            </h2>
          )}
          <div className={styles.muted}>{stats}</div>
        </div>
        <div className={styles.buttons}>
          {!renaming && <button disabled={busy} onClick={() => setRenaming(true)}>Rename</button>}
          {next && (
            <button disabled={busy} title={`Merge "${tripLabel(next)}" into this trip`}
              onClick={() => edit.mutate(() => tripEdits.merge(trip.id, [next.id]))}>
              Merge with next
            </button>
          )}
          <button disabled={busy} onClick={notATrip}>Not a trip</button>
        </div>
      </div>
      {edit.error && <p role="alert" className={styles.error}>{edit.error.message}</p>}
      <DayList days={trip.days} actions={photoActions} />
      {trip.undated.length > 0 && <PhotoGrid title="No date" photos={trip.undated} actions={photoActions} />}
    </section>
  )
}

type RenameProps = {
  initial: string
  placeholder: string
  busy: boolean
  onSave: (name: string | null) => void
  onCancel: () => void
}

function RenameForm({ initial, placeholder, busy, onSave, onCancel }: RenameProps) {
  // A controlled input: React state holds the text, and the input shows whatever it says.
  const [name, setName] = useState(initial)

  function submit(e: FormEvent) {
    e.preventDefault() // stop the browser's own form submission (a full page reload)
    onSave(name.trim() || null) // empty means "use the dates" again
  }

  return (
    <form onSubmit={submit} className={styles.rename}>
      <input aria-label="Trip name" autoFocus maxLength={200} value={name} placeholder={placeholder}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => e.key === 'Escape' && onCancel()} />
      <button type="submit" className="primary" disabled={busy}>Save</button>
      <button type="button" onClick={onCancel} disabled={busy}>Cancel</button>
    </form>
  )
}
