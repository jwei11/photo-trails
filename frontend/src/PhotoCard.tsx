import type { ReactNode } from 'react'
import { photoUrl, type Photo } from './api.ts'
import { formatOffset } from './format.ts'
import styles from './PhotoCard.module.css'

// Time sources that are guesses get a warning badge; the others are trustworthy.
const GUESSED_ZONE: Partial<Record<NonNullable<Photo['time_source']>, string>> = {
  neighbour: 'zone guessed from nearby photo',
  default_zone: 'zone unknown, default used',
}

export default function PhotoCard({ photo, actions }: { photo: Photo; actions?: ReactNode }) {
  const href = photo.web_key ? photoUrl.web(photo.id) : photoUrl.original(photo.id)
  const guess = photo.time_source ? GUESSED_ZONE[photo.time_source] : undefined
  const zone = photo.tz_name ?? (photo.local_offset_min != null ? formatOffset(photo.local_offset_min) : null)

  return (
    <figure className={styles.card}>
      <a href={href} target="_blank" rel="noreferrer" className={styles.media}>
        {photo.thumb_key ? (
          <img src={photoUrl.thumb(photo.id)} loading="lazy" alt={photo.original_filename ?? ''} />
        ) : (
          <span className={styles.noimg}>{photo.processing_error ? "Can't preview this file" : 'No preview'}</span>
        )}
      </a>
      <figcaption>
        {photo.local_time && (
          <div>
            <strong>{photo.local_time.slice(11, 16)}</strong> <span className={styles.muted}>{zone}</span>
          </div>
        )}
        {(photo.place_name || photo.country_name) && (
          <div title={photo.region_name ?? undefined}>
            {/* The neighbourhood first, when it's more precise than the place: "Baixa, Lisbon". */}
            {[
              photo.nearest_place_name !== photo.place_name ? photo.nearest_place_name : null,
              photo.place_name,
              photo.country_name,
            ].filter(Boolean).join(', ')}
          </div>
        )}
        <div className={styles.muted}>{photo.original_filename ?? '(no name)'}</div>
        {guess && <span className={`${styles.badge} ${styles.guess}`}>{guess}</span>}
        {photo.shot_at && photo.lat == null && <span className={`${styles.badge} ${styles.info}`}>no GPS</span>}
        {photo.camera_model && <div className={styles.muted}>{photo.camera_model}</div>}
        {actions && <div className={styles.actions}>{actions}</div>}
      </figcaption>
    </figure>
  )
}
