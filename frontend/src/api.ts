// Types mirror what the FastAPI endpoints return. Timestamps arrive as ISO strings.

export type Photo = {
  id: string
  original_filename: string | null
  content_type: string
  camera_make: string | null
  camera_model: string | null
  lat: number | null
  lon: number | null
  shot_at: string | null
  // Wall-clock time where the photo was taken; only set on dated photos in the timeline.
  local_time?: string
  tz_name: string | null
  local_offset_min: number | null
  time_source: 'exif_offset' | 'gps' | 'coordinates' | 'neighbour' | 'default_zone' | null
  thumb_key: string | null
  web_key: string | null
  processing_error: string | null
  trip_id: string | null
}

export type Day = { date: string; photos: Photo[] }

export type Trip = {
  id: string
  name: string | null
  is_locked: boolean
  start: string | null
  end: string | null
  day_count: number
  photo_count: number
  distance_km: number
  bbox: [number, number, number, number] | null // min_lat, min_lon, max_lat, max_lon
}

export type TimelineTrip = Trip & { days: Day[]; undated: Photo[] }

export type Timeline = {
  trips: TimelineTrip[]
  not_in_trip: Day[]
  undated: Photo[]
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// Every API call goes through here, so paths are written without the /api prefix.
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, options)
  if (!res.ok) throw new ApiError(res.status, `${res.status} ${await res.text()}`)
  return (res.status === 204 ? null : await res.json()) as T
}

function send(method: string, path: string, body?: unknown) {
  return api<unknown>(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

// Trip edits. Each one locks the trips it touches, so the grouping algorithm leaves them alone.
export const tripEdits = {
  // null goes back to showing the trip's dates.
  rename: (tripId: string, name: string | null) => send('PATCH', `/trips/${tripId}`, { name }),
  merge: (tripId: string, otherIds: string[]) => send('POST', `/trips/${tripId}/merge`, { trip_ids: otherIds }),
  // Photos taken at or after `at` (an instant, e.g. a photo's shot_at) become a new trip.
  split: (tripId: string, at: string) => send('POST', `/trips/${tripId}/split`, { at }),
  // "Not a trip": its photos leave trips and aren't regrouped automatically.
  dissolve: (tripId: string) => send('DELETE', `/trips/${tripId}`),
  // null takes the photo out of its trip.
  movePhoto: (photoId: string, tripId: string | null) => send('PUT', `/photos/${photoId}/trip`, { trip_id: tripId }),
}

export const photoUrl = {
  thumb: (id: string) => `/api/photos/${id}/thumb`,
  web: (id: string) => `/api/photos/${id}/web`,
  original: (id: string) => `/api/photos/${id}`,
}
