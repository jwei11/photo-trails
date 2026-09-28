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
  time_source: string | null
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

export type Timeline = {
  trips: (Trip & { days: Day[]; undated: Photo[] })[]
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

export const photoUrl = {
  thumb: (id: string) => `/api/photos/${id}/thumb`,
  web: (id: string) => `/api/photos/${id}/web`,
  original: (id: string) => `/api/photos/${id}`,
}
