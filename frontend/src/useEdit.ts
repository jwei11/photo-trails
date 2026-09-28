import { useMutation, useQueryClient } from '@tanstack/react-query'

/**
 * Runs edits that change the timeline, one at a time.
 *
 *   const edit = useEdit()
 *   edit.mutate(() => tripEdits.rename(id, 'Lisbon'))
 *
 * After an edit succeeds, the cached timeline is marked stale and refetched. The edit
 * stays pending until that refetch finishes, so buttons stay disabled until the page
 * shows the result, and a second click can't act on data that's about to change.
 * `edit.error` holds the last failure for the component to show.
 */
export function useEdit() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (request: () => Promise<unknown>) => request(),
    // Returning the promise is what keeps the edit pending through the refetch.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['timeline'] }),
  })
}
