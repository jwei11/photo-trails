import { useSyncExternalStore } from 'react'
import { importsApi } from '../api.ts'
import { queryClient } from '../queryClient.ts'
import { putWithProgress, sha256Hex } from './browser.ts'
import { ImportEngine } from './importEngine.ts'

// One engine for the whole app, created when this module is first imported. It lives as
// long as the page does, independent of which components are on screen.
export const importEngine = new ImportEngine({
  ...importsApi,
  put: putWithProgress,
  sha256: sha256Hex,
  wait: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  // New photos change the timeline, the import history, and the camera list.
  onChanged: () => queryClient.invalidateQueries(),
})

/** The current import state; the component re-renders whenever it changes. */
export function useImport() {
  return useSyncExternalStore(importEngine.subscribe, importEngine.getSnapshot)
}
