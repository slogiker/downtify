<template>
  <Teleport to="body">
    <div
      class="fixed inset-0 z-[60] flex items-end justify-center bg-black/60 p-0 sm:items-center sm:p-6"
      role="dialog"
      aria-modal="true"
      aria-label="Jam"
      @click.self="$emit('close')"
    >
      <div class="w-full max-w-md rounded-t-3xl bg-[#111318] p-5 text-white shadow-2xl sm:rounded-3xl">
        <header class="mb-4 flex items-center justify-between">
          <h2 class="text-base font-semibold">Jam</h2>
          <button
            type="button"
            class="flex size-9 items-center justify-center rounded-full bg-white/10 hover:bg-white/20"
            aria-label="Close"
            @click="$emit('close')"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </header>

        <p v-if="error" class="mb-3 rounded-xl bg-red-500/15 px-3 py-2 text-sm text-red-200">
          {{ error }}
        </p>

        <!-- No room yet: start one, or type the six letters someone read out. -->
        <template v-if="!session">
          <p class="mb-4 text-sm text-white/60">
            Everyone in a jam hears the same second. One person hosts; the
            others follow and can still skip or pause.
          </p>
          <button
            type="button"
            class="mb-4 w-full rounded-xl bg-white px-4 py-3 text-sm font-semibold text-black disabled:opacity-40"
            :disabled="busy || !player.currentTrack.value"
            @click="jam.start()"
          >
            Start a jam with what's playing
          </button>
          <div class="flex gap-2">
            <input
              v-model="code"
              class="min-w-0 flex-1 rounded-xl bg-white/10 px-3 py-3 text-center font-mono text-lg uppercase tracking-[0.3em] placeholder:text-white/30"
              maxlength="8"
              placeholder="CODE"
              aria-label="Jam code"
              @keyup.enter="jam.join(code)"
            />
            <button
              type="button"
              class="rounded-xl bg-white/15 px-4 py-3 text-sm font-semibold disabled:opacity-40"
              :disabled="busy || code.trim().length < 4"
              @click="jam.join(code)"
            >
              Join
            </button>
          </div>
        </template>

        <!-- In a room. -->
        <template v-else>
          <div class="mb-4 rounded-2xl bg-white/5 p-4 text-center">
            <p class="text-xs uppercase tracking-widest text-white/40">Code</p>
            <p class="font-mono text-3xl font-semibold tracking-[0.3em]">{{ session.code }}</p>
            <p class="mt-1 text-xs text-white/50">
              {{ session.owner ? 'You are hosting' : 'You are following' }}
            </p>
          </div>

          <ul class="mb-4 space-y-1 text-sm">
            <li
              v-for="member in session.members"
              :key="member.device_id"
              class="flex items-center justify-between rounded-lg px-2 py-1.5"
              :class="member.me ? 'bg-white/10' : ''"
            >
              <span class="truncate">{{ member.device_name || member.device_id }}</span>
              <span v-if="member.me" class="text-xs text-white/40">this one</span>
            </li>
          </ul>

          <p class="mb-4 text-sm text-white/60">
            <template v-if="session.state?.track_id">
              Shared now: {{ stateLine }}
            </template>
            <template v-else>
              Nothing shared yet — press “Sync everyone”.
            </template>
          </p>

          <label class="mb-4 flex items-center gap-3 text-sm">
            <input v-model="jam.following.value" type="checkbox" class="size-4" />
            <span>Follow the room's position</span>
          </label>

          <div class="flex gap-2">
            <button
              v-if="session.owner"
              type="button"
              class="flex-1 rounded-xl bg-white px-4 py-3 text-sm font-semibold text-black disabled:opacity-40"
              :disabled="busy"
              @click="jam.syncAll()"
            >
              Sync everyone
            </button>
            <button
              type="button"
              class="flex-1 rounded-xl bg-white/15 px-4 py-3 text-sm font-semibold disabled:opacity-40"
              :disabled="busy"
              @click="leave"
            >
              {{ session.owner ? 'End the jam' : 'Leave' }}
            </button>
          </div>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { useJam } from '/src/model/jam'
import { usePlayer } from '/src/model/player'

defineEmits(['close'])

const jam = useJam()
const player = usePlayer()
const code = ref('')

const stateLine = computed(() => {
  const state = jam.session.value?.state
  if (!state?.track_id) return ''
  const title = player.currentTrack.value?.id === state.track_id
    ? player.currentTrack.value?.title
    : state.track_id
  const seconds = Math.max(0, Math.round(state.position || 0))
  const clock = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
  return `${title || state.track_id} · ${clock}${state.playing ? '' : ' (paused)'}`
})

async function leave() {
  await jam.leave()
}

onMounted(() => {
  jam.open()
})

onBeforeUnmount(() => {
  jam.stopPolling()
})
</script>
