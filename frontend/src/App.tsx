import { Link, Route, Routes } from 'react-router'
import styles from './App.module.css'
import TimelinePage from './TimelinePage.tsx'

export default function App() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link to="/" className={styles.title}>
          Trip Mapper
        </Link>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<TimelinePage />} />
          <Route path="*" element={<p>Page not found.</p>} />
        </Routes>
      </main>
    </div>
  )
}
