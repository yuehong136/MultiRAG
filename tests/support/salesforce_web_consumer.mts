/** Actual pinned Web domain client; SQL and cleanup belong to the Python fixture. */
import assert from 'node:assert/strict'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const checkout = resolve(process.argv[2])
const origin = new URL(process.argv[3])
assert.equal(origin.protocol, 'http:')
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname))
assert.equal(origin.pathname, '/')
assert.equal(origin.username + origin.password + origin.search + origin.hash, '')
const token = process.env.TEST_TENANT_TOKEN
assert.ok(token)
const { apiClient } = await import(pathToFileURL(`${checkout}/src/api/client.ts`).href)
const { createDatasourceConnectorAPI } = await import(pathToFileURL(`${checkout}/src/api/datasource.ts`).href)
const api = createDatasourceConnectorAPI(origin.origin)
const originalFetch = globalThis.fetch
const requests: Array<{ method: string; path: string }> = []
const created: Array<{ id: string; name: string }> = []
globalThis.fetch = async (input, options) => {
  const target = new URL(input instanceof Request ? input.url : String(input))
  assert.equal(target.origin, origin.origin)
  assert.match(target.pathname, /^\/api\/v1\/connectors(?:\/[a-zA-Z0-9_-]+)?$/)
  assert.equal(new Headers(options?.headers).get('Authorization'), `Bearer ${token}`)
  const method = options?.method ?? 'GET'
  assert.ok(method === 'POST' || method === 'GET')
  requests.push({ method, path: target.pathname })
  return originalFetch(input, { ...options, redirect: 'error' })
}
try {
  apiClient.setAuthToken(token)
  for (const [suffix, objects] of [['default', undefined], ['none', []], ['selected', ['Account', 'Contact']]] as const) {
    const config = {
      batch_size: 100,
      sync_deleted_files: false,
      credentials: { instance_url: 'https://owned.my.salesforce.com', client_id: 'controlled-client', client_secret: 'controlled-salesforce-client-secret' },
      ...(objects === undefined ? {} : { objects: [...objects] }),
    }
    const name = `after98-w-${suffix}`
    const saved = await api.set({ name, source: 'salesforce', config, refresh_freq: 0, prune_freq: 0 })
    assert.ok(typeof saved.id === 'string' && saved.id)
    created.push({ id: saved.id, name })
    const readback = await api.get(saved.id)
    assert.equal(readback.name, name)
    assert.equal(readback.source, 'salesforce')
    if (objects === undefined) {
      assert.equal(Object.hasOwn(readback.config, 'objects'), false)
    } else {
      assert.deepEqual(readback.config.objects, [...objects])
    }
  }
  assert.equal(new Set(created.map(row => row.id)).size, 3)
  assert.equal(requests.filter(row => row.method === 'POST').length, 3)
  assert.equal(requests.filter(row => row.method === 'GET').length, 3)
  console.log(JSON.stringify({ passed: true, created, requests }))
} finally {
  apiClient.setAuthToken(null)
  globalThis.fetch = originalFetch
}
