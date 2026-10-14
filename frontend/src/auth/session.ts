import { useQuery } from '@tanstack/react-query'
import { api, send } from '../api.ts'
import { queryClient } from '../queryClient.ts'

export type User = {
  id: string
  email: string
  // invited: made by the server's admin. signup: signed up with Google. guest: "Try the
  // demo", deleted at expires_at. (demo is the account guests copy from; nobody signs in as it.)
  kind: 'invited' | 'signup' | 'guest' | 'demo'
  expires_at: string | null
}

export type Me = {
  user: User | null
  google: boolean // whether this server offers sign-in with Google
  signup: boolean // whether signing in with Google creates an account right now
  demo: boolean // whether "Try the demo" is available
}

export const ME_KEY = ['me']

export function useMe() {
  return useQuery({ queryKey: ME_KEY, queryFn: () => api<Me>('/auth/me'), staleTime: Infinity })
}

export async function signInWithLink(token: string) {
  await send('POST', '/auth/link', { token })
  // Anything cached belonged to whoever was signed in before.
  queryClient.clear()
}

// A new guest account holding a copy of the demo trip, signed in at once. Returns the
// trip's id, to open its map.
export async function startDemo() {
  const { trip_id } = await send<{ trip_id: string | null }>('POST', '/auth/guest')
  queryClient.clear()
  return trip_id
}

export async function signOut() {
  await send('POST', '/auth/logout')
  // Drop every cached photo and trip, so nothing of this account stays in the page.
  queryClient.clear()
}
