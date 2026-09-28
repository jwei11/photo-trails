import type { ReactNode } from 'react'
import type { Photo } from './api.ts'
import PhotoCard from './PhotoCard.tsx'
import styles from './PhotoGrid.module.css'

type Props = {
  title?: ReactNode
  photos: Photo[]
  // Buttons to show under each photo; which ones depends on where the grid is.
  actions?: (photo: Photo) => ReactNode
}

export default function PhotoGrid({ title, photos, actions }: Props) {
  return (
    <div>
      {title && <h3 className={styles.title}>{title}</h3>}
      <div className={styles.grid}>
        {photos.map((p) => (
          <PhotoCard key={p.id} photo={p} actions={actions?.(p)} />
        ))}
      </div>
    </div>
  )
}
