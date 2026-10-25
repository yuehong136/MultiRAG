/** Fixed, installed Web API clients against the owned Python HTTP listener. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const root = process.env.WEB_COMPLETION_CHECKOUT
const base = process.env.AFTER4776_WEB_BASE
const token = process.env.AFTER4776_WEB_TOKEN
const mode = process.env.AFTER4776_WEB_MODE
assert.ok(root && base && token && mode)
Object.defineProperty(globalThis, 'localStorage', {
  value: { getItem: () => token, setItem: () => {}, removeItem: () => {} },
  configurable: true,
})
const { apiClient } = await import(pathToFileURL(`${root}/src/api/client.ts`).href)
apiClient.setAuthToken(token)
const originalFetch = globalThis.fetch
const requests: Array<{ path: string; method: string }> = []
globalThis.fetch = (url, init) => {
  const parsed = new URL(String(url))
  requests.push({ path: parsed.pathname, method: init?.method || 'GET' })
  return originalFetch(`${base}${parsed.pathname}${parsed.search}`, init)
}
try {
  if (mode === 'agent') {
    const { agentAPI } = await import(pathToFileURL(`${root}/src/api/agent.ts`).href)
    const created = await agentAPI.setAgent({
      title: 'Web label consumer', canvas_type: 'Marketing',
      dsl: JSON.parse(process.env.AFTER4776_WEB_DSL!),
    })
    const id = created.id
    assert.ok(id)
    for (const [patch, expected] of [
      [{ canvas_type: 'Finance' }, 'Finance'],
      [{}, 'Finance'],
      [{ canvas_type: '' }, ''],
    ] as const) {
      await agentAPI.setAgent({ id, title: 'Web label consumer', ...patch })
      const saved = await agentAPI.fetchAgent(id)
      assert.equal(saved.canvas_type, expected)
      assert.equal(saved.canvas_category, 'agent_canvas')
    }
    console.log(JSON.stringify({ passed: true, mode, id, requests }))
  } else {
    assert.equal(mode, 'dataset')
    const id = process.env.AFTER4776_WEB_DATASET!
    const { knowledgeAPI } = await import(pathToFileURL(`${root}/src/api/knowledge.ts`).href)
    const { getDefaultFormValues } = await import(pathToFileURL(`${root}/src/types/knowledge-form.ts`).href)
    assert.deepEqual(getDefaultFormValues().parser_config?.built_in_metadata, [])
    const api = knowledgeAPI.knowledgeBase
    const before = await api.get(id)
    await api.update({ kb_id: id, avatar: '', parser_config: { chunk_token_num: 256 } })
    const retained = await api.get(id)
    assert.equal(retained.avatar, '')
    assert.equal(retained.parser_config.chunk_token_num, 256)
    assert.deepEqual(retained.parser_config.built_in_metadata, before.parser_config.built_in_metadata)
    assert.deepEqual(retained.parser_config.extension, { preserve: false, count: 0 })
    await api.update({ kb_id: id, parser_config: { built_in_metadata: [] } })
    const cleared = await api.get(id)
    assert.deepEqual(cleared.parser_config.built_in_metadata, [])
    assert.deepEqual(cleared.parser_config.extension, retained.parser_config.extension)
    assert.equal(cleared.parser_config.chunk_token_num, 256)
    console.log(JSON.stringify({ passed: true, mode, id, requests }))
  }
} finally {
  globalThis.fetch = originalFetch
}
