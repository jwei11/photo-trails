import { photoUrl, type Photo, type TimelineTrip } from '../api.ts'
import type { MapMarker } from './TripMap.tsx'

// The trip map's data, worked out from the timeline without any map code, so it can be
// tested on its own.

export type Located = Photo & { lat: number; lon: number }
export type Stop = Located & { shot_at: string }

const located = (p: Photo): p is Located => p.lat != null && p.lon != null

/** Every photo in the trip, dated ones in time order, then undated ones. */
export function tripPhotos(trip: TimelineTrip): Photo[] {
  return [...trip.days.flatMap((d) => d.photos), ...trip.undated]
}

/**
 * The route: dated photos with a location, in time order. Undated photos have a place but
 * no position in time, so they're shown on the map but not on the route or the scrubber.
 */
export function route(trip: TimelineTrip): Stop[] {
  return tripPhotos(trip)
    .filter((p): p is Stop => located(p) && p.shot_at != null)
    .sort((a, b) => a.shot_at.localeCompare(b.shot_at))
}

/** Photos to show as markers: everything with a location, dated or not. */
export function markers(trip: TimelineTrip): Located[] {
  return tripPhotos(trip).filter(located)
}

/** A photo as the owner's map shows it. */
export function photoMarker(p: Located): MapMarker {
  return {
    id: p.id,
    lat: p.lat,
    lon: p.lon,
    thumb: p.thumb_key ? photoUrl.thumb(p.id) : null,
    web: p.web_key ? photoUrl.web(p.id) : null,
    title: p.original_filename ?? '',
    lines: [
      p.local_time ? `${p.local_time.slice(0, 10)} ${p.local_time.slice(11, 16)}` : 'No date',
      [p.place_name, p.country_name].filter(Boolean).join(', '),
      p.original_filename ?? '',
    ],
  }
}

/** Kilometres along the route up to and including stop `upTo`, straight lines between stops. */
export function distanceKm(stops: Stop[], upTo = stops.length - 1) {
  let km = 0
  for (let i = 1; i <= upTo && i < stops.length; i++) km += haversineKm(stops[i - 1], stops[i])
  return km
}

export function haversineKm(a: { lat: number; lon: number }, b: { lat: number; lon: number }) {
  const rad = Math.PI / 180
  const dLat = (b.lat - a.lat) * rad
  const dLon = (b.lon - a.lon) * rad
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2
  return 2 * 6371 * Math.asin(Math.sqrt(h))
}

/**
 * Elevation from the photos' GPS altitude, or null if fewer than two have one. "Climb" adds
 * up every rise between consecutive photos, so it's a lower bound: the climbing between
 * photos isn't seen. Rises under 5 m are ignored, since phone GPS altitude wobbles by
 * more than that standing still.
 */
export function elevation(stops: Stop[]) {
  const heights = stops.map((s) => s.altitude_m).filter((h): h is number => h != null)
  if (heights.length < 2) return null
  let climb = 0
  for (let i = 1; i < heights.length; i++) {
    const rise = heights[i] - heights[i - 1]
    if (rise >= 5) climb += rise
  }
  return { min: Math.min(...heights), max: Math.max(...heights), climb }
}

/** "2 days 5 h", "5 h 20 min", "12 min": the time from the first photo to the last. */
export function duration(stops: Stop[]) {
  if (stops.length < 2) return null
  const minutes = Math.round((Date.parse(stops.at(-1)!.shot_at) - Date.parse(stops[0].shot_at)) / 60000)
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor((minutes % 1440) / 60)
  const mins = minutes % 60
  if (days) return `${days} ${days === 1 ? 'day' : 'days'}${hours ? ` ${hours} h` : ''}`
  if (hours) return `${hours} h${mins ? ` ${mins} min` : ''}`
  return `${mins} min`
}
