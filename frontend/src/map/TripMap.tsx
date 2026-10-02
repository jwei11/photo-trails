import L from 'leaflet'
import 'leaflet.markercluster' // adds L.markerClusterGroup to Leaflet
import 'leaflet/dist/leaflet.css'
import 'leaflet.markercluster/dist/MarkerCluster.css'
import { useEffect, useMemo } from 'react'
import { CircleMarker, MapContainer, Pane, Polyline, TileLayer, useMap } from 'react-leaflet'
import { photoUrl } from '../api.ts'
import './map.css'
import type { Located, Stop } from './route.ts'

// OpenStreetMap's own tile servers: fine for development with attribution, not for a
// deployed app's traffic (see the README). Changing provider is this URL.
const TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
const ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

type Props = {
  stops: Stop[] // the route, in time order
  markers: Located[] // every located photo, including undated ones
  current: number // index into stops: where the scrubber is
  onSelect: (photoId: string) => void
}

export default function TripMap({ stops, markers, current, onSelect }: Props) {
  // Fit the whole trip on first render. Computed once: refitting on every scrubber move
  // would fight the user's own zooming.
  const bounds = useMemo(
    () => L.latLngBounds(markers.map((p) => [p.lat, p.lon] as [number, number])).pad(0.1),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  )
  const line = stops.map((s) => [s.lat, s.lon] as [number, number])
  const here = stops[current]

  return (
    // No scroll-wheel zoom: scrolling the page over the map would zoom it instead.
    // The +/- buttons, double-click, and pinch still zoom.
    <MapContainer bounds={bounds} maxZoom={18} className="trip-map" scrollWheelZoom={false}>
      <TileLayer url={TILES} attribution={ATTRIBUTION} maxZoom={19} />
      {/* The whole route faintly, and the part walked so far, up to the scrubber, solid.
          className is a direct prop, not in pathOptions: react-leaflet applies pathOptions
          with Leaflet's setStyle, which ignores className, so it only works at creation. */}
      <Polyline positions={line} className="route-ahead" weight={3} dashArray="4 6" />
      <Polyline positions={line.slice(0, current + 1)} className="route-done" weight={4} />
      <PhotoClusters photos={markers} onSelect={onSelect} />
      {/* Its own pane above the markers (which sit at z-index 600), so a cluster can't hide it. */}
      <Pane name="here" style={{ zIndex: 650 }}>
        {here && (
          // A ring, not a dot, and not clickable: a cluster under it stays readable
          // and clickable.
          <CircleMarker center={[here.lat, here.lon]} radius={26} className="route-here" weight={4} interactive={false} />
        )}
      </Pane>
      {here && <FollowPoint lat={here.lat} lon={here.lon} />}
    </MapContainer>
  )
}

/** Keep the scrubber's point on screen: pan only when it would leave the view. */
function FollowPoint({ lat, lon }: { lat: number; lon: number }) {
  const map = useMap()
  useEffect(() => {
    if (!map.getBounds().pad(-0.1).contains([lat, lon])) map.panTo([lat, lon])
  }, [map, lat, lon])
  return null
}

/**
 * Photo markers, merged into numbered clusters where they're close together on screen.
 * Leaflet.markercluster isn't a React component, so this creates the layer itself and
 * rebuilds its markers when the photos change.
 */
function PhotoClusters({ photos, onSelect }: { photos: Located[]; onSelect: (id: string) => void }) {
  const map = useMap()
  useEffect(() => {
    const layer = L.markerClusterGroup({
      showCoverageOnHover: false,
      maxClusterRadius: 48, // pixels: markers closer than this on screen merge
      iconCreateFunction: (cluster) =>
        L.divIcon({
          html: `<span>${cluster.getChildCount()}</span>`,
          className: 'photo-cluster',
          iconSize: [40, 40],
        }),
    })
    layer.addLayers(
      photos.map((p) => {
        const marker = L.marker([p.lat, p.lon], {
          icon: L.divIcon({
            html: p.thumb_key ? `<img src="${photoUrl.thumb(p.id)}" alt="" loading="lazy">` : '',
            className: 'photo-marker',
            iconSize: [40, 40],
          }),
          title: p.original_filename ?? '',
        })
        marker.bindPopup(() => popup(p), { minWidth: 200 })
        marker.on('click', () => onSelect(p.id))
        return marker
      }),
    )
    map.addLayer(layer)
    return () => {
      map.removeLayer(layer)
    }
  }, [map, photos, onSelect])
  return null
}

// Built as DOM nodes, not an HTML string, so a filename or place can't inject markup.
function popup(p: Located) {
  const box = document.createElement('div')
  box.className = 'photo-popup'
  if (p.web_key) {
    const link = document.createElement('a')
    link.href = photoUrl.web(p.id)
    link.target = '_blank'
    const img = document.createElement('img')
    img.src = photoUrl.thumb(p.id)
    img.alt = p.original_filename ?? ''
    link.append(img)
    box.append(link)
  }
  const lines = [
    p.local_time ? `${p.local_time.slice(0, 10)} ${p.local_time.slice(11, 16)}` : 'No date',
    [p.place_name, p.country_name].filter(Boolean).join(', '),
    p.original_filename ?? '',
  ]
  for (const text of lines.filter(Boolean)) {
    const line = document.createElement('div')
    line.textContent = text
    box.append(line)
  }
  return box
}
