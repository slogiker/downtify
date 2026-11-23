# Jam on 3.14 — what was ported, what was broken, what is not proven

The server the NAS runs is upstream **3.14.0** (the image `downtify:jam-3.14`, built from this
branch). The jam and the device handoff were written against the 3.10-era `main`; this branch
carries them forward and fixes three defects that driving the real API exposed.

## The port

Branch `feat/jam-3.14` = `3.14.0` + four cherry-picks (playback sessions, the mobile contract doc,
`/beta` hosting, the beta error reports). Conflicts were three and all were "both sides added
lines": the capabilities block, two auth rule blocks, the router list. Nothing was dropped.

`PlaybackStateStore` and `JamStore` still open the library database through `connect_sqlite(path)`,
so no store rewrite was needed — `main.py:_open_library_stores` hands them the same `library_db`.

## The three defects

1. **A guest leaving ended the room.** `leave()` compared the *account* with `owner_user_id`, and a
   jam's members are devices: the host's own second phone closed the session for everyone. Ownership
   is now `owner_device_id`, with the account as the fallback for rows written before the column.
2. **A guest's payload said `owner: true`.** Same cause, same fix, in `_jam_payload`.
3. **Starting a jam ignored the state sent with it.** Every session opened on a blank frame and the
   host had to publish a second time. `POST /api/v1/playback/jam` now accepts
   `{track_id, position, playing, queue}` and writes it as revision 1.

## The web player

`/api/v1/playback` is open to a signed-in **user**, not just a paired device (the rest of `/api/v1`
stays device-only). A browser session has no device id, so `_playback_identity` derives a stable
`web-<hash>` from the session cookie — two tabs are two participants instead of one row overwriting
itself.

UI: `frontend/src/model/jam.js` (API + polling follow) and
`frontend/src/components/player/JamPanel.vue`, opened by a **JAM** button in Now Playing. Start with
what is playing, join by code, member list, *Sync everyone* for the host, a follow toggle, leave or
end. The panel polls the session every 2.5 s and adopts the room's position (extrapolated from
`updated_at`) when the shared track is the one already playing; the web player streams what the
library handed it, so a track id it has never seen is reported instead of guessed at.

## Verified

- `docker build` of the tree, then a scratch instance on port 4535 with a fresh `/data`:
  `capabilities` has `jam`, `playback_handoff`, `playlist_delete`; the startup log is clean.
- Two paired devices: report → handoff carries track/position/queue; start → code, revision 1 with
  the state that was sent; join (second device `owner: false`); stale write → `409` with the current
  state; retry on the new revision → applied; member leave → `ended: false`, the host keeps the
  session; owner leave → session gone for the other device; a device on `/api/settings` → `403`.
- A cookie session (the browser's way in) does the same: `GET/POST/DELETE /api/v1/playback/jam`
  answer `200` with a `web-…` device member.
- The Vite build ships the panel: `Sync everyone` and `playback/jam` are in
  `assets/NowPlaying-*.js`, and the NAS serves that build.

## Not proven

- **Two phones and a browser in the same room.** No device or browser automation here: the frames,
  the drift correction and the audible result are still the one claim this work cannot make. That is
  the first thing to do with `0.6.0-beta.14` against `music.slogiker.si`.
- The web follow path only seeks when the room's track is already loaded; jumping the web player to
  an arbitrary track id needs the library row, which is the next piece of work.
