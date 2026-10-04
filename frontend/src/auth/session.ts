import { useQuery } from '@tanstack/react-query'
import { api, send } from '../api.ts'
import { queryClient } from '../queryClient.ts'

export type Me = {
  user: { id: string; email: string } | null
  google: boolean // whether this server offers sign-in with Google
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

export async function signOut() {
  await send('POST', '/auth/logout')
  // Drop every cached photo and trip, so nothing of this account stays in the page.
  queryClient.clear()
}
