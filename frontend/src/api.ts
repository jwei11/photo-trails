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

export const photoUrl = {
  thumb: (id: string) => `/api/photos/${id}/thumb`,
  web: (id: string) => `/api/photos/${id}/web`,
  original: (id: string) => `/api/photos/${id}`,
}
