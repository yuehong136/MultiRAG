/** Frozen sibling Web client against the owned deletion API; no response mocks. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const root = process.env.WEB_SESSION_CLEANUP_CHECKOUT
const base = process.env.SESSION_CLEANUP_BASE
const session = process.env.SESSION_CLEANUP_ID
assert.ok(root && base && session && process.env.SESSION_CLEANUP_TOKEN)
const { apiClient } = await import(pathToFileURL(`${root}/src/api/client.ts`).href)
const { conversationAPI } = await import(pathToFileURL(`${root}/src/api/conversation.ts`).href)
const originalFetch = globalThis.fetch
const requests: { path: string; method: string; body: unknown; status: number; response: unknown }[] = []
globalThis.fetch = async (input, options) => {
  const url = new URL(String(input))
  assert.equal(url.pathname, '/v1/conversation/rm')
  const response = await originalFetch(`${base}${url.pathname}`, options)
  requests.push({ path: url.pathname, method: options?.method || '', body: JSON.parse(String(options?.body)), status: response.status, response: await response.clone().json() })
  return response
}
try {
  apiClient.setAuthToken(process.env.SESSION_CLEANUP_TOKEN)
  const result = await conversationAPI.removeConversation([session, session])
  assert.equal(result, true)
  assert.equal(requests.length, 1)
  assert.equal(requests[0].method, 'POST')
  assert.deepEqual(requests[0].body, { conversation_ids: [session, session] })
  assert.deepEqual(requests[0].response, { retcode: 0, retmsg: 'success', data: true })
  console.log(JSON.stringify({ passed: true, webSha: '170315a8b7d46b1b752b3f574e41b86738c7c2b3', client: 'conversationAPI.removeConversation', requests }))
} finally {
  globalThis.fetch = originalFetch
}
