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
    expect(await screen.findByText(/Ask whoever runs this server/)).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Sign in with Google' })).toBeNull()
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
