import { useQuery } from '@tanstack/react-query'
import { useRef, type ChangeEvent } from 'react'
import { importsApi, photoUrl, type ImportItem } from '../api.ts'
import { plural } from '../format.ts'
import { needsUpload, type ImportState } from './importEngine.ts'
import styles from './ImportPage.module.css'
import { importEngine, useImport } from './useImport.ts'

const STATUS_TEXT: Record<ImportItem['status'], string> = {
  uploading: 'waiting',
  processing: 'processing',
  done: 'imported',
  duplicate: 'already imported',
  failed: 'failed',
}

export default function ImportPage() {
  const state = useImport()
  // A hidden second file input: for re-picking files to resume or retry an upload.
  const resumeInput = useRef<HTMLInputElement>(null)
  const busy = state.phase !== 'idle'

  // Picking files starts the import at once; the input is cleared so the same files can
  // be picked again later.
  function onPick(e: ChangeEvent<HTMLInputElement>, action: (files: File[]) => Promise<unknown>) {
    const files = [...(e.target.files ?? [])]
    e.target.value = ''
    if (files.length) void action(files)
  }

  async function retry(item: ImportItem) {
    // False means the file isn't in memory (the page was reloaded): ask for it again.
    if (!(await importEngine.retry(item.id))) resumeInput.current?.click()
  }

  const needFiles = state.items.filter((i) => needsUpload(i) && !state.files.has(i.id))
  // Only list what needs attention or went through in this session; the rest are counted.
  const shown = state.items.filter((i) => !['done', 'duplicate'].includes(i.status) || state.progress.has(i.id))

  return (
    <>
      <h1 className={styles.title}>Import photos</h1>
      <section className={styles.panel}>
        <label className={styles.pick}>
          <input type="file" multiple accept="image/*,.heic,.heif" disabled={busy}
            onChange={(e) => onPick(e, (files) => importEngine.start(files))} />
        </label>
        <input ref={resumeInput} type="file" multiple hidden aria-label="Choose files to resume"
          onChange={(e) => onPick(e, (files) => importEngine.resume(files))} />
        <p className={styles.summary} aria-live="polite">{summary(state)}</p>
        {state.message && <p className={styles.muted}>{state.message}</p>}
        {state.processing && state.phase === 'idle' && (
          <p className={styles.muted}>
            Uploads are done; the server is processing the rest. You can leave this page.
          </p>
        )}
        {needFiles.length > 0 && !busy && (
          <p>
            {plural(needFiles.length, 'file')} still {needFiles.length === 1 ? 'needs' : 'need'} uploading.{' '}
            <button className="small" onClick={() => resumeInput.current?.click()}>Choose them again to resume</button>
          </p>
        )}
        {shown.length > 0 && (
          <div className={styles.items}>
            {shown.slice(0, 200).map((item) => (
              <ItemRow key={item.id} item={item} progress={state.progress.get(item.id)} busy={busy} onRetry={retry} />
            ))}
          </div>
        )}
      </section>
      <PastImports busy={busy} />
    </>
  )
}

function summary(state: ImportState) {
  if (state.phase === 'hashing') return `Checking files for duplicates… ${state.hashed} of ${state.toHash}`
  const { items } = state
  if (!items.length) return 'Pick photos to import. Files already imported are skipped without uploading.'
  const count = (status: ImportItem['status']) => items.filter((i) => i.status === status).length
  const parts = [`${count('done') + count('duplicate')} of ${items.length} done`]
  if (count('processing')) parts.push(`${count('processing')} processing`)
  if (count('duplicate')) parts.push(`${count('duplicate')} already imported`)
  if (count('failed')) parts.push(`${count('failed')} failed`)
  if (state.skipped.length) parts.push(`${state.skipped.length} skipped (not images)`)
  if (state.repeats) parts.push(`${state.repeats} selected twice, sent once`)
  return parts.join(' · ')
}

type RowProps = { item: ImportItem; progress?: number; busy: boolean; onRetry: (item: ImportItem) => void }

function ItemRow({ item, progress, busy, onRetry }: RowProps) {
  const pct = Math.round((progress ?? 0) * 100)
  const sending = item.status === 'uploading' && progress !== undefined && pct < 100
  const finished = item.status === 'done' || item.status === 'duplicate'
  return (
    <div className={`${styles.row} ${item.status === 'failed' ? styles.failed : ''}`}>
      <span className={styles.name}>{item.filename ?? '(no name)'}</span>
      <span className={styles.muted}>{formatSize(item.size_bytes)}</span>
      <progress max={100} value={item.status === 'uploading' ? pct : 100} />
      <span className={styles.state}>{sending ? `uploading ${pct}%` : STATUS_TEXT[item.status]}</span>
      {finished && item.photo_id ? (
        <a href={photoUrl.web(item.photo_id)} target="_blank" rel="noreferrer">view</a>
      ) : <span />}
      {item.status === 'failed' ? (
        <button className="small" disabled={busy} onClick={() => onRetry(item)}>
          {item.failed_stage === 'processing' ? 'Retry processing' : 'Retry upload'}
        </button>
      ) : <span />}
      {item.error && <div className={styles.error}>{item.error}</div>}
    </div>
  )
}

function PastImports({ busy }: { busy: boolean }) {
  const { data, error } = useQuery({ queryKey: ['imports'], queryFn: importsApi.list })
  return (
    <details className={styles.past}>
      <summary>Recent imports</summary>
      {error && <p className={styles.error}>{error.message}</p>}
      {data?.length === 0 && <p className={styles.muted}>No imports yet.</p>}
      {data?.map((imp) => {
        const c = imp.counts
        const bits = [`${c.done + c.duplicate} of ${c.total} done`]
        if (c.failed) bits.push(`${c.failed} failed`)
        if (c.uploading) bits.push(`${c.uploading} not uploaded`)
        return (
          <div key={imp.id} className={styles.pastRow}>
            <span>{new Date(imp.created_at).toLocaleString()}</span>
            <span className={styles.muted}>{bits.join(' · ')}</span>
            {(c.failed > 0 || c.uploading > 0) && (
              <button className="small" disabled={busy} onClick={() => importEngine.open(imp.id)}>Open</button>
            )}
          </div>
        )
      })}
    </details>
  )
}

function formatSize(bytes: number) {
  return bytes > 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1e3))} KB`
}
