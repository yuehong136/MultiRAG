/** Execute the installed Web serializer and transport against owned real routes. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

let raw = ''
for await (const chunk of process.stdin) raw += chunk
const input = JSON.parse(raw)
const root = process.env.WEB_COMPLETION_CHECKOUT
assert.ok(root)
const load = (path: string) => import(pathToFileURL(`${root}/${path}`).href)
const { apiClient } = await load('src/api/client.ts')
const { knowledgeDocumentAPI } = await load('src/api/knowledge-documents.ts')
apiClient.setAuthToken(input.token)
const nativeFetch = globalThis.fetch
const receipts: Array<{ method: string; path: string; status: number; code?: number }> = []
globalThis.fetch = async (url, options) => {
  const parsed = new URL(String(url))
  const response = await nativeFetch(input.base + parsed.pathname + parsed.search, options)
  const body = await response.clone().json()
  receipts.push({ method: options?.method || 'GET', path: parsed.pathname, status: response.status, code: body.code })
  return response
}
try {
  assert.equal(await knowledgeDocumentAPI.createChunk({ kb_id: input.kb, doc_id: input.doc, content_with_weight: 'actual-web-image', image_base64: input.image }), true)
  assert.equal(await knowledgeDocumentAPI.createChunk({ kb_id: input.kb, doc_id: input.doc, content_with_weight: 'actual-web-text' }), true)
  await assert.rejects(() => knowledgeDocumentAPI.createChunk({ kb_id: input.kb, doc_id: input.doc, content_with_weight: 'actual-web-rejected', image_base64: '%%%' }))
  assert.deepEqual(receipts.map((receipt) => receipt.code), [0, 0, 102])
  console.log(JSON.stringify({ passed: true, receipts }))
} finally {
  globalThis.fetch = nativeFetch
}
