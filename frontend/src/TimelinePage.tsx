import { useQuery } from '@tanstack/react-query'
import { api, photoUrl, type Day, type Photo, type Timeline } from './api.ts'
import styles from './TimelinePage.module.css'

export default function TimelinePage() {
  // The key names this data in the shared cache; after an edit, invalidating
  // ['timeline'] makes every component showing it refetch.
  const { data, isPending, error } = useQuery({
    queryKey: ['timeline'],
    queryFn: () => api<Timeline>('/timeline'),
  })

  if (isPending) return <p className={styles.muted}>Loading…</p>
  if (error) return <p className={styles.error}>Couldn't load photos: {error.message}</p>

  return (
    <>
      {data.trips.map((trip) => {
        const first = trip.days[0]?.date
        const last = trip.days.at(-1)?.date
        return (
          <section key={trip.id} className={styles.section}>
            <h2>{trip.name ?? (first ? dateRange(first, last!) : 'Trip')}</h2>
            <p className={styles.muted}>
              {trip.photo_count} photos · {trip.day_count} days · {Math.round(trip.distance_km)} km
            </p>
            <DayList days={trip.days} />
            {trip.undated.length > 0 && <PhotoGrid title="No date" photos={trip.undated} />}
          </section>
        )
      })}
      {data.not_in_trip.length > 0 && (
        <section className={styles.section}>
          <h2>Not in a trip</h2>
          <DayList days={data.not_in_trip} />
        </section>
      )}
      {data.undated.length > 0 && (
        <section className={styles.section}>
          <h2>Undated</h2>
          <PhotoGrid photos={data.undated} />
        </section>
      )}
    </>
  )
}

function DayList({ days }: { days: Day[] }) {
  return days.map((day) => <PhotoGrid key={day.date} title={formatDay(day.date)} photos={day.photos} />)
}

function PhotoGrid({ title, photos }: { title?: string; photos: Photo[] }) {
  return (
    <div>
      {title && <h3 className={styles.day}>{title}</h3>}
      <div className={styles.grid}>
        {photos.map((p) => (
          <PhotoCard key={p.id} photo={p} />
        ))}
      </div>
    </div>
  )
}

function PhotoCard({ photo }: { photo: Photo }) {
  const href = photo.web_key ? photoUrl.web(photo.id) : photoUrl.original(photo.id)
  return (
    <figure className={styles.card}>
      <a href={href} target="_blank" rel="noreferrer">
        {photo.thumb_key ? (
          <img src={photoUrl.thumb(photo.id)} loading="lazy" alt={photo.original_filename ?? ''} />
        ) : (
          <div className={styles.noimg}>{photo.processing_error ? "Can't preview this file" : 'No preview'}</div>
        )}
      </a>
      {photo.local_time && <figcaption>{photo.local_time.slice(11, 16)}</figcaption>}
    </figure>
  )
}

// Dates are local calendar days ("2025-07-01"). Formatting them in UTC stops the
// browser's own timezone from shifting them to the day before.
const dayFormat = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })
const rangeFormat = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' })

function formatDay(date: string) {
  return dayFormat.format(new Date(date))
}

function dateRange(first: string, last: string) {
  return first === last ? rangeFormat.format(new Date(first)) : rangeFormat.formatRange(new Date(first), new Date(last))
}
