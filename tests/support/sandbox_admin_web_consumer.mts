/** Actual sibling Admin domain client against the registered scratch SQL routes. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
const root = process.env.WEB_SANDBOX_CHECKOUT!
const base = process.env.SANDBOX_ADMIN_BASE!
const token = process.env.SANDBOX_ADMIN_TOKEN!
const normal = process.env.SANDBOX_NORMAL_TOKEN!
assert.ok(root && base && token && normal)
Object.defineProperty(globalThis, 'window', { configurable: true, value: globalThis })
const values = new Map<string, string>()
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
  getItem: (key: string) => values.get(key) ?? null,
  setItem: (key: string, value: string) => values.set(key, value),
  removeItem: (key: string) => values.delete(key),
} })
const originalFetch = globalThis.fetch
const paths: string[] = []
globalThis.fetch = (input, options) => {
  const url = new URL(String(input), 'http://owned.invalid')
  const path = url.pathname.replace(/^\/admin-api/, '/api/v1/admin')
  assert.ok(path.startsWith('/api/v1/admin/sandbox/'))
  paths.push(`${options?.method ?? 'GET'} ${path}`)
  return originalFetch(`${base}${path}${url.search}`, options)
}
try {
  const { adminAPI, setAdminToken } = await import(pathToFileURL(`${root}/src/api/admin.ts`).href)
  setAdminToken(token)
  const providers = await adminAPI.listSandboxProviders()
  assert.ok(providers.some((item: { id: string; available: boolean }) => item.id === 'local' && item.available))
  const schema = await adminAPI.getSandboxSchema('ssh')
  assert.ok(schema.private_key.multiline && schema.private_key.secret)
  const config = { python_bin: process.env.SANDBOX_PYTHON_BIN!, work_dir: process.env.SANDBOX_LOCAL_ROOT!, timeout: 4, max_artifacts: 0 }
  const saved = await adminAPI.saveSandboxConfig('local', config, true)
  assert.equal(saved.effective_provider_type, 'local')
  assert.equal(saved.config.max_artifacts, 0)
  assert.equal((await adminAPI.getSandboxConfig()).provider_type, 'local')
  const tested = await adminAPI.testSandboxConnection('local', config)
  assert.equal(tested.success, true)
  assert.match(tested.details.stdout, /TEST_PASSED/)
  await assert.rejects(adminAPI.saveSandboxConfig('local', { ...config, timeout: true }, true))
  setAdminToken(normal)
  await assert.rejects(adminAPI.getSandboxConfig())
  await assert.rejects(adminAPI.testSandboxConnection('local', config))
  console.log(JSON.stringify({ passed: true, requests: paths.length, paths, actualLocalProbe: true, ordinaryUserDenied: true, strictBooleanRejected: true }))
} finally { globalThis.fetch = originalFetch }
