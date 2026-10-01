import { useMutation, useQueryClient } from '@tanstack/react-query'

/**
 * Runs edits that change server data, one at a time.
 *
 *   const edit = useEdit()
 *   edit.mutate(() => tripEdits.rename(id, 'Lisbon'))
 *
 * After an edit succeeds, every cached query is marked stale: almost any edit ripples
 * further than its own screen (a clock correction moves photos between days and trips; a
 * new home changes which photos are trips). Queries on screen refetch at once; the rest
 * refetch when they're next shown. The edit stays pending until the on-screen refetches
 * finish, so buttons stay disabled until the page shows the result, and a second click
 * can't act on data that's about to change. `edit.error` holds the last failure.
 */
export function useEdit() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (request: () => Promise<unknown>) => request(),
    // Returning the promise is what keeps the edit pending through the refetch.
    onSuccess: () => queryClient.invalidateQueries(),
  })
}
