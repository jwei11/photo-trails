# How it works

## How a photo gets its time

"When was this taken" is two questions: the **instant** (an absolute UTC moment, used for ordering) and the **local zone** (what the wall clock said there, used for display). Cameras record neither reliably: `DateTimeOriginal` is a wall-clock reading with no zone.

The **zone** comes from, in order:
1. The GPS coordinates, via an offline timezone-boundary lookup (`timezonefinder`).
2. The nearest photo in time that has coordinates, if one is within 48 hours. This is how a no-GPS camera shooting alongside a phone gets placed.
3. The offset the camera recorded (`OffsetTimeOriginal`), as a fixed offset.
4. `DEFAULT_TIMEZONE`, flagged in the UI as a guess.

The **instant** comes from the best available source, and `time_source` records which one:
1. `exif_offset`: camera time minus the offset the camera recorded (most phones write this).
2. `gps`: GPS satellite time.
3. `coordinates` / `neighbour` / `default_zone`: camera time interpreted in the zone found above.

These are independent. A camera left on New York time in Lisbon records `09:00 -04:00`: the instant is right (13:00Z), and it displays as 14:00 in Europe/Lisbon because the zone comes from the GPS.

**Clock drift.** A wrong camera clock is corrected with a per-camera offset (make + model + serial), optionally limited to a range of camera-clock times. The raw `camera_time` is never modified; `shot_at` is recomputed from the raw values plus the offsets, so corrections are reversible.

The logic lives in `app/timeresolve.py` as pure functions with no database or files, and is covered by `tests/test_timeresolve.py`.

## How photos become trips

Dated photos are walked in time order, and a new trip starts when:
- photos are more than **48 hours** apart, or
- they're more than **300 km** apart **and** more than **24 hours** apart, or
- a photo taken **at home** falls between them.

"At home" means within the radius (default 25 km) of any home you've set. A photo without GPS takes the at-home status of the nearest photo in time. Home photos, undated photos, and groups of fewer than **3 photos** aren't trips; they're shown as "Not in a trip", not deleted.

The algorithm is sometimes wrong, so fixes are one click: rename, merge, split at a photo, remove a photo, or "not a trip". Any edited trip becomes **locked**. Re-running the algorithm (after every upload, clock correction, or home change) only regroups photos outside locked trips, so manual work is never undone. A late photo that falls inside a locked trip's time span (say a friend's, imported later) joins it. Photos removed by hand are pinned out and never regrouped.

Trip IDs are stable across re-runs: a regrouped set of photos keeps the ID that most of its photos already had. Share links (M11) will depend on this.

The logic is in `app/trips.py` (pure functions, `tests/test_trips.py`), and the edit operations in `app/trip_store.py` (`tests/test_trips_api.py`).
