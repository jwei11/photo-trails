import CamerasSection from './CamerasSection.tsx'
import HomesSection from './HomesSection.tsx'
import styles from './Settings.module.css'

// Rarely-changed settings that affect how every photo is read: when (camera clocks) and
// whether it's part of a trip (homes).
export default function SettingsPage() {
  return (
    <>
      <h1 className={styles.title}>Settings</h1>
      <CamerasSection />
      <HomesSection />
    </>
  )
}
