import { useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { settingsApi } from '../api.ts'
import { useEdit } from '../useEdit.ts'
import { parseCoordinates } from './settingsFormat.ts'
import styles from './Settings.module.css'

export default function HomesSection() {
  const homes = useQuery({ queryKey: ['homes'], queryFn: settingsApi.homes })
  const remove = useEdit()

  return (
    <section className={styles.panel}>
      <h2>Homes</h2>
      <p className={styles.muted}>
        Photos near a home aren't counted as trips. Get coordinates from a map app (e.g.
        right-click a spot in Google Maps).
      </p>
      {homes.error && <p className={styles.error}>Couldn't load homes: {homes.error.message}</p>}
      {homes.data?.length === 0 && (
        <p className={styles.muted}>No home set: photos taken at home may be grouped into trips.</p>
      )}
      {homes.data && homes.data.length > 0 && (
        <ul className={styles.offsets}>
          {homes.data.map((h) => (
            <li key={h.id}>
              <span>
                <strong>{h.name}</strong>{' '}
                <span className={styles.muted}>{h.lat.toFixed(4)}, {h.lon.toFixed(4)} · within {h.radius_km} km</span>
              </span>
              <button className="small" disabled={remove.isPending}
                onClick={() => remove.mutate(() => settingsApi.removeHome(h.id))}>
                Remove
              </button>
            </li>
          ))}
          {remove.error && <li className={styles.error}>{remove.error.message}</li>}
        </ul>
      )}
      <AddHomeForm />
    </section>
  )
}

function AddHomeForm() {
  const add = useEdit()
  const [name, setName] = useState('')
  const [coords, setCoords] = useState('')
  const [radius, setRadius] = useState('25')
  const [problem, setProblem] = useState<string | null>(null)

  function submit(e: FormEvent) {
    e.preventDefault()
    const point = parseCoordinates(coords)
    if (!point) return setProblem('Enter coordinates as: latitude, longitude (e.g. 37.7749, -122.4194)')
    setProblem(null)
    add.mutate(() => settingsApi.addHome({ name: name.trim(), ...point, radius_km: Number(radius) }), {
      onSuccess: () => {
        setName('')
        setCoords('')
        setRadius('25')
      },
    })
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <div className={styles.row}>
        <input aria-label="Home name" placeholder="Name, e.g. Parents' house" required maxLength={100}
          value={name} onChange={(e) => setName(e.target.value)} />
        <input aria-label="Coordinates" placeholder="37.7749, -122.4194" required
          value={coords} onChange={(e) => setCoords(e.target.value)} />
        <label className={styles.muted}>
          within <input type="number" aria-label="Radius in km" min={1} max={500} step={1} required
            value={radius} onChange={(e) => setRadius(e.target.value)} /> km
        </label>
        <button type="submit" className="primary" disabled={add.isPending}>Add home</button>
      </div>
      {(problem ?? add.error) && <p role="alert" className={styles.error}>{problem ?? add.error?.message}</p>}
    </form>
  )
}
