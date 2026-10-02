import { describe, expect, it } from 'vitest'
import { photo, trip } from '../test/fakeApi.tsx'
import { distanceKm, duration, elevation, markers, route } from './route.ts'

const at = (id: string, time: string, lat: number | null, lon: number | null, altitude_m: number | null = null) =>
  photo(id, { shot_at: time, local_time: time.slice(0, 19), lat, lon, altitude_m })

describe('trip route', () => {
  const lisbon = at('a', '2025-07-01T09:00:00Z', 38.72, -9.14, 10)
  const noGps = at('b', '2025-07-01T10:00:00Z', null, null)
  const madrid = at('c', '2025-07-02T15:30:00Z', 40.42, -3.70, 650)
  const sintra = at('d', '2025-07-01T12:00:00Z', 38.80, -9.39, 200)
  const undated = photo('e', { shot_at: null, lat: 38.7, lon: -9.1 })
  const t = { ...trip('t', [lisbon, noGps, madrid, sintra]), undated: [undated] }

  it('runs through dated, located photos in time order', () => {
    expect(route(t).map((s) => s.id)).toEqual(['a', 'd', 'c'])
  })

  it('shows undated photos as markers, but not photos without a location', () => {
    expect(markers(t).map((p) => p.id).sort()).toEqual(['a', 'c', 'd', 'e'])
  })

  it('measures distance along the route, or up to a point on it', () => {
    const stops = route(t)
    expect(distanceKm(stops, 0)).toBe(0)
    expect(distanceKm(stops, 1)).toBeCloseTo(23.3, 0) // Lisbon -> Sintra
    expect(distanceKm(stops)).toBeCloseTo(23.3 + 520, -1) // then Sintra -> Madrid, ~520 km
  })

  it('reports the elevation range and climb, ignoring GPS wobble', () => {
    expect(elevation(route(t))).toEqual({ min: 10, max: 650, climb: 640 })
    const wobble = [at('x', '2025-07-01T09:00:00Z', 1, 1, 100), at('y', '2025-07-01T09:10:00Z', 1, 1, 103)]
    expect(elevation(route({ ...trip('w', wobble), undated: [] }))?.climb).toBe(0)
    expect(elevation(route({ ...trip('n', [noGps, lisbon]), undated: [] }))).toBeNull()
  })

  it('describes how long the trip took', () => {
    expect(duration(route(t))).toBe('1 day 6 h')
    const short = [at('x', '2025-07-01T09:00:00Z', 1, 1), at('y', '2025-07-01T14:20:00Z', 1, 1)]
    expect(duration(route({ ...trip('s', short), undated: [] }))).toBe('5 h 20 min')
    expect(duration([])).toBeNull()
  })
})
