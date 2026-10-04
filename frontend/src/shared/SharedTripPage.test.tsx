import { screen, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import type { SharedTrip } from '../api.ts'
import type { MapMarker } from '../map/TripMap.tsx'
import { renderWithApp, stubApi } from '../test/fakeApi.tsx'
import SharedTripPage from './SharedTripPage.tsx'

// Leaflet doesn't run in the simulated browser; the stand-in shows what the map was given.
vi.mock('../map/TripMap.tsx', () => ({
  default: ({ markers }: { markers: MapMarker[] }) => (
    <div data-testid="map">{markers.map((m) => <img key={m.id} alt={m.title} src={m.thumb ?? ''} />)}</div>
  ),
}))

const shared: SharedTrip = {
  name: null, precision: 'city', exact_dates: false, period: 'July 2025', start: null, end: null,
  day_count: 2, photo_count: 3, distance_km: 503,
  countries: [{ code: 'PT', name: 'Portugal', days: 1 }, { code: 'ES', name: 'Spain', days: 1 }],
  places: [{ name: 'Lisbon', country_code: 'PT', photos: 2 }, { name: 'Madrid', country_code: 'ES', photos: 1 }],
  photos: [
    { id: 'a', lat: 38.7, lon: -9.1, place_name: 'Lisbon', country_name: 'Portugal', has_preview: true, day: 1, time: '09:30' },
    { id: 'b', lat: 38.7, lon: -9.1, place_name: 'Lisbon', country_name: 'Portugal', has_preview: true, day: 1, time: '13:00' },
    { id: 'c', lat: 40.4, lon: -3.7, place_name: 'Madrid', country_name: 'Spain', has_preview: false, day: 2, time: '20:15' },
  ],
}

function open(token: string) {
  renderWithApp(<Routes><Route path="/s/:token" element={<SharedTripPage />} /></Routes>, `/s/${token}`)
}

describe('shared trip', () => {
  it('shows the trip by month and day number, with images from the share link', async () => {
    stubApi({ '/api/shared/tok': shared })
    open('tok')
    expect(await screen.findByRole('heading', { name: 'Lisbon & Madrid' })).toBeInTheDocument()
    expect(screen.getByText('July 2025')).toBeInTheDocument()
    const day1 = within(screen.getByRole('region', { name: 'Day 1' }))
    expect(day1.getByText('09:30 · Lisbon')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Day 2' })).getByText('No preview')).toBeInTheDocument()
    // Images come through the share, never the owner's photo URLs.
    const map = within(await screen.findByTestId('map'))
    expect(map.getByAltText('Day 1, 09:30').getAttribute('src')).toBe('/api/shared/tok/photos/a/thumb')
    expect(document.body.innerHTML).not.toContain('/api/photos/')
    expect(screen.getByText(/to the nearest town or city, and days are numbered/)).toBeInTheDocument()
  })

  it('shows exact dates when the owner chose them', async () => {
    stubApi({ '/api/shared/tok': { ...shared, exact_dates: true, start: '2025-07-01', end: '2025-07-02' } })
    open('tok')
    expect(await screen.findByText('2025-07-01 to 2025-07-02')).toBeInTheDocument()
  })

  it('says a turned-off or unknown link does not work', async () => {
    stubApi({}, () => ({ status: 404, body: { detail: 'Not found' } }))
    open('nope')
    expect(await screen.findByText(/This link doesn't work/)).toBeInTheDocument()
  })
})
