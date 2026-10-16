<script setup>
import Modal from './ui/Modal.vue'
import Btn from './ui/Btn.vue'

const open = defineModel('open', { type: Boolean })
defineProps({ title: String, text: String, confirm: { type: String, default: 'Delete' }, danger: { type: Boolean, default: true }, loading: Boolean })
const emit = defineEmits(['confirm'])
</script>

<template>
  <Modal v-model:open="open" :title="title">
    <p class="text-sm text-fg2">{{ text }}</p>
    <template #footer>
      <Btn variant="ghost" @click="open = false">Cancel</Btn>
      <Btn :variant="danger ? 'danger' : 'primary'" :loading="loading" @click="emit('confirm')">{{ confirm }}</Btn>
    </template>
  </Modal>
</template>
