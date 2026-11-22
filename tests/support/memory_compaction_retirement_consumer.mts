import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

interface Configuration {
  baseURL: string
  token: string
  memoryId: string
  operationId: string
  artifactId: string
  sourceIds: string[]
  ownedScratchConfirmed: true
  mode: 'submit' | 'read-generated' | 'read-retired'
}

const config: Configuration = JSON.parse(readFileSync(process.argv[2], 'utf8'))
assert.equal(config.ownedScratchConfirmed, true)
const origin = new URL(config.baseURL)
assert.equal(origin.protocol, 'http:')
assert.equal(origin.hostname, '127.0.0.1')
const fromWeb = (path: string) => pathToFileURL(resolve(process.cwd(), path)).href
const originalFetch = globalThis.fetch
const basePath = `/api/v1/memories/${encodeURIComponent(config.memoryId)}`
const operationPath = `${basePath}/context-compaction-operations/${encodeURIComponent(config.operationId)}`
const artifactPath = `${basePath}/context-compactions/${encodeURIComponent(config.artifactId)}`
const retirementPath = `${artifactPath}/retirements`
const requests: { method: string; path: string; status: number }[] = []

globalThis.fetch = async (input, options) => {
  const url = new URL(input instanceof Request ? input.url : String(input))
  const method = options?.method ?? (input instanceof Request ? input.method : 'GET')
  assert.equal(url.origin, origin.origin)
  assert.ok(url.pathname.startsWith(basePath + '/'))
  assert.ok(method === 'GET' || (
    config.mode === 'submit' &&
    method === 'POST' &&
    url.pathname === retirementPath &&
    requests.every((request) => request.method !== 'POST')
  ))
  const response = await originalFetch(input, options)
  requests.push({ method, path: url.pathname, status: response.status })
  return response
}

try {
  let receipt
  if (config.mode === 'submit') {
    const { APIClient, APIError } = await import(fromWeb('src/api/client.ts'))
    const { createMemoryCompactionAPI } = await import(fromWeb('src/api/memory-compaction.ts'))
    const { validateCompactionResult } = await import(fromWeb('src/lib/memory-compaction.ts'))
    const { retentionCanConfirm, validateRetentionPreview } = await import(fromWeb('src/lib/memory-compaction-retention.ts'))
    const client = new APIClient(config.baseURL)
    client.setAuthToken(config.token)
    try {
      const api = createMemoryCompactionAPI(config.baseURL, client)
      const initial = await api.operation(config.memoryId, config.operationId)
      validateCompactionResult(initial, config.memoryId, config.operationId)
      assert.equal(initial.artifact.artifact_id, config.artifactId)
      const preview = await api.retentionPreview(config.memoryId, config.artifactId)
      validateRetentionPreview(preview, initial)
      assert.ok(retentionCanConfirm(preview, initial))
      await assert.rejects(
        () => api.retire(config.memoryId, config.artifactId, preview.preview_sha256),
        (error: unknown) => {
          assert.ok(error instanceof APIError)
          assert.equal(error.status, 503)
          assert.deepEqual(error.details, {
            operation_id: config.operationId,
            error_code: 'operation_commit_unknown',
            requires_reconciliation: true,
          })
          return true
        },
      )
      receipt = {
        operationId: config.operationId,
        artifactId: config.artifactId,
        previewSha256: preview.preview_sha256,
        status: 503,
      }
    } finally {
      client.setAuthToken(null)
    }
    assert.deepEqual(requests, [
      { method: 'GET', path: operationPath, status: 200 },
      { method: 'GET', path: `${artifactPath}/retention-preview`, status: 200 },
      { method: 'POST', path: retirementPath, status: 503 },
    ])
  } else if (config.mode === 'read-generated') {
    const { runMemoryCompactionConsumer } = await import(fromWeb('scripts/consumers/memory-compaction.ts'))
    receipt = await runMemoryCompactionConsumer({ ...config, mode: 'read', prefixCount: 1, expectedState: 'generated', exportReference: true })
  } else {
    assert.equal(config.mode, 'read-retired')
    const { runMemoryCompactionRetentionConsumer } = await import(fromWeb('scripts/consumers/memory-compaction-retention.ts'))
    receipt = await runMemoryCompactionRetentionConsumer({ ...config, mode: 'read' })
  }
  assert.equal(receipt.artifactId, config.artifactId)
  assert.equal(receipt.operationId, config.operationId)
  assert.ok(requests.some((request) => request.method === 'GET' && request.path === operationPath && request.status === 200))
  console.log(JSON.stringify({ receipt, requests }))
} finally {
  globalThis.fetch = originalFetch
}
