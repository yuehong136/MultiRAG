/** Pinned production Web clients against a real, owned HTTP API. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const root = process.env.WEB_DATASET_CHECKOUT
const base = process.env.METADATA_WEB_BASE
const token = process.env.METADATA_WEB_TOKEN
const dataset = process.env.METADATA_WEB_DATASET
const document = process.env.METADATA_WEB_DOCUMENT
assert.ok(root && base && token && dataset && document)
const { apiClient } = await import(pathToFileURL(`${root}/src/api/client.ts`).href)
const { knowledgeDocumentAPI } = await import(pathToFileURL(`${root}/src/api/knowledge-documents.ts`).href)
const originalFetch = globalThis.fetch
const responses: Array<{ path: string; http: number; code: number }> = []
globalThis.fetch = async (input, options) => {
  const url = new URL(String(input))
  assert.match(url.pathname, /^\/api\/v1\/datasets\//)
  const response = await originalFetch(`${base}${url.pathname}${url.search}`, options)
  const body = await response.clone().json()
  responses.push({ path: url.pathname + url.search, http: response.status, code: body.code })
  return response
}
try {
  apiClient.setAuthToken(token)
  for (const [metadata, expected] of [
    [{}, [document]],
    [{ score: 0 }, [document]],
    [{ approved: false }, [document]],
    [{ score: 100 }, []],
    [{ approved: true }, []],
    [{ missing: false }, []],
  ] as const) {
    const result = await knowledgeDocumentAPI.list({ kb_id: dataset, filter_params: { metadata } })
    assert.equal(result.total, expected.length)
    assert.deepEqual(result.docs.map((doc: { id: string }) => doc.id), expected)
  }
  assert.ok(responses.every((response) => response.http === 200 && response.code === 0))
  console.log(JSON.stringify({ passed: true, mode: 'documents', responses }))
} finally {
  globalThis.fetch = originalFetch
}
