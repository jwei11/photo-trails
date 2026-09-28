import type { TimelineTrip } from './api.ts'

export function plural(n: number, word: string) {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

export function formatOffset(minutes: number) {
  const sign = minutes < 0 ? '−' : '+'
  const m = Math.abs(minutes)
  return `UTC${sign}${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
}

// Dates here are local calendar days ("2025-07-01"): the day it was where the photo was
// taken. Formatting them in UTC stops the viewer's own timezone from shifting them.
const dayFormat = new Intl.DateTimeFormat(undefined, {
  weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
})
const rangeFormat = new Intl.DateTimeFormat(undefined, {
  day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC',
})

export function formatDay(date: string) {
  return dayFormat.format(new Date(date))
}

export function dateRange(first: string, last: string) {
  return first === last
    ? rangeFormat.format(new Date(first))
    : rangeFormat.formatRange(new Date(first), new Date(last))
}

/** The trip's local dates, from its first to its last photo's day. */
export function tripDates(trip: TimelineTrip) {
  const first = trip.days[0]?.date
  const last = trip.days.at(-1)?.date
  return first && last ? dateRange(first, last) : null
}

export function tripTitle(trip: TimelineTrip) {
  return trip.name ?? tripDates(trip) ?? 'Untitled trip'
}
