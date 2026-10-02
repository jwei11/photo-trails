// Types mirror what the FastAPI endpoints return. Timestamps arrive as ISO strings.

export type Photo = {
  id: string
  original_filename: string | null
  content_type: string
  camera_make: string | null
  camera_model: string | null
  lat: number | null
  lon: number | null
  altitude_m: number | null // GPS altitude in metres, if the camera recorded it
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
  // From M7 (null without GPS): the place a person would name ("Lisbon"), the closest named
  // place, often a neighbourhood ("Baixa"), and the country containing the photo.
  place_name: string | null
  nearest_place_name: string | null
  region_name: string | null
  country_name: string | null
  country_code: string | null
  place_distance_m: number | null
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
  // In the order first visited. A country's days are the local days with a photo in it,
  // so a travel day counts for both countries.
  countries: { code: string; name: string | null; days: number }[]
  places: { name: string; country_code: string | null; photos: number }[]
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
  if (!res.ok) throw new ApiError(res.status, errorMessage(res.status, await res.text()))
  return (res.status === 204 ? null : await res.json()) as T
}

// FastAPI errors look like {"detail": "..."} (or {"detail": {"message": ...}} for some 409s,
// or a list for validation errors). Show the sentence, not the JSON.
function errorMessage(status: number, body: string) {
  try {
    const { detail } = JSON.parse(body)
    if (typeof detail === 'string') return detail
    if (typeof detail?.message === 'string') return detail.message
    if (Array.isArray(detail) && typeof detail[0]?.msg === 'string') return detail[0].msg
  } catch {
    // not JSON: fall through
  }
  return `${status} ${body}`.trim()
}

export function send<T = unknown>(method: string, path: string, body?: unknown) {
  return api<T>(path, {
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

export type ImportCounts = { total: number; uploading: number; done: number; duplicate: number; failed: number }

export type ImportItem = {
  id: string
  position: number
  filename: string | null
  content_type: string
  size_bytes: number
  declared_sha256: string
  // 'processing' is only ever set in the browser, between the upload and the server's answer.
  status: 'uploading' | 'processing' | 'done' | 'duplicate' | 'failed'
  // 'upload': the file must be sent again. 'processing': it's in storage; completing retries.
  failed_stage: 'upload' | 'processing' | null
  error: string | null
  photo_id: string | null
  // Present when the server just signed one: where to PUT the file.
  upload_url?: string
}

export type Import = { id: string; created_at: string; counts: ImportCounts; items: ImportItem[] }
export type ImportSummary = Omit<Import, 'items'>

export type FileDescription = { filename: string; content_type: string; size_bytes: number; sha256: string }

export const importsApi = {
  create: (files: FileDescription[]) => send<Import>('POST', '/imports', { files }),
  get: (importId: string) => api<Import>(`/imports/${importId}`),
  list: () => api<ImportSummary[]>('/imports'),
  renewUrl: (importId: string, itemId: string) =>
    send<ImportItem>('POST', `/imports/${importId}/items/${itemId}/upload-url`),
  complete: (importId: string, itemId: string) =>
    send<ImportItem>('POST', `/imports/${importId}/items/${itemId}/complete`),
  reportFailure: (importId: string, itemId: string, error: string) =>
    send<ImportItem>('POST', `/imports/${importId}/items/${itemId}/failed`, { error: error.slice(0, 500) }),
}

// A camera is identified by make + model + serial; clock offsets match all three exactly.
export type CameraId = { camera_make: string; camera_model: string; camera_serial: string | null }

export type Camera = CameraId & {
  photo_count: number
  // The camera's own clock readings (no timezone): the range a correction can cover.
  first_camera_time: string | null
  last_camera_time: string | null
}

export type ClockOffset = CameraId & {
  id: string
  offset_seconds: number // added to the camera's clock: positive if it was behind
  // Camera-clock times, half-open [from, to); null means unbounded on that side.
  valid_from: string | null
  valid_to: string | null
  note: string | null
}

export type NewClockOffset = Omit<ClockOffset, 'id'>

export type Home = { id: string; name: string; lat: number; lon: number; radius_km: number }

export const settingsApi = {
  cameras: () => api<Camera[]>('/cameras'),
  offsets: () => api<ClockOffset[]>('/camera-offsets'),
  // Without replace, an offset overlapping an existing one for the camera fails with 409.
  addOffset: (offset: NewClockOffset, replace = false) =>
    send<ClockOffset>('POST', '/camera-offsets', { ...offset, replace_overlapping: replace }),
  removeOffset: (id: string) => send('DELETE', `/camera-offsets/${id}`),
  homes: () => api<Home[]>('/homes'),
  addHome: (home: Omit<Home, 'id'>) => send<Home>('POST', '/homes', home),
  removeHome: (id: string) => send('DELETE', `/homes/${id}`),
}

export const photoUrl = {
  thumb: (id: string) => `/api/photos/${id}/thumb`,
  web: (id: string) => `/api/photos/${id}/web`,
  original: (id: string) => `/api/photos/${id}`,
}
