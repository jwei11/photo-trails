import { describe, expect, it } from 'vitest'
import { countryDays, listNames, placesTitle } from './format.ts'

const place = (name: string, country_code = 'PT') => ({ name, country_code, photos: 1 })
const country = (code: string, name: string, days = 1) => ({ code, name, days })

describe('trip titles from places', () => {
  it('lists names the way you would say them', () => {
    expect(listNames(['Lisbon'])).toBe('Lisbon')
    expect(listNames(['Lisbon', 'Madrid'])).toBe('Lisbon & Madrid')
    expect(listNames(['Lisbon', 'Porto', 'Madrid'])).toBe('Lisbon, Porto & Madrid')
    expect(listNames(['A', 'B', 'C', 'D', 'E'])).toBe('A, B & 3 more')
  })

  it('names a trip after its places when there are one or two', () => {
    expect(placesTitle({ places: [place('Lisbon')], countries: [country('PT', 'Portugal')] })).toBe('Lisbon')
    expect(placesTitle({
      places: [place('Lisbon'), place('Madrid', 'ES')],
      countries: [country('PT', 'Portugal'), country('ES', 'Spain')],
    })).toBe('Lisbon & Madrid')
  })

  it('names a trip with many places across countries after the countries', () => {
    expect(placesTitle({
      places: [place('Lisbon'), place('Sintra'), place('Madrid', 'ES')],
      countries: [country('PT', 'Portugal'), country('ES', 'Spain')],
    })).toBe('Portugal & Spain')
  })

  it('names a trip with many places in one country after the first places', () => {
    expect(placesTitle({
      places: [place('Lisbon'), place('Sintra'), place('Cascais'), place('Porto')],
      countries: [country('PT', 'Portugal')],
    })).toBe('Lisbon, Sintra & 2 more')
  })

  it('has no place title without location data', () => {
    expect(placesTitle({ places: [], countries: [] })).toBeNull()
  })
})

describe('days per country', () => {
  it('reads like a sentence', () => {
    expect(countryDays({ countries: [country('PT', 'Portugal', 5), country('ES', 'Spain', 2)] }))
      .toBe('5 days in Portugal, 2 in Spain')
    expect(countryDays({ countries: [country('PT', 'Portugal', 1)] })).toBe('1 day in Portugal')
    expect(countryDays({ countries: [] })).toBe('')
  })
})
