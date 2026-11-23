import { ref } from 'vue'
import { useLibrary } from '/src/model/library'
import { usePlayer } from '/src/model/player'

/**
 * The jam, on the web player.
 *
 * A jam is one shared playback state per account: a host device publishes
 * `{track_id, position, playing, queue}`, everyone else adopts it. The server
 * hands out a six-letter code, members are devices, and every accepted write
 * raises `revision` - a stale write is refused with the current state, so two
 * people pressing play at once converge instead of fighting.
 *
 * The browser is not a paired device, so the server gives it its own stable
 * id from the session cookie (see `_playback_identity`); that is what makes
 * `owner` mean "this browser started the room".
 */

const session = ref(null)
const busy = ref(false)
const error = ref('')
const following = ref(true)
//: What the account's other devices played last (the handoff).
const handoff = ref([])

let timer = null
let appliedRevision = -1

async function request(method, path, body) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  let data = {}
  try {
    data = await res.json()
  } catch {
    data = {}
  }
  if (!res.ok) {
    const detail = data?.detail
    const text =
      typeof detail === 'string'
        ? detail
        : (detail?.error ?? `HTTP ${res.status}`)
    throw new Error(text)
  }
  return data
}

function localQueue(player) {
  const track = player.currentTrack.value
  if (!track) return []
  const ids = [track.id, ...(player.upcoming.value ?? []).map((t) => t.id)]
  return [...new Set(ids.filter(Boolean))]
}

export function useJam() {
  const player = usePlayer()
  const library = useLibrary()

  function snapshot() {
    return {
      track_id: player.currentTrack.value?.id ?? '',
      position: player.currentTime.value || 0,
      playing: !!player.isPlaying.value,
      queue: localQueue(player),
    }
  }

  /**
   * Adopt the room's state. The song is only swapped when it is already in
   * this player's queue - the web player streams what the library handed it,
   * so a track id it has never seen has no file to play; the panel says so
   * instead of pretending.
   */
  function adopt(state) {
    if (!state?.track_id) return
    if (!following.value) return
    const mine = player.currentTrack.value?.id
    if (mine && mine !== state.track_id) return
    const expected = state.position + (state.playing ? (Date.now() - state.updated_at * 1000) / 1000 : 0)
    const drift = Math.abs((player.currentTime.value || 0) - expected)
    if (drift > 1.0) player.seek(Math.max(0, expected))
    if (state.playing && !player.isPlaying.value) player.play()
    if (!state.playing && player.isPlaying.value) player.pause()
  }

  /** Where the other devices stopped - "continue on the PC where the phone was". */
  async function loadHandoff() {
    try {
      const data = await request('GET', '/api/v1/playback/handoff')
      handoff.value = data.devices ?? []
    } catch {
      handoff.value = []
    }
  }

  /** Take over another device's track here, at its position. */
  function continueHere(entry) {
    const item = (library.tracks.value ?? []).find((t) => t.id === entry.track_id)
    if (!item) {
      error.value =
        'That track is not on the library page this session loaded - open Library first, then try again.'
      return false
    }
    error.value = ''
    player.playList([item], { context: 'handoff' })
    window.setTimeout(() => {
      player.seek(entry.position || 0)
      if (entry.playing) player.play()
    }, 300)
    return true
  }

  async function refresh() {
    const data = await request('GET', '/api/v1/playback/jam')
    session.value = data.session ?? null
    if (session.value?.state && session.value.state.revision > appliedRevision) {
      appliedRevision = session.value.state.revision
      if (!session.value.owner) adopt(session.value.state)
    }
  }

  async function start() {
    busy.value = true
    error.value = ''
    try {
      session.value = await request('POST', '/api/v1/playback/jam', snapshot())
      appliedRevision = session.value.state?.revision ?? -1
      startPolling()
    } catch (e) {
      error.value = e.message
    } finally {
      busy.value = false
    }
  }

  async function join(code) {
    busy.value = true
    error.value = ''
    try {
      session.value = await request('POST', '/api/v1/playback/jam/join', {
        code: String(code || '').trim().toUpperCase(),
      })
      appliedRevision = -1
      await refresh()
      startPolling()
    } catch (e) {
      error.value = e.message
    } finally {
      busy.value = false
    }
  }

  /** The host's "sync everyone": push this player's position and queue. */
  async function syncAll() {
    if (!session.value) return
    busy.value = true
    error.value = ''
    try {
      const state = await request('PUT', '/api/v1/playback/jam/state', {
        ...snapshot(),
        revision: session.value.state?.revision,
      })
      if (state.state) {
        session.value = { ...session.value, state: state.state }
        appliedRevision = state.state.revision
      }
    } catch (e) {
      error.value = e.message
      await refresh().catch(() => {})
    } finally {
      busy.value = false
    }
  }

  async function leave() {
    stopPolling()
    try {
      await request('DELETE', '/api/v1/playback/jam', {})
    } catch (e) {
      error.value = e.message
    }
    session.value = null
    appliedRevision = -1
  }

  function startPolling() {
    if (timer) return
    timer = setInterval(() => {
      refresh().catch(() => {})
    }, 2500)
  }

  function stopPolling() {
    if (timer) clearInterval(timer)
    timer = null
  }

  /** Called once the panel is on screen. */
  async function open() {
    error.value = ''
    try {
      await Promise.all([refresh(), loadHandoff()])
      if (session.value) startPolling()
    } catch (e) {
      error.value = e.message
    }
  }

  return {
    session,
    handoff,
    loadHandoff,
    continueHere,
    busy,
    error,
    following,
    open,
    start,
    join,
    leave,
    syncAll,
    refresh,
    stopPolling,
  }
}
