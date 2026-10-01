import { describe, expect, it, vi } from 'vitest'
import type { FileDescription, Import, ImportItem } from '../api.ts'
import { ImportEngine, PARALLEL_UPLOADS, type EngineDeps } from './importEngine.ts'

// Files whose "hash" is their name, so tests control what counts as the same file.
const file = (name: string, type = 'image/jpeg') => new File([name], name, { type })

/** A fake server that remembers what it was asked. `stored` hashes are already imported. */
function fakeServer({ stored = [] as string[], overrides = {} as Partial<EngineDeps> } = {}) {
  let imp: Import | null = null
  let urls = 0
  const item = (id: string) => imp!.items.find((i) => i.id === id)!
  const deps: EngineDeps = {
    create: vi.fn(async (files: FileDescription[]) => {
      imp = {
        id: 'imp1', created_at: '2025-07-01T00:00:00Z', counts: {} as Import['counts'],
        items: files.map((f, n): ImportItem => ({
          id: `item-${f.filename}`, position: n, filename: f.filename, content_type: f.content_type,
          size_bytes: f.size_bytes, declared_sha256: f.sha256, error: null, failed_stage: null,
          ...(stored.includes(f.sha256)
            ? { status: 'duplicate', photo_id: `existing-${f.sha256}` }
            : { status: 'uploading', photo_id: `photo-${f.filename}`, upload_url: `https://bucket/${++urls}` }),
        })),
      }
      return structuredClone(imp)
    }),
    get: vi.fn(async () => structuredClone(imp!)),
    renewUrl: vi.fn(async (_imp: string, id: string) => ({ ...item(id), upload_url: `https://bucket/${++urls}` })),
    complete: vi.fn(async (_imp: string, id: string) => Object.assign(item(id), { status: 'done' as const })),
    reportFailure: vi.fn(async (_imp: string, id: string, error: string) =>
      Object.assign(item(id), { status: 'failed' as const, failed_stage: 'upload' as const, error })),
    put: vi.fn(async (_url: string, _file: File, _type: string, onProgress: (f: number) => void) => onProgress(1)),
    sha256: vi.fn(async (f: File) => f.name),
    onChanged: vi.fn(),
    ...overrides,
  }
  return { deps, engine: new ImportEngine(deps) }
}

const statuses = (engine: ImportEngine) =>
  Object.fromEntries(engine.getSnapshot().items.map((i) => [i.filename, i.status]))

describe('ImportEngine.start', () => {
  it('hashes, registers the batch once, uploads the new files, and processes them', async () => {
    const { deps, engine } = fakeServer({ stored: ['old.jpg'] })
    await engine.start([file('a.jpg'), file('old.jpg'), file('notes.txt', 'text/plain'), file('b.heic', '')])

    expect(deps.create).toHaveBeenCalledOnce()
    expect(vi.mocked(deps.create).mock.calls[0][0]).toEqual([
      { filename: 'a.jpg', content_type: 'image/jpeg', size_bytes: 5, sha256: 'a.jpg' },
      { filename: 'old.jpg', content_type: 'image/jpeg', size_bytes: 7, sha256: 'old.jpg' },
      // Browsers often report no type for HEIC; it's sent as unknown bytes.
      { filename: 'b.heic', content_type: 'application/octet-stream', size_bytes: 6, sha256: 'b.heic' },
    ])
    expect(deps.put).toHaveBeenCalledTimes(2) // the already-imported file isn't sent
    expect(statuses(engine)).toEqual({ 'a.jpg': 'done', 'old.jpg': 'duplicate', 'b.heic': 'done' })

    const state = engine.getSnapshot()
    expect(state.skipped).toEqual(['notes.txt'])
    expect(state.phase).toBe('idle')
    expect(deps.onChanged).toHaveBeenCalled()
  })

  it('sends a file picked twice only once', async () => {
    const { deps, engine } = fakeServer()
    await engine.start([file('a.jpg'), file('a.jpg'), file('b.jpg')])

    expect(vi.mocked(deps.create).mock.calls[0][0]).toHaveLength(2)
    expect(engine.getSnapshot().repeats).toBe(1)
  })

  it(`never has more than ${PARALLEL_UPLOADS} uploads in flight`, async () => {
    let inFlight = 0
    let most = 0
    const { engine } = fakeServer({
      overrides: {
        put: async () => {
          most = Math.max(most, ++inFlight)
          await new Promise((resolve) => setTimeout(resolve, 1))
          inFlight--
        },
      },
    })
    await engine.start(Array.from({ length: 10 }, (_, n) => file(`${n}.jpg`)))

    expect(most).toBe(PARALLEL_UPLOADS)
    expect(Object.values(statuses(engine)).every((s) => s === 'done')).toBe(true)
  })

  it('retries a failed upload once with a fresh URL', async () => {
    const put = vi.fn().mockRejectedValueOnce(new Error('network error during upload')).mockResolvedValue(undefined)
    const { deps, engine } = fakeServer({ overrides: { put } })
    await engine.start([file('a.jpg')])

    expect(deps.renewUrl).toHaveBeenCalledOnce()
    expect(put.mock.calls.map((call) => call[0])).toEqual(['https://bucket/1', 'https://bucket/2'])
    expect(statuses(engine)).toEqual({ 'a.jpg': 'done' })
  })

  it('records a second upload failure on the server, and the other files still finish', async () => {
    const put = vi.fn(async (_url: string, f: File) => {
      if (f.name === 'bad.jpg') throw new Error('storage rejected the upload (403)')
    })
    const { deps, engine } = fakeServer({ overrides: { put } })
    await engine.start([file('bad.jpg'), file('good.jpg')])

    expect(deps.reportFailure).toHaveBeenCalledWith('imp1', 'item-bad.jpg', 'storage rejected the upload (403)')
    expect(statuses(engine)).toEqual({ 'bad.jpg': 'failed', 'good.jpg': 'done' })
  })

  it("doesn't upload a file the server says is already finished", async () => {
    const put = vi.fn().mockRejectedValueOnce(new Error('lost response'))
    const { deps, engine } = fakeServer({ overrides: { put } })
    // The first PUT actually arrived and was processed; the renewal says so.
    vi.mocked(deps.renewUrl).mockImplementationOnce(async (_imp, id) => {
      const { upload_url: _, ...item } = (await deps.get('imp1')).items.find((i) => i.id === id)!
      return { ...item, status: 'done' } // no upload_url: nothing left to send
    })
    await engine.start([file('a.jpg')])

    expect(put).toHaveBeenCalledOnce()
    expect(statuses(engine)).toEqual({ 'a.jpg': 'done' })
  })

  it('shows a failure to register the batch and goes idle', async () => {
    const { engine } = fakeServer({ overrides: { create: async () => { throw new Error('Server unavailable') } } })
    await engine.start([file('a.jpg')])

    expect(engine.getSnapshot()).toMatchObject({ phase: 'idle', message: 'Import failed: Server unavailable', items: [] })
  })
})

describe('ImportEngine.open', () => {
  it('shows why a past import could not be opened', async () => {
    const { engine } = fakeServer({ overrides: { get: async () => { throw new Error('Import or item not found') } } })
    await engine.open('gone')

    expect(engine.getSnapshot().message).toBe("Couldn't open that import: Import or item not found")
  })

  it('does not replace an import that started while it was loading', async () => {
    let reply!: (imp: Import) => void
    let uploadStarted!: () => void
    let finishUpload!: () => void
    const started = new Promise<void>((resolve) => { uploadStarted = resolve })
    const { engine } = fakeServer({
      overrides: {
        get: () => new Promise<Import>((resolve) => { reply = resolve }),
        put: () => new Promise<void>((resolve) => { finishUpload = resolve; uploadStarted() }),
      },
    })
    const opening = engine.open('old')
    const importing = engine.start([file('a.jpg')])
    // The old import arrives while the new one's file is mid-upload.
    await started
    reply({ id: 'old', created_at: '', counts: {} as Import['counts'], items: [] })
    await opening
    finishUpload()
    await importing

    expect(engine.getSnapshot().importId).toBe('imp1')
    expect(statuses(engine)).toEqual({ 'a.jpg': 'done' })
  })
})

describe('ImportEngine retries', () => {
  it('retries processing on the server without uploading again', async () => {
    const complete = vi.fn().mockRejectedValueOnce(new Error('Failed to fetch'))
    const { deps, engine } = fakeServer({ overrides: { complete } })
    complete.mockImplementation(async () => ({ ...engine.getSnapshot().items[0], status: 'done' }))
    await engine.start([file('a.jpg')])
    expect(engine.getSnapshot().items[0]).toMatchObject({ status: 'failed', failed_stage: 'processing' })

    expect(await engine.retry('item-a.jpg')).toBe(true)
    expect(deps.put).toHaveBeenCalledOnce()
    expect(statuses(engine)).toEqual({ 'a.jpg': 'done' })
  })

  it('after a reload, asks for the file again and resumes by matching its hash', async () => {
    const put = vi.fn(async () => { throw new Error('network error during upload') })
    const { deps, engine: first } = fakeServer({ overrides: { put } })
    await first.start([file('a.jpg'), file('b.jpg')])

    // A new engine is what a page reload gives you: same server, no files in memory.
    const engine = new ImportEngine({ ...deps, put: vi.fn(async () => {}) })
    await engine.open('imp1')
    expect(await engine.retry('item-a.jpg')).toBe(false) // the page then opens the file picker

    await engine.resume([file('a.jpg'), file('unrelated.jpg')])
    expect(statuses(engine)).toEqual({ 'a.jpg': 'done', 'b.jpg': 'failed' })
    expect(engine.getSnapshot().message).toBe("Resuming 1 file (1 didn't match).")
  })
})
