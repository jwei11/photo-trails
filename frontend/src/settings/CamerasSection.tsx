import { useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { ApiError, settingsApi, type Camera, type ClockOffset, type NewClockOffset } from '../api.ts'
import { plural } from '../format.ts'
import { cameraKey, cameraName, clockTime, coverage, formatShift, sameCamera } from './settingsFormat.ts'
import { useEdit } from '../useEdit.ts'
import styles from './Settings.module.css'

export default function CamerasSection() {
  const cameras = useQuery({ queryKey: ['cameras'], queryFn: settingsApi.cameras })
  const offsets = useQuery({ queryKey: ['camera-offsets'], queryFn: settingsApi.offsets })
  const error = cameras.error ?? offsets.error

  // Corrections whose camera has no photos left (e.g. its photos were re-imported
  // differently) would otherwise be invisible. Only knowable once the cameras have loaded:
  // before that, every correction would look like an orphan.
  const orphans = cameras.data
    ? (offsets.data ?? []).filter((o) => !cameras.data.some((c) => sameCamera(c, o)))
    : []

  return (
    <section className={styles.panel}>
      <h2>Cameras</h2>
      <p className={styles.muted}>
        If a camera's clock was wrong, say how far off it was. Its photos move to the right
        time; the original readings are kept, so a correction can always be removed.
      </p>
      {error && <p className={styles.error}>Couldn't load cameras: {error.message}</p>}
      {cameras.data?.length === 0 && <p className={styles.muted}>No camera information yet.</p>}
      {cameras.data?.map((camera) => (
        <CameraRow key={cameraKey(camera)} camera={camera}
          offsets={(offsets.data ?? []).filter((o) => sameCamera(camera, o))} />
      ))}
      {orphans.length > 0 && (
        <div className={styles.item}>
          <strong>Corrections for cameras with no photos</strong>
          <OffsetList offsets={orphans} />
        </div>
      )}
    </section>
  )
}

function CameraRow({ camera, offsets }: { camera: Camera; offsets: ClockOffset[] }) {
  const [correcting, setCorrecting] = useState(false)
  return (
    <div className={styles.item}>
      <div className={styles.row}>
        <strong>{cameraName(camera)}</strong>
        <span className={styles.muted}>
          {plural(camera.photo_count, 'photo')}
          {camera.first_camera_time && ` · clock read ${clockTime(camera.first_camera_time)} to ${clockTime(camera.last_camera_time!)}`}
        </span>
        {!correcting && <button className="small" onClick={() => setCorrecting(true)}>Correct clock…</button>}
      </div>
      <OffsetList offsets={offsets} />
      {correcting && <CorrectionForm camera={camera} onDone={() => setCorrecting(false)} />}
    </div>
  )
}

function OffsetList({ offsets }: { offsets: ClockOffset[] }) {
  const edit = useEdit()
  if (!offsets.length) return null
  return (
    <ul className={styles.offsets}>
      {offsets.map((o) => (
        <li key={o.id}>
          <span>
            Clock {o.offset_seconds > 0 ? 'behind' : 'ahead'} by {formatShift(o.offset_seconds)}, {coverage(o)}
            {o.note && <span className={styles.muted}> · {o.note}</span>}
          </span>
          <button className="small" disabled={edit.isPending}
            onClick={() => edit.mutate(() => settingsApi.removeOffset(o.id))}>
            Remove
          </button>
        </li>
      ))}
      {edit.error && <li className={styles.error}>{edit.error.message}</li>}
    </ul>
  )
}

function CorrectionForm({ camera, onDone }: { camera: Camera; onDone: () => void }) {
  const edit = useEdit()
  const [hours, setHours] = useState('0')
  const [minutes, setMinutes] = useState('0')
  const [direction, setDirection] = useState<'behind' | 'ahead'>('behind')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [note, setNote] = useState('')
  const [problem, setProblem] = useState<string | null>(null)

  function submit(e: FormEvent) {
    e.preventDefault()
    const seconds = (direction === 'behind' ? 1 : -1) * (Number(hours) * 3600 + Number(minutes) * 60)
    if (!Number.isFinite(seconds) || seconds === 0) return setProblem('Enter how far off the clock was.')
    // Both are "YYYY-MM-DDTHH:MM" strings, which compare correctly as text.
    if (from && to && from >= to) return setProblem('"From" must be before "to".')
    setProblem(null)

    const offset: NewClockOffset = {
      camera_make: camera.camera_make,
      camera_model: camera.camera_model,
      camera_serial: camera.camera_serial,
      offset_seconds: seconds,
      valid_from: from || null,
      valid_to: to || null,
      note: note.trim() || null,
    }
    // Try to add it; if an existing correction covers some of the same times, the server
    // says 409 and nothing changes. Only then ask, and replace it in one request.
    edit.mutate(
      async () => {
        try {
          await settingsApi.addOffset(offset)
        } catch (err) {
          if (!(err instanceof ApiError && err.status === 409)) throw err
          if (!confirm('This camera already has a correction covering some of these times. Replace it?')) return false
          await settingsApi.addOffset(offset, true)
        }
        return true
      },
      { onSuccess: (applied) => applied && onDone() },
    )
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <div className={styles.row}>
        <span>Clock was</span>
        <input type="number" min={0} max={168} aria-label="Hours" value={hours} onChange={(e) => setHours(e.target.value)} />h
        <input type="number" min={0} max={59} aria-label="Minutes" value={minutes} onChange={(e) => setMinutes(e.target.value)} />m
        <select aria-label="Direction" value={direction} onChange={(e) => setDirection(e.target.value as 'behind' | 'ahead')}>
          <option value="behind">behind (add time)</option>
          <option value="ahead">ahead (subtract time)</option>
        </select>
      </div>
      <div className={styles.row}>
        <span className={styles.muted}>Only for photos with the camera clock reading (optional):</span>
        <label>from <input type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} /></label>
        <label>to <input type="datetime-local" value={to} onChange={(e) => setTo(e.target.value)} /></label>
      </div>
      <div className={styles.row}>
        <input className={styles.wide} aria-label="Note" maxLength={500} placeholder="Note, e.g. left on home time after the flight"
          value={note} onChange={(e) => setNote(e.target.value)} />
        <button type="submit" className="primary" disabled={edit.isPending}>Apply</button>
        <button type="button" onClick={onDone} disabled={edit.isPending}>Cancel</button>
      </div>
      {(problem ?? edit.error) && <p role="alert" className={styles.error}>{problem ?? edit.error?.message}</p>}
    </form>
  )
}
