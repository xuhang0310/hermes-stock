import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const shellSource = await readFile(new URL('../src/components/AppShell.vue', import.meta.url), 'utf8')
const appSource = await readFile(new URL('../src/App.vue', import.meta.url), 'utf8')

test('settings navigation is visible only to admin roles', () => {
  assert.match(
    shellSource,
    /const canViewSettings = computed\([\s\S]*\['admin', 'super_admin'\]\.includes\(props\.user\.role\)/
  )
  assert.match(
    shellSource,
    /\.\.\.\(canViewSettings\.value \? \[\{ key: 'settings'(?: as PageKey)?, label: '[^']+' \}\] : \[\]\)/
  )
})

test('non-admin users cannot activate or render the settings page', () => {
  assert.match(appSource, /const canViewSettings = computed\([\s\S]*\['admin', 'super_admin'\]/)
  assert.match(appSource, /if \(page === 'settings' && !canViewSettings\.value\) return/)
  assert.match(appSource, /activePage === 'settings' && canViewSettings/)
})
