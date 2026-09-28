import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Timeline } from './api.ts'
import { fakeApi, photo, renderWithApp, trip } from './test/fakeApi.tsx'
import TimelinePage from './TimelinePage.tsx'

const lisbonPhotos = [
  photo('a', { shot_at: '2025-07-01T08:30:00+00:00', local_time: '2025-07-01T09:30:00' }),
  photo('b', { shot_at: '2025-07-01T12:00:00+00:00', local_time: '2025-07-01T13:00:00' }),
  photo('c', { shot_at: '2025-07-02T09:00:00+00:00', local_time: '2025-07-02T10:00:00' }),
]
const portoPhotos = [
  photo('d', { shot_at: '2025-07-05T10:00:00+00:00', local_time: '2025-07-05T11:00:00' }),
  photo('e', { shot_at: '2025-07-05T11:00:00+00:00', local_time: '2025-07-05T12:00:00' }),
  photo('f', { shot_at: '2025-07-05T12:00:00+00:00', local_time: '2025-07-05T13:00:00' }),
]

const timeline: Timeline = {
  trips: [
    trip('lisbon', lisbonPhotos, { name: 'Lisbon' }),
    trip('porto', portoPhotos, { name: 'Porto', is_locked: true }),
  ],
  not_in_trip: [{ date: '2025-06-20', photos: [photo('home', { local_time: '2025-06-20T18:00:00' })] }],
  undated: [photo('screenshot', { shot_at: null, local_time: undefined, time_source: null })],
}

// Each trip is a <section> labelled with its title, so tests can look inside one trip.
async function tripNamed(name: string) {
  return within(await screen.findByRole('region', { name }))
}

describe('timeline', () => {
  it('shows trips, their stats, and the photos outside trips', async () => {
    fakeApi(timeline)
    renderWithApp(<TimelinePage />)

    const lisbon = await tripNamed('Lisbon')
    expect(lisbon.getByText(/2 days · 3 photos · 12 km/)).toBeInTheDocument()
    expect(within(await screen.findByRole('region', { name: 'Porto' })).getByText('edited')).toBeInTheDocument()
    expect(screen.getByText('8 photos · 2 trips')).toBeInTheDocument()
    expect(screen.getByText(/Not in a trip/)).toBeInTheDocument()
    expect(screen.getByText(/with no time information/)).toBeInTheDocument()
  })

  it("offers Split on every dated photo except the trip's first", async () => {
    fakeApi(timeline)
    renderWithApp(<TimelinePage />)

    const lisbon = await tripNamed('Lisbon')
    expect(lisbon.getAllByRole('button', { name: 'Split here' })).toHaveLength(2)
    expect(lisbon.getAllByRole('button', { name: 'Remove' })).toHaveLength(3)
  })
})

describe('crossing the date line', () => {
  // Tokyo, then an eastward flight to Honolulu: the second photo is later in time but on
  // an earlier local date, so its day is listed first.
  const tokyo = photo('tokyo', { shot_at: '2025-07-01T23:00:00+00:00', local_time: '2025-07-02T08:00:00' })
  const honolulu = photo('honolulu', { shot_at: '2025-07-02T07:00:00+00:00', local_time: '2025-07-01T21:00:00' })
  const later = photo('later', { shot_at: '2025-07-03T02:00:00+00:00', local_time: '2025-07-02T16:00:00' })
  const pacific = { ...timeline, trips: [trip('pacific', [honolulu, tokyo, later], { name: 'Pacific' })] }

  it("hides Split on the earliest photo in time, not the first one listed", async () => {
    const api = fakeApi(pacific)
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)

    const figure = async (name: string) => within((await screen.findByAltText(`${name}.jpg`)).closest('figure')!)
    expect((await figure('tokyo')).queryByRole('button', { name: 'Split here' })).not.toBeInTheDocument()
    await user.click((await figure('honolulu')).getByRole('button', { name: 'Split here' }))

    await waitFor(() =>
      expect(api.edits()).toEqual([
        { method: 'POST', path: '/api/trips/pacific/split', body: { at: '2025-07-02T07:00:00+00:00' } },
      ]),
    )
  })
})

describe('trip edits', () => {
  it('renames a trip, refetches the timeline, and closes the form', async () => {
    const api = fakeApi(timeline)
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)

    const lisbon = await tripNamed('Lisbon')
    await user.click(lisbon.getByRole('button', { name: 'Rename' }))
    const input = lisbon.getByRole('textbox', { name: 'Trip name' })
    await user.clear(input)
    await user.type(input, '  Lisbon with Sam {Enter}')

    await waitFor(() => expect(api.timelineLoads()).toBe(2))
    expect(api.edits()).toEqual([{ method: 'PATCH', path: '/api/trips/lisbon', body: { name: 'Lisbon with Sam' } }])
    await waitFor(() => expect(lisbon.queryByRole('textbox')).not.toBeInTheDocument())
  })

  it('renaming to nothing goes back to the dates', async () => {
    const api = fakeApi(timeline)
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)

    const lisbon = await tripNamed('Lisbon')
    await user.click(lisbon.getByRole('button', { name: 'Rename' }))
    await user.clear(lisbon.getByRole('textbox', { name: 'Trip name' }))
    await user.click(lisbon.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.edits()).toEqual([{ method: 'PATCH', path: '/api/trips/lisbon', body: { name: null } }]))
  })

  it('cancelling a rename sends nothing', async () => {
    const api = fakeApi(timeline)
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)

    const lisbon = await tripNamed('Lisbon')
    await user.click(lisbon.getByRole('button', { name: 'Rename' }))
    await user.type(lisbon.getByRole('textbox', { name: 'Trip name' }), 'x{Escape}')

    expect(lisbon.queryByRole('textbox')).not.toBeInTheDocument()
    expect(api.edits()).toEqual([])
  })

  it('merges a trip with the next one; the last trip has no next', async () => {
    const api = fakeApi(timeline)
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)

    expect((await tripNamed('Porto')).queryByRole('button', { name: 'Merge with next' })).not.toBeInTheDocument()
    await user.click((await tripNamed('Lisbon')).getByRole('button', { name: 'Merge with next' }))

    await waitFor(() =>
      expect(api.edits()).toEqual([{ method: 'POST', path: '/api/trips/lisbon/merge', body: { trip_ids: ['porto'] } }]),
    )
  })

  it('splits at the chosen photo’s instant', async () => {
    const api = fakeApi(timeline)
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)

    const splits = (await tripNamed('Lisbon')).getAllByRole('button', { name: 'Split here' })
    await user.click(splits[1]) // photo c, the first photo of day two

    await waitFor(() =>
      expect(api.edits()).toEqual([
        { method: 'POST', path: '/api/trips/lisbon/split', body: { at: '2025-07-02T09:00:00+00:00' } },
      ]),
    )
  })

  it('"Not a trip" asks first and does nothing if you decline', async () => {
    const api = fakeApi(timeline)
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)

    const lisbon = await tripNamed('Lisbon')
    await user.click(lisbon.getByRole('button', { name: 'Not a trip' }))
    expect(api.edits()).toEqual([])

    await user.click(lisbon.getByRole('button', { name: 'Not a trip' }))
    await waitFor(() => expect(api.edits()).toEqual([{ method: 'DELETE', path: '/api/trips/lisbon', body: undefined }]))
    expect(confirm).toHaveBeenCalledTimes(2)
  })

  it('removes a photo from its trip', async () => {
    const api = fakeApi(timeline)
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)

    await user.click((await tripNamed('Porto')).getAllByRole('button', { name: 'Remove' })[0])

    await waitFor(() => expect(api.edits()).toEqual([{ method: 'PUT', path: '/api/photos/d/trip', body: { trip_id: null } }]))
  })

  it('adds a loose photo to a trip', async () => {
    const api = fakeApi(timeline)
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)

    const selects = await screen.findAllByRole('combobox', { name: 'Add to trip' })
    expect(selects).toHaveLength(2) // the photo at home and the undated one
    await user.selectOptions(selects[0], 'Porto')

    await waitFor(() => expect(api.edits()).toEqual([{ method: 'PUT', path: '/api/photos/home/trip', body: { trip_id: 'porto' } }]))
  })

  it('shows a failed edit, keeps the old data, and re-enables the buttons', async () => {
    const api = fakeApi(timeline, () => ({ status: 400, body: { detail: 'Trips overlap' } }))
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)

    const lisbon = await tripNamed('Lisbon')
    await user.click(lisbon.getByRole('button', { name: 'Merge with next' }))

    expect(await lisbon.findByRole('alert')).toHaveTextContent('Trips overlap')
    expect(api.timelineLoads()).toBe(1) // nothing changed, so nothing to refetch
    expect(lisbon.getByRole('button', { name: 'Merge with next' })).toBeEnabled()
  })

  it('disables the trip’s buttons while an edit is in flight', async () => {
    let finish!: () => void
    const api = fakeApi(timeline)
    const user = userEvent.setup()
    renderWithApp(<TimelinePage />)
    const lisbon = await tripNamed('Lisbon')

    // Hold the next edit open until the test releases it.
    const realFetch = globalThis.fetch
    vi.stubGlobal('fetch', vi.fn((url: string, options?: RequestInit) =>
      options?.method === 'POST'
        ? new Promise<Response>((resolve) => { finish = () => resolve(new Response(null, { status: 204 })) })
        : realFetch(url, options)))

    await user.click(lisbon.getByRole('button', { name: 'Merge with next' }))
    expect(lisbon.getByRole('button', { name: 'Not a trip' })).toBeDisabled()

    finish()
    await waitFor(() => expect(lisbon.getByRole('button', { name: 'Not a trip' })).toBeEnabled())
    expect(api.timelineLoads()).toBe(2)
  })
})
