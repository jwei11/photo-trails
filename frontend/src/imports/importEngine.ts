import type { FileDescription, Import, ImportItem } from '../api.ts'
import { plural } from '../format.ts'

// The browser hashes each file, registers the whole batch in one request (files the server
// already has come back as duplicates), then uploads the rest straight to storage a few at
// a time and tells the server each one arrived. The server's worker processes them in the
// background; the engine then follows the import, polling until every file is done.

export const PARALLEL_UPLOADS = 4 // matches the parallelism measured in M3
export const POLL_MS = 1000 // how often to check on files the worker is processing

// Same rule as the server: images except SVG (it can contain script), or unknown bytes.
const UPLOADABLE = /^(image\/(?!svg)[\w.+-]+|application\/octet-stream)$/

export type ImportState = {
  importId: string | null
  items: ImportItem[]
  // Upload progress (0..1) for items sent in this session. Finished items that have an
  // entry are still listed, so you can see what just went through.
  progress: ReadonlyMap<string, number>
  // The File behind each item, while it's in memory. After a page reload this is empty,
  // and unfinished items have to be picked again to resume.
  files: ReadonlyMap<string, File>
  // hashing and uploading need this page; while they run, no other import can start.
  phase: 'idle' | 'hashing' | 'uploading'
  // Following the worker's progress. Not "busy": the work is on the server, so another
  // import can start (this one keeps going there) and the page can be closed.
  processing: boolean
  hashed: number // files hashed so far, while phase is 'hashing'
  toHash: number
  skipped: string[] // names of files that aren't images
  repeats: number // files picked twice in one batch, sent once
  message: string | null // a one-off note or error about the batch as a whole
}

// Everything the engine does to the outside world, passed in so tests can fake it.
export type EngineDeps = {
  create: (files: FileDescription[]) => Promise<Import>
  get: (importId: string) => Promise<Import>
  renewUrl: (importId: string, itemId: string) => Promise<ImportItem>
  complete: (importId: string, itemId: string) => Promise<ImportItem>
  reportFailure: (importId: string, itemId: string, error: string) => Promise<ImportItem>
  put: (url: string, file: File, contentType: string, onProgress: (fraction: number) => void) => Promise<void>
  sha256: (file: File) => Promise<string>
  wait: (ms: number) => Promise<void>
  // Called when files may have become photos, so cached server data can be refreshed.
  onChanged: () => void
}

const EMPTY: ImportState = {
  importId: null, items: [], progress: new Map(), files: new Map(), phase: 'idle',
  processing: false, hashed: 0, toHash: 0, skipped: [], repeats: 0, message: null,
}

export function needsUpload(item: ImportItem) {
  return item.status === 'uploading' || (item.status === 'failed' && item.failed_stage === 'upload')
}

/**
 * Owns one import at a time. React reads it through `subscribe` and `getSnapshot`, the
 * two functions `useSyncExternalStore` needs: every change replaces `state` with a new
 * object (never mutating the old one) and tells the listeners, so React can tell what
 * changed by comparing references.
 */
export class ImportEngine {
  private state: ImportState = EMPTY
  private listeners = new Set<() => void>()
  private deps: EngineDeps
  // Each follow() loop takes the next number; a loop that sees a newer one stops.
  private following = 0

  constructor(deps: EngineDeps) {
    this.deps = deps
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  getSnapshot = () => this.state

  get busy() {
    return this.state.phase !== 'idle'
  }

  private update(change: Partial<ImportState>) {
    this.state = { ...this.state, ...change }
    this.listeners.forEach((listener) => listener())
  }

  private setItem(item: Partial<ImportItem> & { id: string }) {
    this.update({ items: this.state.items.map((i) => (i.id === item.id ? { ...i, ...item } : i)) })
  }

  private setProgress(itemId: string, fraction: number) {
    this.update({ progress: new Map(this.state.progress).set(itemId, fraction) })
  }

  /** Import a batch of files the user just picked. */
  async start(picked: File[]) {
    if (this.busy) return
    const skipped = picked.filter((f) => !UPLOADABLE.test(f.type || 'application/octet-stream'))
    const files = picked.filter((f) => !skipped.includes(f))
    if (!files.length) {
      this.update({ ...EMPTY, message: skipped.length ? 'Only image files can be imported.' : null })
      return
    }
    this.following++ // stop following any previous import; it carries on on the server
    this.update({ ...EMPTY, phase: 'hashing', toHash: files.length, skipped: skipped.map((f) => f.name) })
    try {
      // The same file picked twice (e.g. from two folders) is only sent once.
      const seen = new Set<string>()
      const unique: File[] = []
      const described: FileDescription[] = []
      for (const [n, f] of files.entries()) {
        const sha256 = await this.deps.sha256(f)
        this.update({ hashed: n + 1 })
        if (seen.has(sha256)) continue
        seen.add(sha256)
        unique.push(f)
        described.push({ filename: f.name, content_type: f.type || 'application/octet-stream', size_bytes: f.size, sha256 })
      }
      const created = await this.deps.create(described)
      // The server returns items in the order they were described.
      const byItem = new Map(created.items.map((item, i) => [item.id, unique[i]]))
      this.update({
        importId: created.id,
        items: created.items,
        files: byItem,
        repeats: files.length - unique.length,
        phase: 'uploading',
      })
      const uploads = created.items.filter((i) => i.status === 'uploading')
      await pool(uploads.map((i) => () => this.upload(created.id, i, byItem.get(i.id)!)), PARALLEL_UPLOADS)
    } catch (e) {
      this.update({ message: `Import failed: ${errorText(e)}` })
    } finally {
      this.update({ phase: 'idle' })
      this.deps.onChanged()
    }
    this.followIfProcessing()
  }

  /** Show a past import, e.g. to retry its failures. Its files aren't in memory. */
  async open(importId: string) {
    if (this.busy) return
    let imp: Import
    try {
      imp = await this.deps.get(importId)
    } catch (e) {
      if (!this.busy) this.update({ message: `Couldn't open that import: ${errorText(e)}` })
      return
    }
    // An import started while this was loading takes priority: showing the old one now
    // would replace the running import's state.
    if (this.busy) return
    this.following++
    this.update({ ...EMPTY, importId: imp.id, items: imp.items })
    this.followIfProcessing()
  }

  /** Match re-picked files to unfinished items by content hash, then upload them. */
  async resume(picked: File[]) {
    const importId = this.state.importId
    if (this.busy || !importId) return
    this.update({ phase: 'hashing', hashed: 0, toHash: picked.length, message: null })
    try {
      const byHash = new Map(this.state.items.filter(needsUpload).map((i) => [i.declared_sha256, i]))
      const files = new Map(this.state.files)
      const matched: ImportItem[] = []
      for (const [n, f] of picked.entries()) {
        const item = byHash.get(await this.deps.sha256(f))
        this.update({ hashed: n + 1 })
        if (item && !files.has(item.id)) {
          files.set(item.id, f)
          matched.push(item)
        }
      }
      const unmatched = picked.length - matched.length
      this.update({
        files,
        phase: 'uploading',
        message: `Resuming ${plural(matched.length, 'file')}${unmatched ? ` (${unmatched} didn't match)` : ''}.`,
      })
      // Their old URLs may have expired: get fresh ones.
      await pool(matched.map((i) => () => this.upload(importId, { ...i, upload_url: undefined }, files.get(i.id)!)), PARALLEL_UPLOADS)
    } finally {
      this.update({ phase: 'idle' })
      this.deps.onChanged()
    }
    this.followIfProcessing()
  }

  /**
   * Retry one failed item. A processing failure is retried on the server, since the file
   * is already stored. An upload failure needs the file: returns false if it isn't in
   * memory, so the page can ask for it to be picked again.
   */
  async retry(itemId: string): Promise<boolean> {
    const { importId, items, files } = this.state
    const item = items.find((i) => i.id === itemId)
    if (!item || this.busy || !importId) return true
    const file = files.get(item.id)
    if (item.failed_stage === 'upload' && !file) return false
    this.update({ phase: 'uploading', message: null })
    try {
      if (item.failed_stage === 'processing') {
        this.setItem({ id: item.id, status: 'processing', error: null })
        await this.finish(importId, item)
      } else {
        await this.upload(importId, { ...item, upload_url: undefined }, file!)
      }
    } finally {
      this.update({ phase: 'idle' })
      this.deps.onChanged()
    }
    this.followIfProcessing()
    return true
  }

  // Upload one file and have the server process it. Never throws: a failure is recorded
  // on the server (so it survives a reload) and shown on the item. The import and file are
  // passed in, fixed when the upload was queued, rather than read from the current state.
  private async upload(importId: string, item: ImportItem, file: File) {
    // A renewal can report the file already finished (e.g. a lost response); then we're done.
    const renew = async () => {
      const renewed = await this.deps.renewUrl(importId, item.id)
      if (!renewed.upload_url) this.setItem(renewed)
      return renewed
    }
    try {
      if (!item.upload_url) {
        item = await renew()
        if (!item.upload_url) return
      }
      this.setItem({ ...item, status: 'uploading', error: null })
      const onProgress = (fraction: number) => this.setProgress(item.id, fraction)
      this.setProgress(item.id, 0)
      try {
        await this.deps.put(item.upload_url!, file, item.content_type, onProgress)
      } catch {
        // One automatic retry with a fresh URL: covers expired URLs in long imports and
        // brief network blips. A second failure is reported and shown with a Retry button.
        item = await renew()
        if (!item.upload_url) return
        await this.deps.put(item.upload_url, file, item.content_type, onProgress)
      }
      this.setProgress(item.id, 1)
      this.setItem({ id: item.id, status: 'processing' })
      await this.finish(importId, item)
    } catch (e) {
      try {
        this.setItem(await this.deps.reportFailure(importId, item.id, errorText(e)))
      } catch {
        // Couldn't even record it: show it locally. It still reads as "uploading" on the
        // server, so reopening the import later offers to resume it.
        this.setItem({ id: item.id, status: 'failed', failed_stage: 'upload', error: errorText(e) })
      }
    }
  }

  private followIfProcessing() {
    const { importId, items, processing } = this.state
    if (importId && !processing && items.some((i) => i.status === 'processing')) void this.follow(importId)
  }

  /** Poll the import until the worker has finished every file, updating items as it goes. */
  private async follow(importId: string) {
    const me = ++this.following
    const current = () => me === this.following && this.state.importId === importId
    this.update({ processing: true })
    try {
      while (current()) {
        const imp = await this.deps.get(importId)
        if (!current()) return
        // The server is the truth for status; keep only what it doesn't know (upload URLs).
        const fresh = new Map(imp.items.map((i) => [i.id, i]))
        this.update({ items: this.state.items.map((i) => ({ ...i, ...fresh.get(i.id) })) })
        if (!imp.items.some((i) => i.status === 'processing')) break
        await this.deps.wait(POLL_MS)
      }
    } catch (e) {
      if (current()) this.update({ message: `Couldn't check progress: ${errorText(e)}. Reopen the import to check again.` })
    } finally {
      if (me === this.following) {
        this.update({ processing: false })
        this.deps.onChanged() // processed photos are now on the timeline
      }
    }
  }

  private async finish(importId: string, item: ImportItem) {
    try {
      this.setItem(await this.deps.complete(importId, item.id))
    } catch (e) {
      // The request itself failed (network, server down): the file is stored, so it's a
      // processing retry, not a new upload.
      this.setItem({ id: item.id, status: 'failed', failed_stage: 'processing', error: errorText(e) })
    }
  }
}

/** Run tasks with at most `limit` in flight at once. */
export async function pool(tasks: (() => Promise<void>)[], limit: number) {
  let next = 0
  // Each worker takes the next task as soon as its current one finishes. JavaScript runs
  // one piece of code at a time, so `next++` can't hand the same task to two workers.
  const worker = async () => {
    while (next < tasks.length) await tasks[next++]()
  }
  await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker))
}

function errorText(e: unknown) {
  return e instanceof Error ? e.message : String(e)
}
