import { act, fireEvent, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fakeApi, photo, renderWithApp, trip } from '../test/fakeApi.tsx'
import type { Located, Stop } from './route.ts'
import TripMapPage, { PLAY_STEP_MS } from './TripMapPage.tsx'

// jsdom can't draw a Leaflet map, so the map is a stand-in that lists what it was given and
// lets a test "click" a marker. The map itself was checked in a real browser.
vi.mock('./TripMap.tsx', () => ({
  default: ({ stops, markers, current, onSelect }: {
    stops: Stop[]; markers: Located[]; current: number; onSelect: (id: string) => void
  }) => (
    <div data-testid="map" data-current={stops[current]?.id}>
      {markers.map((m) => <button key={m.id} onClick={() => onSelect(m.id)}>marker {m.id}</button>)}
    </div>
  ),
}))

afterEach(() => vi.useRealTimers())

const photos = [
  photo('a', { shot_at: '2025-07-01T08:00:00Z', local_time: '2025-07-01T09:00:00', lat: 38.72, lon: -9.14, place_name: 'Lisbon' }),
  photo('b', { shot_at: '2025-07-01T11:00:00Z', local_time: '2025-07-01T12:00:00', lat: 38.80, lon: -9.39, place_name: 'Sintra' }),
  photo('c', { shot_at: '2025-07-02T14:00:00Z', local_time: '2025-07-02T16:00:00', lat: 40.42, lon: -3.70, place_name: 'Madrid', country_name: 'Spain' }),
  photo('nogps', { shot_at: '2025-07-01T10:00:00Z', local_time: '2025-07-01T11:00:00', lat: null, lon: null }),
]

function openTrip(id = 'iberia', ps = photos) {
  fakeApi({
    trips: [trip('iberia', ps, { name: 'Iberia', photo_count: ps.length, places: [{ name: 'Lisbon', country_code: 'PT', photos: 1 }] })],
    not_in_trip: [],
    undated: [],
  })
  renderWithApp(<Routes><Route path="/trips/:tripId" element={<TripMapPage />} /></Routes>, `/trips/${id}`)
}

const now = () => screen.getByText(/Photo \d of \d/).textContent

describe('trip map page', () => {
  it('shows the trip stats, and the scrubber at the end of the route', async () => {
    openTrip()

    expect(await screen.findByRole('heading', { name: 'Iberia' })).toBeInTheDocument()
    const stats = screen.getAllByRole('definition').map((d) => d.textContent)
    expect(stats).toContain('1 day 6 h')
    expect(stats).toContain('543 km')
    expect(await screen.findByTestId('map')).toHaveAttribute('data-current', 'c')
    expect(now()).toBe('Photo 3 of 3 · 543 km so far')
    expect(screen.getByText("1 photo in this trip has no location and isn't on the map.")).toBeInTheDocument()
  })

  it('moves through the trip with the scrubber', async () => {
    openTrip()
    const scrubber = await screen.findByRole('slider', { name: 'Trip timeline' })

    fireEvent.change(scrubber, { target: { value: '0' } }) // jsdom can't drag or key a slider

    expect(now()).toBe('Photo 1 of 3 · 0 km so far')
    expect(screen.getByTestId('map')).toHaveAttribute('data-current', 'a')
  })

  it('jumps the scrubber to a photo clicked on the map', async () => {
    openTrip()
    const map = await screen.findByTestId('map')

    await userEvent.click(within(map).getByRole('button', { name: 'marker b' }))

    expect(now()).toBe('Photo 2 of 3 · 23 km so far')
  })

  it('plays the trip from the start, one photo per step, and stops at the end', async () => {
    openTrip()
    await screen.findByTestId('map')
    vi.useFakeTimers()

    act(() => { screen.getByRole('button', { name: 'Play the trip' }).click() })
    expect(now()).toBe('Photo 1 of 3 · 0 km so far') // at the end, Play restarts from the start
    act(() => { vi.advanceTimersByTime(PLAY_STEP_MS) })
    expect(now()).toMatch(/^Photo 2 of 3/)
    act(() => { vi.advanceTimersByTime(PLAY_STEP_MS * 5) })
    expect(now()).toMatch(/^Photo 3 of 3/)
    expect(screen.getByRole('button', { name: 'Play the trip' })).toBeInTheDocument() // stopped itself
  })

  it('says so when no photo has a location, instead of an empty map', async () => {
    openTrip('iberia', [photos[3]])

    expect(await screen.findByText(/None of this trip's photos have a location/)).toBeInTheDocument()
    expect(screen.queryByTestId('map')).not.toBeInTheDocument()
  })

  it('handles a trip that no longer exists', async () => {
    openTrip('gone')

    expect(await screen.findByText(/That trip doesn't exist any more/)).toBeInTheDocument()
  })
})
