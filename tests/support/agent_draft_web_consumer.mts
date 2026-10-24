/** Real Web Agent clients and runtime parser against an owned scratch API. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const root = process.env.WEB_COMPLETION_CHECKOUT
const base = process.env.AGENT_DRAFT_BASE
const token = process.env.AGENT_DRAFT_TOKEN
const id = process.env.AGENT_DRAFT_ID
assert.ok(root && base && token && id)
Object.defineProperty(globalThis, 'localStorage', {
  value: { getItem: () => token, setItem: () => {}, removeItem: () => {} },
  configurable: true,
})
const { agentAPI } = await import(pathToFileURL(`${root}/src/api/agent.ts`).href)
const { apiClient } = await import(pathToFileURL(`${root}/src/api/client.ts`).href)
const { consumeRuntimeStream } = await import(pathToFileURL(`${root}/src/pages/agent/features/runtime-workbench/runtime-stream.ts`).href)
const { normalizeRuntimeEvent } = await import(pathToFileURL(`${root}/src/pages/agent/features/runtime-workbench/utils.ts`).href)
apiClient.setAuthToken(token)
const originalFetch = globalThis.fetch
const requests: Array<{ path: string; method: string }> = []
globalThis.fetch = (url, init) => {
  const parsed = new URL(String(url))
  requests.push({ path: parsed.pathname, method: init?.method || 'GET' })
  return originalFetch(`${base}${parsed.pathname}${parsed.search}`, init)
}
try {
  let sessionId: string | undefined
  for (const query of ['Web draft first', 'Web draft continued']) {
    const response = await agentAPI.runAgent({ id, mode: 'draft', session_id: sessionId, query })
    let content = ''
    let received: string | undefined
    let finished = false
    await consumeRuntimeStream(response, (raw: unknown) => {
      const event = normalizeRuntimeEvent(raw)
      assert.ok(!event.errorMessage, event.errorMessage)
      if (event.sessionId) {
        if (received) assert.equal(event.sessionId, received)
        received = event.sessionId
      }
      if (event.event === 'message') content += event.outputContent || ''
      if (event.event === 'workflow_finished' || event.event === 'message_end') finished = true
    })
    assert.ok(finished && received)
    assert.equal(content, 'runtime snapshot')
    if (sessionId) assert.equal(received, sessionId)
    sessionId = received
  }
  const session = await agentAPI.fetchSession(id, sessionId!)
  assert.equal(session.id, sessionId)
  assert.equal(session.source, 'agent')
  assert.deepEqual(session.messages.map((message: { role: string }) => message.role), ['user', 'assistant', 'user', 'assistant'])
  const listed = await agentAPI.fetchSessions(id)
  assert.ok(listed.sessions.some((item: { id: string }) => item.id === sessionId))
  console.log(JSON.stringify({ passed: true, session_id: sessionId, requests, consumers: ['agentAPI.runAgent', 'consumeRuntimeStream', 'normalizeRuntimeEvent', 'agentAPI.fetchSession', 'agentAPI.fetchSessions'] }))
} finally {
  globalThis.fetch = originalFetch
}
