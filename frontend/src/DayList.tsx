import type { ReactNode } from 'react'
import type { Day, Photo } from './api.ts'
import { formatDay, plural } from './format.ts'
import PhotoGrid from './PhotoGrid.tsx'
import styles from './PhotoGrid.module.css'

type Props = { days: Day[]; actions?: (photo: Photo) => ReactNode }

export default function DayList({ days, actions }: Props) {
  return days.map((day) => (
    <PhotoGrid
      key={day.date}
      title={<>{formatDay(day.date)} <span className={styles.count}>· {plural(day.photos.length, 'photo')}</span></>}
      photos={day.photos}
      actions={actions}
    />
  ))
}
