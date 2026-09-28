import { useQuery } from '@tanstack/react-query'
import { api, tripEdits, type Photo, type Timeline, type TimelineTrip } from './api.ts'
import DayList from './DayList.tsx'
import { plural, tripTitle } from './format.ts'
import PhotoGrid from './PhotoGrid.tsx'
import styles from './TimelinePage.module.css'
import TripSection from './TripSection.tsx'
import { useEdit } from './useEdit.ts'

export default function TimelinePage() {
  // The key names this data in the shared cache; useEdit invalidates ['timeline'] after
  // every edit, which refetches it here.
  const { data, isPending, error } = useQuery({
    queryKey: ['timeline'],
    queryFn: () => api<Timeline>('/timeline'),
  })
  // Edits on photos outside any trip ("Add to trip…").
  const edit = useEdit()

  if (isPending) return <p className={styles.muted}>Loading…</p>
  if (error) return <p className={styles.error}>Couldn't load photos: {error.message}</p>

  const { trips } = data
  const loose = data.not_in_trip.reduce((n, day) => n + day.photos.length, 0)
  const inTrips = trips.reduce((n, trip) => n + trip.photo_count, 0)
  const total = inTrips + loose + data.undated.length
  const addToTrip = (p: Photo) => (
    <AddToTrip trips={trips} disabled={edit.isPending}
      onPick={(tripId) => edit.mutate(() => tripEdits.movePhoto(p.id, tripId))} />
  )

  return (
    <>
      <p className={styles.muted}>
        {total ? `${plural(total, 'photo')} · ${plural(trips.length, 'trip')}` : 'No photos yet.'}
      </p>
      {edit.error && <p role="alert" className={styles.error}>{edit.error.message}</p>}
      {trips.map((trip, i) => (
        <TripSection key={trip.id} trip={trip} next={trips[i + 1]} />
      ))}
      {loose > 0 && (
        <section className={styles.loose}>
          <h2>
            Not in a trip{' '}
            <span className={styles.muted}>· {plural(loose, 'photo')} at home, in small groups, or removed</span>
          </h2>
          <DayList days={data.not_in_trip} actions={addToTrip} />
        </section>
      )}
      {data.undated.length > 0 && (
        <section className={styles.loose}>
          <h2>
            Undated <span className={styles.muted}>· {plural(data.undated.length, 'photo')} with no time information</span>
          </h2>
          <PhotoGrid photos={data.undated} actions={addToTrip} />
        </section>
      )}
    </>
  )
}

type AddToTripProps = { trips: TimelineTrip[]; disabled: boolean; onPick: (tripId: string) => void }

function AddToTrip({ trips, disabled, onPick }: AddToTripProps) {
  if (!trips.length) return null
  // value="" keeps the select showing its prompt; picking a trip fires the edit, and the
  // photo then disappears from this section when the timeline refetches.
  return (
    <select className="small" aria-label="Add to trip" value="" disabled={disabled}
      onChange={(e) => onPick(e.target.value)}>
      <option value="">Add to trip…</option>
      {trips.map((t) => (
        <option key={t.id} value={t.id}>{tripTitle(t)}</option>
      ))}
    </select>
  )
}
