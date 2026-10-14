import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { renderWithApp, stubApi } from '../test/fakeApi.tsx'
import LoginPage from './LoginPage.tsx'

function renderLogin(at: string) {
  renderWithApp(
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<p>Signed in home</p>} />
      <Route path="/trips/:tripId" element={<p>Trip map</p>} />
    </Routes>,
    at,
  )
}

describe('login page', () => {
  it('offers Google when the server has it set up', async () => {
    stubApi({ '/api/auth/me': { user: null, google: true } })
    renderLogin('/login')
    const button = await screen.findByRole('link', { name: 'Sign in with Google' })
    expect(button.getAttribute('href')).toBe('/api/auth/google/start')
  })

  it('without Google, says to ask for a link', async () => {
    stubApi({ '/api/auth/me': { user: null, google: false } })
    renderLogin('/login')
    expect(await screen.findByText(/ask whoever runs this server/)).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Sign in with Google' })).toBeNull()
  })

  it('says Google can create an account while sign-ups are open', async () => {
    stubApi({ '/api/auth/me': { user: null, google: true, signup: true, demo: false } })
    renderLogin('/login')
    expect(await screen.findByRole('link', { name: 'Sign in or sign up with Google' })).toBeTruthy()
    expect(screen.queryByText(/invite-only/)).toBeNull()
  })

  it('starts a demo account when asked, and only then', async () => {
    const api = stubApi({ '/api/auth/me': { user: null, google: true, signup: false, demo: true } }, () => ({
      status: 200,
      body: { user: { id: 'g', email: 'guest-1@demo.invalid', kind: 'guest', expires_at: null }, trip_id: 't1' },
    }))
    renderLogin('/login')
    const button = await screen.findByRole('button', { name: 'Try the demo' })
    expect(api.edits()).toEqual([])
    await userEvent.click(button)
    expect(api.edits()).toEqual([{ method: 'POST', path: '/api/auth/guest', body: undefined }])
    expect(await screen.findByText('Trip map')).toBeTruthy()  // straight to the demo trip's map
  })

  it('says why a demo could not start', async () => {
    stubApi({ '/api/auth/me': { user: null, google: false, signup: false, demo: true } }, () => ({
      status: 503, body: { detail: 'The demo is full right now. Try again in a few hours.' },
    }))
    renderLogin('/login')
    await userEvent.click(await screen.findByRole('button', { name: 'Try the demo' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/demo is full/)
  })

  it('offers no demo when the server has none', async () => {
    stubApi({ '/api/auth/me': { user: null, google: true, signup: false, demo: false } })
    renderLogin('/login')
    await screen.findByRole('link', { name: 'Sign in with Google' })
    expect(screen.queryByRole('button', { name: 'Try the demo' })).toBeNull()
  })

  it('shows why a sign-in failed', async () => {
    stubApi({ '/api/auth/me': { user: null, google: true } })
    renderLogin('/login?error=x%40example.com+hasn%27t+been+invited.')
    expect((await screen.findByRole('alert')).textContent).toBe("x@example.com hasn't been invited.")
  })

  it('signs in with a link only when the button is pressed', async () => {
    const api = stubApi({ '/api/auth/me': { user: null, google: false } }, () => ({
      status: 200, body: { user: { id: 'u', email: 'a@example.com' } },
    }))
    renderLogin('/login?link=tok123')
    const button = await screen.findByRole('button', { name: 'Sign in with this link' })
    expect(api.edits()).toEqual([])  // opening the link spent nothing
    await userEvent.click(button)
    expect(api.edits()).toEqual([{ method: 'POST', path: '/api/auth/link', body: { token: 'tok123' } }])
    expect(await screen.findByText('Signed in home')).toBeTruthy()
  })

  it('says so when a link has expired', async () => {
    stubApi({ '/api/auth/me': { user: null, google: false } }, () => ({
      status: 400, body: { detail: 'This sign-in link has expired or was already used.' },
    }))
    renderLogin('/login?link=old')
    await userEvent.click(await screen.findByRole('button', { name: 'Sign in with this link' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/expired or was already used/)
  })
})
