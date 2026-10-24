/** Fixed production Web methods against registered HTTP and owned SQL/doc stores. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const root = process.env.WEB_DATASET_CHECKOUT
const base = process.env.DATASET_CHUNK_WEB_BASE
const token = process.env.DATASET_CHUNK_WEB_TOKEN
const ids = JSON.parse(process.env.DATASET_CHUNK_WEB_IDS || '{}') as Record<string, string>
assert.ok(root && base && token && ids.a && ids.b && ids.foreign)
const { apiClient } = await import(pathToFileURL(`${root}/src/api/client.ts`).href)
const { knowledgeDocumentAPI } = await import(pathToFileURL(`${root}/src/api/knowledge-documents.ts`).href)
const { knowledgeMetadataAPI } = await import(pathToFileURL(`${root}/src/api/knowledge-metadata.ts`).href)
const { knowledgeTagAPI } = await import(pathToFileURL(`${root}/src/api/knowledge-tags.ts`).href)
const originalFetch = globalThis.fetch
const responses: Array<{ method: string; path: string; http: number; code: number }> = []
globalThis.fetch = async (input, options) => {
  const url = new URL(String(input))
  assert.match(url.pathname, /^\/api\/v1\/datasets\//)
  const response = await originalFetch(`${base}${url.pathname}${url.search}`, options)
  const body = await response.clone().json()
  responses.push({ method: options?.method || 'GET', path: url.pathname + url.search, http: response.status, code: body.code })
  return response
}
try {
  apiClient.setAuthToken(token)
  const chunk = await knowledgeDocumentAPI.getChunk({ kb_id: ids.a, doc_id: 'doc-a', chunk_id: 'chunk-a' })
  assert.equal(chunk.chunk_id, 'chunk-a')
  assert.equal(chunk.doc_id, 'doc-a')
  assert.equal(chunk.content_with_weight, 'body a')
  assert.equal(chunk.available_int, 0)
  const metadata = await knowledgeMetadataAPI.getFlattened([ids.a, ids.b])
  assert.deepEqual(metadata.rank, { '0': ['doc-a', 'doc-b'] })
  assert.deepEqual(metadata.enabled, { false: ['doc-a', 'doc-b'] })
  await assert.rejects(() => knowledgeMetadataAPI.getFlattened([ids.a, ids.foreign]))
  await assert.rejects(() => knowledgeDocumentAPI.getChunk({ kb_id: ids.a, doc_id: 'doc-b', chunk_id: 'chunk-a' }))
  await knowledgeTagAPI.delete(ids.a, ['web-remove'])
  assert.deepEqual(responses.map(({ code }) => code), [0, 0, 403, 404, 0])
  console.log(JSON.stringify({ passed: true, responses }))
} finally {
  globalThis.fetch = originalFetch
}
