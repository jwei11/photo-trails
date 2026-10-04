import { useQuery } from '@tanstack/react-query'
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router'
import { api, photoUrl, type Timeline } from '../api.ts'
import { countryDays, listNames, plural, tripDates, tripTitle } from '../format.ts'
import { distanceKm, duration, elevation, markers as locatedPhotos, photoMarker, route, tripPhotos } from './route.ts'
import SharePanel from './SharePanel.tsx'
import styles from './TripMapPage.module.css'

// Leaflet is only needed on this page: loading it separately keeps the timeline fast.
const TripMap = lazy(() => import('./TripMap.tsx'))

export const PLAY_STEP_MS = 700

export default function TripMapPage() {
  const { tripId } = useParams()
  const { data, isPending, error } = useQuery({ queryKey: ['timeline'], queryFn: () => api<Timeline>('/timeline') })
  const trip = data?.trips.find((t) => t.id === tripId)

  if (isPending) return <p className={styles.muted}>Loading…</p>
  if (error) return <p role="alert">Couldn't load the trip: {error.message}</p>
  if (!trip) return <p>That trip doesn't exist any more. <Link to="/">Back to the timeline</Link></p>
  return <TripView key={trip.id} trip={trip} />
}

function TripView({ trip }: { trip: Timeline['trips'][number] }) {
  const stops = useMemo(() => route(trip), [trip])
  const markers = useMemo(() => locatedPhotos(trip), [trip])
  const mapMarkers = useMemo(() => markers.map(photoMarker), [markers])
  const unlocated = tripPhotos(trip).length - markers.length
  // Start at the end, showing the whole trip; Play replays it from the start.
  const [current, setCurrent] = useState(Math.max(0, stops.length - 1))
  const [playing, setPlaying] = useState(false)
  const [sharing, setSharing] = useState(false)
  const last = stops.length - 1
  const here = stops[Math.min(current, last)]

  useEffect(() => {
    if (!playing) return
    const timer = setInterval(() => {
      setCurrent((i) => {
        if (i >= last) {
          setPlaying(false)
          return i
        }
        return i + 1
      })
    }, PLAY_STEP_MS)
    return () => clearInterval(timer)
  }, [playing, last])

  const play = () => {
    if (current >= last) setCurrent(0)
    setPlaying(!playing)
  }
  // A clicked marker moves the scrubber to that photo (if it's on the route).
  const select = useCallback(
    (photoId: string) => {
      const i = stops.findIndex((s) => s.id === photoId)
      if (i >= 0) {
        setPlaying(false)
        setCurrent(i)
      }
    },
    [stops],
  )

  const elev = elevation(stops)
  const stats: [string, string][] = [
    ['When', tripDates(trip) ?? 'No dates'],
    ['Duration', duration(stops) ?? '–'],
    ['Distance', `${Math.round(distanceKm(stops))} km`],
    ['Photos', String(trip.photo_count)],
  ]
  if (trip.places.length) stats.push(['Places', listNames(trip.places.map((p) => p.name), 6)])
  if (trip.countries.length) stats.push(['Countries', countryDays(trip)])
  if (elev) stats.push(['Elevation', `${Math.round(elev.min)}–${Math.round(elev.max)} m, ${Math.round(elev.climb)} m climbed`])

  return (
    <div>
      <p><Link to="/">← Timeline</Link></p>
      <div className={styles.heading}>
        <h2 className={styles.title}>{tripTitle(trip)}</h2>
        <button onClick={() => setSharing(!sharing)} aria-expanded={sharing}>Share…</button>
      </div>
      {sharing && <SharePanel tripId={trip.id} />}
      <dl className={styles.stats}>
        {stats.map(([label, value]) => (
          <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
        ))}
      </dl>

      {markers.length === 0 ? (
        <p className={styles.muted}>None of this trip's photos have a location, so there's nothing to map.</p>
      ) : (
        <Suspense fallback={<div className={styles.mapPlaceholder}>Loading map…</div>}>
          <TripMap stops={stops} markers={mapMarkers} current={Math.min(current, last)} onSelect={select} />
        </Suspense>
      )}

      {stops.length > 1 && here && (
        <div className={styles.scrubber}>
          <button onClick={play} aria-label={playing ? 'Pause' : 'Play the trip'}>{playing ? 'Pause' : '▶ Play'}</button>
          <input
            type="range" min={0} max={last} value={Math.min(current, last)}
            aria-label="Trip timeline"
            aria-valuetext={`Photo ${current + 1} of ${stops.length}`}
            onChange={(e) => {
              setPlaying(false)
              setCurrent(Number(e.target.value))
            }}
          />
          <div className={styles.now} aria-live="polite">
            {here.thumb_key && <img src={photoUrl.thumb(here.id)} alt="" />}
            <div>
              <strong>{here.local_time ? `${here.local_time.slice(0, 10)} ${here.local_time.slice(11, 16)}` : ''}</strong>
              <div>{[here.place_name, here.country_name].filter(Boolean).join(', ')}</div>
              <div className={styles.muted}>
                Photo {current + 1} of {stops.length} · {Math.round(distanceKm(stops, current))} km so far
              </div>
            </div>
          </div>
        </div>
      )}

      {unlocated > 0 && (
        <p className={styles.muted}>
          {plural(unlocated, 'photo')} in this trip {unlocated === 1 ? 'has' : 'have'} no location and{' '}
          {unlocated === 1 ? "isn't" : "aren't"} on the map.
        </p>
      )}
    </div>
  )
}
