/** Frozen sibling Web client against the owned deletion API; no response mocks. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const root = process.env.WEB_SESSION_CLEANUP_CHECKOUT
const base = process.env.SESSION_CLEANUP_BASE
const session = process.env.SESSION_CLEANUP_ID
const chat = process.env.SESSION_CLEANUP_CHAT
const webSha = process.env.SESSION_CLEANUP_WEB_SHA
const token = process.env.SESSION_CLEANUP_TOKEN
assert.ok(root && base && session && chat && webSha && token)
const { apiClient } = await import(pathToFileURL(`${root}/src/api/client.ts`).href)
const { conversationAPI } = await import(pathToFileURL(`${root}/src/api/conversation.ts`).href)
const originalFetch = globalThis.fetch
const requests: { path: string; method: string; body: unknown; status: number; response: unknown }[] = []
globalThis.fetch = async (input, options) => {
  const url = new URL(String(input))
  assert.equal(url.pathname, `/api/v1/chats/${chat}/sessions`)
  assert.equal(new Headers(options?.headers).get('Authorization'), `Bearer ${token}`)
  const response = await originalFetch(`${base}${url.pathname}`, options)
  requests.push({ path: url.pathname, method: options?.method || '', body: JSON.parse(String(options?.body)), status: response.status, response: await response.clone().json() })
  return response
}
try {
  apiClient.setAuthToken(token)
  const result = await conversationAPI.removeConversations(chat, [session, session])
  const expected = { success_count: 1, errors: [`Duplicate session ids: ${session}`] }
  assert.deepEqual(result, expected)
  assert.equal(requests.length, 1)
  assert.equal(requests[0].method, 'DELETE')
  assert.equal(requests[0].status, 200)
  assert.deepEqual(requests[0].body, { ids: [session, session] })
  assert.deepEqual(requests[0].response, { code: 0, data: expected })
  console.log(JSON.stringify({ passed: true, webSha, client: 'conversationAPI.removeConversations', requests }))
} finally {
  apiClient.setAuthToken(null)
  globalThis.fetch = originalFetch
}
