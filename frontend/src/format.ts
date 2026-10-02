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

/** "A", "A & B", "A, B & C", or "A, B & 3 more". */
export function listNames(names: string[], max = 3) {
  if (names.length <= 1) return names[0] ?? ''
  if (names.length > max) return `${names.slice(0, 2).join(', ')} & ${names.length - 2} more`
  return `${names.slice(0, -1).join(', ')} & ${names.at(-1)}`
}

/** A title from where the trip went: its places if few, otherwise its countries. */
export function placesTitle(trip: Pick<TimelineTrip, 'places' | 'countries'>) {
  const places = trip.places.map((p) => p.name)
  const countries = trip.countries.map((c) => c.name ?? c.code)
  if (!places.length) return countries.length ? listNames(countries) : null
  if (places.length <= 2) return listNames(places)
  return countries.length >= 2 ? listNames(countries) : listNames(places, 2)
}

/** Your own name for the trip if you gave one; otherwise where it went; otherwise when. */
export function tripTitle(trip: TimelineTrip) {
  return trip.name ?? placesTitle(trip) ?? tripDates(trip) ?? 'Untitled trip'
}

/** "5 days in Portugal, 2 in Spain". */
export function countryDays(trip: Pick<TimelineTrip, 'countries'>) {
  return trip.countries
    .map((c, i) => `${i === 0 ? plural(c.days, 'day') : c.days} in ${c.name ?? c.code}`)
    .join(', ')
}
