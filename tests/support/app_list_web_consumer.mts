/** Real sibling Web list clients against the owned HTTP API. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'

const root = process.env.WEB_DATASET_CHECKOUT
const base = process.env.APP_LIST_BASE
const owners = JSON.parse(process.env.APP_LIST_OWNERS || '[]') as string[]
const tokens = JSON.parse(process.env.APP_LIST_TOKENS || '[]') as string[]
assert.ok(root && base && owners.length === 3 && tokens.length === 2)
const { apiClient, APIError } = await import(pathToFileURL(`${root}/src/api/client.ts`).href)
const { dialogAPI } = await import(pathToFileURL(`${root}/src/api/dialog.ts`).href)
const { searchAPI } = await import(pathToFileURL(`${root}/src/api/search.ts`).href)
const originalFetch = globalThis.fetch
const paths: string[] = []
globalThis.fetch = (input, options) => {
  const url = new URL(String(input))
  assert.match(url.pathname, /^\/api\/v1\/(chats|searches)$/)
  paths.push(url.pathname)
  return originalFetch(`${base}${url.pathname}${url.search}`, options)
}
try {
  for (const token of tokens) {
    apiClient.setAuthToken(token)
    const defaultChats = await dialogAPI.listChats({ page: 0, page_size: 0 })
    const defaultSearches = await searchAPI.list({ page: 0, page_size: 0 })
    assert.equal(defaultChats.total, 4)
    assert.equal(defaultSearches.total, 4)
    const params = { owner_ids: [owners[0], owners[0]], page: 2, page_size: 1, orderby: 'name', desc: false }
    const chats = await dialogAPI.listChats(params)
    const searches = await searchAPI.list(params)
    assert.equal(chats.total, 2)
    assert.equal(chats.chats.length, 1)
    assert.equal(chats.chats[0].name, 'Owner App 1')
    assert.equal(searches.total, 2)
    assert.equal(searches.search_apps[0].name, 'Owner App 1')
    for (const call of [
      () => dialogAPI.listChats({ owner_ids: [owners[0], owners[1]] }),
      () => searchAPI.list({ owner_ids: [owners[1]] }),
    ]) {
      await assert.rejects(call, (error: unknown) => error instanceof APIError && error.code === '103')
    }
  }
  console.log(JSON.stringify({ passed: true, credentials: ['jwt', 'api-key'], requests: paths.length, clients: ['dialogAPI.listChats', 'searchAPI.list'], checks: ['default scope', 'repeated owner_ids', 'pagination total', 'foreign owner rejected'] }))
} finally {
  globalThis.fetch = originalFetch
}
