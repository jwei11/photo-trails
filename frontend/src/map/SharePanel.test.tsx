import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { Share, Sharing } from '../api.ts'
import { renderWithApp, stubApi } from '../test/fakeApi.tsx'
import SharePanel from './SharePanel.tsx'

const link: Share = {
  id: 's1', token: 'tok', path: '/s/tok', precision: 'city', exact_dates: false,
  created_at: '2025-08-01T10:00:00Z', revoked_at: null,
}

function sharing(overrides: Partial<Sharing> = {}): Sharing {
  return {
    shares: [], can_share: true, has_homes: true, hidden_near_home: 0, hidden_recent: 0,
    shareable_from: '2025-07-10T10:00:00Z', ...overrides,
  }
}

function setup(data: Sharing) {
  const api = stubApi({ '/api/trips/t1/shares': data }, () => ({ status: 201, body: link }))
  renderWithApp(<SharePanel tripId="t1" />)
  return api
}

describe('share panel', () => {
  it('creates a link with the cautious defaults', async () => {
    const api = setup(sharing())
    await userEvent.click(await screen.findByRole('button', { name: 'Create link' }))
    await waitFor(() => expect(api.edits()).toEqual([{
      method: 'POST', path: '/api/trips/t1/shares', body: { precision: 'city', exact_dates: false },
    }]))
  })

  it('can make a more precise link with exact dates', async () => {
    const api = setup(sharing())
    await userEvent.click(await screen.findByLabelText(/To about a kilometre/))
    await userEvent.click(screen.getByLabelText(/Show exact dates/))
    await userEvent.click(screen.getByRole('button', { name: 'Create link' }))
    await waitFor(() => expect(api.edits()[0].body).toEqual({ precision: 'neighbourhood', exact_dates: true }))
  })

  it('warns when no home is set, and says what will be left out', async () => {
    setup(sharing({ has_homes: false, hidden_near_home: 2, hidden_recent: 1 }))
    expect(await screen.findByText(/You haven't set a home/)).toBeInTheDocument()
    expect(screen.getByText('2 photos near a home won\'t be shown.')).toBeInTheDocument()
    expect(screen.getByText('1 photo from the last week won\'t be shown.')).toBeInTheDocument()
  })

  it("won't make a link until a week after the trip", async () => {
    setup(sharing({ can_share: false, shareable_from: '2026-10-09T18:00:00Z' }))
    expect(await screen.findByText(/You can share this trip from .*a week after it ends/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create link' })).toBeNull()
  })

  it('lists active links, and turns one off', async () => {
    const api = setup(sharing({ shares: [link, { ...link, id: 's2', revoked_at: '2025-08-02T10:00:00Z' }] }))
    const box = await screen.findByLabelText('Share link')
    expect(box).toHaveValue(`${window.location.origin}/s/tok`)
    expect(screen.getAllByLabelText('Share link')).toHaveLength(1)  // the revoked one isn't listed
    expect(screen.getByText(/Towns and cities · day numbers/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    await waitFor(() => expect(api.edits()).toEqual([{ method: 'DELETE', path: '/api/shares/s1', body: undefined }]))
  })
})
