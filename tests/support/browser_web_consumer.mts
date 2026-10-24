/** Real typed Web Browser DSL and API clients against an owned backend. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const root = process.env.WEB_COMPLETION_CHECKOUT
const base = process.env.BROWSER_CONSUMER_BASE
const token = process.env.BROWSER_CONSUMER_TOKEN
assert.ok(root && base && token)
Object.defineProperty(globalThis, 'localStorage', {
  value: { getItem: () => token, setItem: () => {}, removeItem: () => {} },
  configurable: true,
})
const load = (path: string) => import(pathToFileURL(`${root}/${path}`).href)
const { agentAPI } = await load('src/api/agent.ts')
const { apiClient } = await load('src/api/client.ts')
const { Operator, NodeHandleId } = await load('src/pages/agent/constant/index.ts')
const { buildGraphNode, serializeGraphToDsl, deserializeDslToGraph } = await load('src/pages/agent/operators/serializers.ts')
const { consumeRuntimeStream } = await load('src/pages/agent/features/runtime-workbench/runtime-stream.ts')
const { normalizeRuntimeEvent } = await load('src/pages/agent/features/runtime-workbench/utils.ts')
apiClient.setAuthToken(token)
const originalFetch = globalThis.fetch
const requests: Array<{ path: string; method: string; status: number }> = []
globalThis.fetch = async (url, init) => {
  const parsed = new URL(String(url))
  const response = await originalFetch(`${base}${parsed.pathname}${parsed.search}`, init)
  requests.push({ path: parsed.pathname, method: init?.method || 'GET', status: response.status })
  return response
}
try {
  const browserId = 'Browser:consumer'
  const browser = buildGraphNode(Operator.Browser, {
    id: browserId,
    form: { llm_id: 'browser-consumer@OpenAI', prompts: 'Inspect uploaded source', max_steps: 2,
      temperature: 0, max_retries: 0, headless: false, enable_default_extensions: false,
      chromium_sandbox: false, persist_session: false, upload_sources: [] },
  })
  const begin = buildGraphNode(Operator.Begin, { id: 'begin' })
  const message = buildGraphNode(Operator.Message, { id: 'Message:answer', form: { content: [`{${browserId}@content}`] } })
  const edge = (source: string, target: string) => ({ id: `${source}-${target}`, source, target,
    sourceHandle: NodeHandleId.Start, targetHandle: NodeHandleId.End })
  const graph = { nodes: [begin, browser, message], edges: [edge('begin', browserId), edge(browserId, message.id)] }
  let dsl = serializeGraphToDsl({ graph })
  const created = await agentAPI.setAgent({ title: 'Browser actual Web consumer', dsl })
  assert.ok(created.id)
  const id = created.id
  const upload = await agentAPI.uploadFile(id, new File(['web browser source bytes'], 'source.txt', { type: 'text/plain' }))
  const uploaded = Array.isArray(upload) ? upload : [upload]
  assert.ok(Array.isArray(uploaded) && uploaded.length === 1 && uploaded[0].id)
  browser.data.form.prompts = 'Inspect {sys.query}'
  browser.data.form.upload_sources = ['{sys.file_refs}']
  dsl = serializeGraphToDsl({ graph })
  await agentAPI.setAgent({ id, title: 'Browser actual Web consumer', dsl })
  const saved = await agentAPI.fetchAgent(id)
  const stored = typeof saved.dsl === 'string' ? JSON.parse(saved.dsl) : saved.dsl
  const restored = serializeGraphToDsl({ graph: deserializeDslToGraph(stored).graph })
  const params = restored.components[browserId].obj.params
  assert.equal(params.temperature, 0)
  assert.equal(params.max_retries, 0)
  assert.equal(params.headless, false)
  assert.equal(params.persist_session, false)
  assert.deepEqual(params.upload_sources, ['{sys.file_refs}'])
  assert.equal(params.outputs.downloaded_files.type, 'Array<Object>')
  const debug = await agentAPI.debugNode({ canvas_id: id, component_id: browserId,
    inputs: { 'sys.query': { value: 'uploaded source' }, 'sys.file_refs': { value: uploaded } } })
  assert.equal(debug.content, 'browser consumer complete')
  assert.equal(debug.downloaded_files.length, 1)
  let content = ''
  let sessionId: string | undefined
  let finished = false
  const response = await agentAPI.runAgent({ id, mode: 'draft', query: 'uploaded source', files: uploaded })
  assert.equal(response.status, 200)
  await consumeRuntimeStream(response, (raw: unknown) => {
    const event = normalizeRuntimeEvent(raw)
    assert.ok(!event.errorMessage, event.errorMessage)
    if (event.sessionId) sessionId = event.sessionId
    if (event.event === 'message') content += event.outputContent || ''
    if (event.event === 'workflow_finished' || event.event === 'message_end') finished = true
  })
  assert.ok(finished && sessionId)
  assert.equal(content, 'browser consumer complete')
  const session = await agentAPI.fetchSession(id, sessionId!)
  assert.equal(session.id, sessionId)
  console.log(JSON.stringify({ passed: true, id, sessionId, uploadId: uploaded[0].id,
    debugFiles: debug.downloaded_files, requests, consumers: ['Browser registry/defaults/serializer',
      'agentAPI.setAgent', 'agentAPI.fetchAgent', 'agentAPI.uploadFile', 'agentAPI.debugNode',
      'agentAPI.runAgent', 'consumeRuntimeStream', 'agentAPI.fetchSession'] }))
} finally {
  globalThis.fetch = originalFetch
}
