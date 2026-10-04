import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Camera, ClockOffset, Home } from '../api.ts'
import { renderWithApp, stubApi, type Call, type Reply } from '../test/fakeApi.tsx'
import SettingsPage from './SettingsPage.tsx'
import { formatShift, parseCoordinates } from './settingsFormat.ts'

const iphone: Camera = {
  camera_make: 'Apple', camera_model: 'iPhone 15', camera_serial: null, photo_count: 5,
  first_camera_time: '2025-07-01T09:30:00', last_camera_time: '2025-07-02T12:00:00',
}
const fuji: Camera = {
  camera_make: 'Fujifilm', camera_model: 'X-T5', camera_serial: 'FX1234', photo_count: 3,
  first_camera_time: '2025-07-01T11:00:00', last_camera_time: '2025-07-02T11:30:00',
}
const fujiOffset: ClockOffset = {
  id: 'off1', camera_make: 'Fujifilm', camera_model: 'X-T5', camera_serial: 'FX1234',
  offset_seconds: 3600, valid_from: '2025-07-02T00:00:00', valid_to: null, note: 'left on Lisbon time',
}
const orphanOffset: ClockOffset = {
  ...fujiOffset, id: 'off2', camera_model: 'X100V', valid_from: null, offset_seconds: -90 * 60, note: null,
}
const home: Home = { id: 'h1', name: 'Dorm', lat: 37.87, lon: -122.26, radius_km: 25 }

function setup(reply?: (call: Call) => Reply) {
  const api = stubApi(
    { '/api/cameras': [iphone, fuji], '/api/camera-offsets': [fujiOffset, orphanOffset], '/api/homes': [home] },
    reply,
  )
  renderWithApp(<SettingsPage />)
  return { api, user: userEvent.setup() }
}

// Each camera's block starts with its name in bold.
async function cameraBlock(name: string) {
  return within((await screen.findByText(name)).closest('div')!.parentElement!)
}

describe('cameras', () => {
  it('lists cameras with their corrections, including corrections for cameras with no photos', async () => {
    setup()
    const fujiBlock = await cameraBlock('Fujifilm X-T5 (#FX1234)')
    expect(fujiBlock.getByText(/Clock behind by 1h, from 2025-07-02 00:00 on/)).toBeInTheDocument()
    expect(fujiBlock.getByText(/left on Lisbon time/)).toBeInTheDocument()
    expect(screen.getByText(/clock read 2025-07-01 09:30 to 2025-07-02 12:00/)).toBeInTheDocument()
    expect(screen.getByText('Corrections for cameras with no photos')).toBeInTheDocument()
    expect(screen.getByText(/Clock ahead by 1h 30m, all photos/)).toBeInTheDocument()
  })

  it("doesn't call corrections orphans before the cameras have loaded", async () => {
    stubApi({ '/api/camera-offsets': [fujiOffset], '/api/homes': [] }, () => ({ status: 503, body: { detail: 'down' } }))
    renderWithApp(<SettingsPage />)

    expect(await screen.findByText(/Couldn't load cameras/)).toBeInTheDocument()
    expect(screen.queryByText('Corrections for cameras with no photos')).not.toBeInTheDocument()
  })

  it('applies a date-limited correction and closes the form', async () => {
    const { api, user } = setup()
    const block = await cameraBlock('Apple iPhone 15')
    await user.click(block.getByRole('button', { name: 'Correct clock…' }))
    await user.clear(block.getByLabelText('Minutes'))
    await user.type(block.getByLabelText('Minutes'), '45')
    await user.selectOptions(block.getByLabelText('Direction'), 'ahead')
    await user.type(block.getByLabelText('from'), '2025-07-02T08:00')
    await user.type(block.getByLabelText('Note'), 'daylight saving')
    await user.click(block.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(api.edits()).toEqual([{
      method: 'POST', path: '/api/camera-offsets',
      body: {
        camera_make: 'Apple', camera_model: 'iPhone 15', camera_serial: null, offset_seconds: -2700,
        valid_from: '2025-07-02T08:00', valid_to: null, note: 'daylight saving', replace_overlapping: false,
      },
    }]))
    await waitFor(() => expect(block.queryByRole('button', { name: 'Apply' })).not.toBeInTheDocument())
  })

  it('asks before replacing an overlapping correction, and replaces it in one request', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { api, user } = setup((call) =>
      call.method === 'POST' && !(call.body as { replace_overlapping: boolean }).replace_overlapping
        ? { status: 409, body: { detail: 'This camera already has an offset covering these dates.' } }
        : { status: 201, body: {} })
    const block = await cameraBlock('Fujifilm X-T5 (#FX1234)')
    await user.click(block.getByRole('button', { name: 'Correct clock…' }))
    await user.type(block.getByLabelText('Hours'), '2') // "0" + "2"
    await user.click(block.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(api.edits().map((c) => (c.body as { replace_overlapping: boolean }).replace_overlapping)).toEqual([false, true]))
    expect(confirm).toHaveBeenCalledOnce()
    expect(api.edits().some((c) => c.method === 'DELETE')).toBe(false) // no separate delete
  })

  it('changes nothing if you decline to replace, and keeps the form open', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { api, user } = setup(() => ({ status: 409, body: { detail: 'overlap' } }))
    const block = await cameraBlock('Fujifilm X-T5 (#FX1234)')
    await user.click(block.getByRole('button', { name: 'Correct clock…' }))
    await user.type(block.getByLabelText('Hours'), '2')
    await user.click(block.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(api.edits()).toHaveLength(1))
    expect(block.getByRole('button', { name: 'Apply' })).toBeEnabled()
    expect(block.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('checks the form before sending anything', async () => {
    const { api, user } = setup()
    const block = await cameraBlock('Apple iPhone 15')
    await user.click(block.getByRole('button', { name: 'Correct clock…' }))
    await user.click(block.getByRole('button', { name: 'Apply' }))
    expect(block.getByRole('alert')).toHaveTextContent('Enter how far off the clock was.')

    await user.type(block.getByLabelText('Hours'), '1')
    await user.type(block.getByLabelText('from'), '2025-07-02T08:00')
    await user.type(block.getByLabelText('to'), '2025-07-01T08:00')
    await user.click(block.getByRole('button', { name: 'Apply' }))
    expect(block.getByRole('alert')).toHaveTextContent('"From" must be before "to".')
    expect(api.edits()).toEqual([])
  })

  it('removes a correction', async () => {
    const { api, user } = setup()
    const block = await cameraBlock('Fujifilm X-T5 (#FX1234)')
    await user.click(block.getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(api.edits()).toEqual([{ method: 'DELETE', path: '/api/camera-offsets/off1', body: undefined }]))
  })
})

describe('homes', () => {
  it('adds a home from pasted coordinates and clears the form', async () => {
    const { api, user } = setup(() => ({ status: 201, body: {} }))
    await user.type(await screen.findByLabelText('Home name'), "Parents' house")
    await user.type(screen.getByLabelText('Coordinates'), ' 37.7749 ,  -122.4194 ')
    await user.clear(screen.getByLabelText('Radius in km'))
    await user.type(screen.getByLabelText('Radius in km'), '10')
    await user.click(screen.getByRole('button', { name: 'Add home' }))

    await waitFor(() => expect(api.edits()).toEqual([{
      method: 'POST', path: '/api/homes', body: { name: "Parents' house", lat: 37.7749, lon: -122.4194, radius_km: 10 },
    }]))
    await waitFor(() => expect(screen.getByLabelText('Home name')).toHaveValue(''))
  })

  it("won't send coordinates it can't read", async () => {
    const { api, user } = setup()
    await user.type(await screen.findByLabelText('Home name'), 'Dorm')
    await user.type(screen.getByLabelText('Coordinates'), 'Berkeley, CA')
    await user.click(screen.getByRole('button', { name: 'Add home' }))

    expect(screen.getByRole('alert')).toHaveTextContent('Enter coordinates as: latitude, longitude')
    expect(api.edits()).toEqual([])
  })

  it('removes a home', async () => {
    const { api, user } = setup()
    const row = within((await screen.findByText('Dorm')).closest('li')!)
    await user.click(row.getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(api.edits()).toEqual([{ method: 'DELETE', path: '/api/homes/h1', body: undefined }]))
  })
})

describe('helpers', () => {
  it('parses coordinates', () => {
    expect(parseCoordinates('37.7749, -122.4194')).toEqual({ lat: 37.7749, lon: -122.4194 })
    expect(parseCoordinates('91, 0')).toBeNull()
    expect(parseCoordinates('37.7')).toBeNull()
    expect(parseCoordinates('1, 2, 3')).toBeNull()
    expect(parseCoordinates(', 5')).toBeNull()
  })

  it('formats clock shifts', () => {
    expect(formatShift(3600)).toBe('1h')
    expect(formatShift(-5400)).toBe('1h 30m')
    expect(formatShift(45)).toBe('45s')
  })
})

describe('home suggestions', () => {
  it('offers a place that looks like home, and adds it on request', async () => {
    const api = stubApi({
      '/api/cameras': [], '/api/camera-offsets': [], '/api/homes': [],
      '/api/homes/suggestions': [{
        lat: 38.69735, lon: -9.21526, place_name: 'Belém', day_count: 6,
        first_day: '2025-07-01', last_day: '2025-07-21',
      }],
    }, () => ({ status: 201, body: {} }))
    renderWithApp(<SettingsPage />)
    expect(await screen.findByText('Near Belém')).toBeInTheDocument()
    expect(screen.getByText(/photos on 6 days/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Add as home' }))
    await waitFor(() => expect(api.edits()).toEqual([{
      method: 'POST', path: '/api/homes', body: { name: 'Belém', lat: 38.69735, lon: -9.21526, radius_km: 25 },
    }]))
  })
})
