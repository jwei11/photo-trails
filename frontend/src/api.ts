import { queryClient } from './queryClient.ts'

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
  // The session ended (expired, or signed out in another tab): recheck who's signed in,
  // which shows the login page.
  if (res.status === 401) void queryClient.invalidateQueries({ queryKey: ['me'] })
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
  homeSuggestions: () => api<HomeSuggestion[]>('/homes/suggestions'),
}

// A place that looks like home: photos on many days, over weeks. Only the owner sees these.
export type HomeSuggestion = {
  lat: number
  lon: number
  place_name: string | null
  day_count: number
  first_day: string
  last_day: string
}

// ---- Sharing

export type Precision = 'city' | 'neighbourhood'

export type Share = {
  id: string
  token: string
  path: string // "/s/<token>": the link, on this site
  precision: Precision
  exact_dates: boolean
  created_at: string
  revoked_at: string | null
}

export type Sharing = {
  shares: Share[]
  can_share: boolean // false until a week after the trip's last photo
  has_homes: boolean
  hidden_near_home: number // photos a share leaves out because they're near a home
  hidden_recent: number // ...or from the last week
  shareable_from: string | null // when the trip can first be shared
}

export const sharesApi = {
  forTrip: (tripId: string) => api<Sharing>(`/trips/${tripId}/shares`),
  create: (tripId: string, precision: Precision, exactDates: boolean) =>
    send<Share>('POST', `/trips/${tripId}/shares`, { precision, exact_dates: exactDates }),
  revoke: (shareId: string) => send('DELETE', `/shares/${shareId}`),
}

// What anyone with a share link sees: snapped positions, day numbers, no filenames.
export type SharedPhoto = {
  id: string
  lat: number | null
  lon: number | null
  place_name: string | null
  country_name: string | null
  has_preview: boolean
  day: number | null // 1 is the trip's first day
  time: string | null // "14:30", local time
  local_time?: string | null // only if the owner chose exact dates
}

export type SharedTrip = {
  name: string | null
  precision: Precision
  exact_dates: boolean
  period: string | null // "July 2025"
  start: string | null // only with exact dates
  end: string | null
  day_count: number
  photo_count: number
  distance_km: number
  countries: { code: string; name: string | null; days: number }[]
  places: { name: string; country_code: string | null; photos: number }[]
  photos: SharedPhoto[]
}

export const sharedApi = {
  trip: (token: string) => api<SharedTrip>(`/shared/${encodeURIComponent(token)}`),
  thumb: (token: string, id: string) => `/api/shared/${encodeURIComponent(token)}/photos/${id}/thumb`,
  web: (token: string, id: string) => `/api/shared/${encodeURIComponent(token)}/photos/${id}/web`,
}

export const photoUrl = {
  thumb: (id: string) => `/api/photos/${id}/thumb`,
  web: (id: string) => `/api/photos/${id}/web`,
  original: (id: string) => `/api/photos/${id}`,
}
