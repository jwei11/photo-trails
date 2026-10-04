import { useQuery } from '@tanstack/react-query'
import { lazy, Suspense, useMemo } from 'react'
import { useParams } from 'react-router'
import { ApiError, sharedApi, type SharedPhoto, type SharedTrip } from '../api.ts'
import { countryDays, listNames, placesTitle, plural } from '../format.ts'
import type { MapMarker, MapPoint } from '../map/TripMap.tsx'
import mapStyles from '../map/TripMapPage.module.css'
import styles from './SharedTripPage.module.css'

const TripMap = lazy(() => import('../map/TripMap.tsx'))

/** A trip someone shared: read-only, for anyone with the link, signed in or not. */
export default function SharedTripPage() {
  const { token = '' } = useParams()
  const { data, isPending, error } = useQuery({
    queryKey: ['shared', token],
    queryFn: () => sharedApi.trip(token),
    retry: false,
  })
  if (isPending) return <p className={mapStyles.muted}>Loading…</p>
  if (error) {
    // Unknown and revoked links look the same on purpose.
    return error instanceof ApiError && error.status === 404
      ? <p>This link doesn't work. It may have been turned off by the person who shared it.</p>
      : <p role="alert">Couldn't load the trip: {error.message}</p>
  }
  return <SharedTripView token={token} trip={data} />
}

type Located = SharedPhoto & { lat: number; lon: number }
const located = (p: SharedPhoto): p is Located => p.lat != null && p.lon != null

function when(p: SharedPhoto) {
  if (p.local_time) return `${p.local_time.slice(0, 10)} ${p.local_time.slice(11, 16)}`
  return p.day ? `Day ${p.day}, ${p.time}` : 'No date'
}

function SharedTripView({ token, trip }: { token: string; trip: SharedTrip }) {
  const markers: (MapMarker & { day: number | null })[] = useMemo(
    () => trip.photos.filter(located).map((p) => ({
      day: p.day,
      id: p.id,
      lat: p.lat,
      lon: p.lon,
      thumb: p.has_preview ? sharedApi.thumb(token, p.id) : null,
      web: p.has_preview ? sharedApi.web(token, p.id) : null,
      title: when(p),
      lines: [when(p), [p.place_name, p.country_name].filter(Boolean).join(', ')],
    })),
    [trip, token],
  )
  // The route: dated, located photos, already in time order.
  const stops: MapPoint[] = useMemo(() => markers.filter((m) => m.day != null), [markers])
  const days = useMemo(() => {
    const byDay = new Map<number | null, SharedPhoto[]>()
    for (const p of trip.photos) byDay.set(p.day, [...(byDay.get(p.day) ?? []), p])
    return [...byDay.entries()]
  }, [trip])

  const dates = trip.exact_dates && trip.start
    ? (trip.start === trip.end ? trip.start : `${trip.start} to ${trip.end}`)
    : trip.period
  const stats: [string, string][] = [
    ['When', dates ?? 'No dates'],
    ['Days', String(trip.day_count)],
    ['Distance', `${trip.distance_km} km`],
    ['Photos', String(trip.photo_count)],
  ]
  if (trip.places.length) stats.push(['Places', listNames(trip.places.map((p) => p.name), 6)])
  if (trip.countries.length) stats.push(['Countries', countryDays(trip)])

  return (
    <div>
      <h2 className={mapStyles.title}>{trip.name ?? placesTitle(trip) ?? trip.period ?? 'A trip'}</h2>
      <dl className={mapStyles.stats}>
        {stats.map(([label, value]) => (
          <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
        ))}
      </dl>

      {markers.length > 0 && (
        <Suspense fallback={<div className={mapStyles.mapPlaceholder}>Loading map…</div>}>
          <TripMap stops={stops} markers={markers} current={stops.length - 1} onSelect={() => {}} />
        </Suspense>
      )}

      {days.map(([day, photos]) => (
        <section key={day ?? 'undated'} aria-label={day ? `Day ${day}` : 'No date'}>
          <h3 className={styles.day}>{day ? `Day ${day}` : 'No date'}</h3>
          <div className={styles.grid}>
            {photos.map((p) => (
              <figure key={p.id} className={styles.photo}>
                {p.has_preview
                  ? <a href={sharedApi.web(token, p.id)} target="_blank" rel="noreferrer"><img src={sharedApi.thumb(token, p.id)} alt="" loading="lazy" /></a>
                  : <div className={styles.noPreview}>No preview</div>}
                <figcaption>
                  {p.local_time ? p.local_time.slice(11, 16) : p.time}
                  {p.place_name && ` · ${p.place_name}`}
                </figcaption>
              </figure>
            ))}
          </div>
        </section>
      ))}

      <p className={`${mapStyles.muted} ${styles.note}`}>
        {plural(trip.photo_count, 'photo')}, shared with Trip Mapper. Locations are shown{' '}
        {trip.precision === 'city' ? 'to the nearest town or city' : 'to about a kilometre'}
        {trip.exact_dates ? '.' : ', and days are numbered instead of dated.'}
      </p>
    </div>
  )
}
