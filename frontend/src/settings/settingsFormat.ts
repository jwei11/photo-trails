import type { CameraId, ClockOffset } from '../api.ts'

export function sameCamera(a: CameraId, b: CameraId) {
  return a.camera_make === b.camera_make && a.camera_model === b.camera_model && a.camera_serial === b.camera_serial
}

export function cameraKey(c: CameraId) {
  return `${c.camera_make}|${c.camera_model}|${c.camera_serial ?? ''}`
}

export function cameraName(c: CameraId) {
  return `${c.camera_make} ${c.camera_model}${c.camera_serial ? ` (#${c.camera_serial})` : ''}`
}

// Camera-clock times have no timezone: show them exactly as the camera recorded them.
export function clockTime(t: string) {
  return t.slice(0, 16).replace('T', ' ')
}

export function formatShift(seconds: number) {
  const abs = Math.abs(seconds)
  const h = Math.floor(abs / 3600)
  const m = Math.floor((abs % 3600) / 60)
  return [h && `${h}h`, m && `${m}m`].filter(Boolean).join(' ') || `${abs}s`
}

export function coverage(o: ClockOffset) {
  if (o.valid_from && o.valid_to) return `from ${clockTime(o.valid_from)} to ${clockTime(o.valid_to)}`
  if (o.valid_from) return `from ${clockTime(o.valid_from)} on`
  if (o.valid_to) return `until ${clockTime(o.valid_to)}`
  return 'all photos'
}

/** "37.7749, -122.4194" → { lat, lon }, or null if it isn't a valid point. */
export function parseCoordinates(text: string) {
  const parts = text.split(',').map((s) => s.trim())
  if (parts.length !== 2 || parts.some((p) => p === '')) return null
  const [lat, lon] = parts.map(Number)
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null
  return { lat, lon }
}
