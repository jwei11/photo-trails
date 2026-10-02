import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { vi } from 'vitest'
import type { Photo, Timeline, TimelineTrip } from '../api.ts'

export type Call = { method: string; path: string; body: unknown }
export type Reply = { status: number; body?: unknown }

/**
 * Replaces fetch with a fake server: every request is recorded in `calls` and answered
 * by `reply`. GETs listed in `data` (by path) answer 200 with that body; anything else
 * goes to `reply` (204 No Content by default).
 */
export function stubApi(data: Record<string, unknown>, reply: (call: Call) => Reply = () => ({ status: 204 })) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, options: RequestInit = {}) => {
    const call = {
      method: options.method ?? 'GET',
      path: url,
      body: options.body ? JSON.parse(options.body as string) : undefined,
    }
    calls.push(call)
    const { status, body } = call.method === 'GET' && url in data ? { status: 200, body: data[url] } : reply(call)
    return new Response(status === 204 ? null : JSON.stringify(body), { status })
  }))
  return {
    calls,
    edits: () => calls.filter((c) => c.method !== 'GET'),
    loads: (path: string) => calls.filter((c) => c.method === 'GET' && c.path === path).length,
  }
}

/** A fake server for the timeline page. */
export function fakeApi(timeline: Timeline, reply?: (call: Call) => Reply) {
  const api = stubApi({ '/api/timeline': timeline }, reply)
  return { ...api, timelineLoads: () => api.loads('/api/timeline') }
}

/** Renders a component with its own fresh cache, as the app would. */
export function renderWithApp(ui: ReactNode) {
  // No retries: a failed request should fail the test at once, not after back-offs.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

export function photo(id: string, overrides: Partial<Photo> = {}): Photo {
  return {
    id,
    original_filename: `${id}.jpg`,
    content_type: 'image/jpeg',
    camera_make: 'Apple',
    camera_model: 'iPhone 15',
    lat: 38.72,
    lon: -9.14,
    shot_at: '2025-07-01T08:30:00+00:00',
    local_time: '2025-07-01T09:30:00',
    tz_name: 'Europe/Lisbon',
    local_offset_min: 60,
    time_source: 'exif_offset',
    thumb_key: `${id}_thumb.webp`,
    web_key: `${id}_web.webp`,
    processing_error: null,
    trip_id: null,
    place_name: 'Lisbon',
    nearest_place_name: 'Baixa',
    region_name: 'Lisbon',
    country_name: 'Portugal',
    country_code: 'PT',
    place_distance_m: 337,
    ...overrides,
  }
}

export function trip(id: string, photos: Photo[], overrides: Partial<TimelineTrip> = {}): TimelineTrip {
  const days = new Map<string, Photo[]>()
  for (const p of photos) {
    const date = p.local_time!.slice(0, 10)
    days.set(date, [...(days.get(date) ?? []), { ...p, trip_id: id }])
  }
  return {
    id,
    name: null,
    is_locked: false,
    start: photos[0].shot_at,
    end: photos.at(-1)!.shot_at,
    day_count: days.size,
    photo_count: photos.length,
    distance_km: 12.3,
    bbox: null,
    days: [...days].map(([date, ps]) => ({ date, photos: ps })),
    undated: [],
    countries: [],
    places: [],
    ...overrides,
  }
}
