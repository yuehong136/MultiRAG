/** Actual accepted Web metadata editor conversion and HTTP domain client. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const descriptor = JSON.parse(readFileSync(process.env.I_METADATA_WEB_DESCRIPTOR!, 'utf8'))
const phase = process.env.I_METADATA_WEB_PHASE
const root = descriptor.checkout
const { apiClient } = await import(pathToFileURL(`${root}/src/api/client.ts`).href)
const { knowledgeMetadataAPI } = await import(pathToFileURL(`${root}/src/api/knowledge-metadata.ts`).href)
const {
  metadataConfigToFields, metadataFieldsToConfig, tableDataToSettings,
  settingsToTableData, InvalidMetadataNumberError,
} = await import(pathToFileURL(`${root}/src/lib/metadata-config.ts`).href)
const originalFetch = globalThis.fetch
const responses: Array<{ method: string; status: number; code: number }> = []
globalThis.fetch = async (input, options) => {
  const url = new URL(String(input), 'http://web.invalid')
  assert.equal(url.pathname, `/api/v1/datasets/${descriptor.dataset}/metadata/config`)
  const response = await originalFetch(descriptor.origin + url.pathname + url.search, options)
  const body = await response.clone().json()
  responses.push({ method: options?.method ?? 'GET', status: response.status, code: body.code })
  return response
}
try {
  apiClient.setAuthToken(readFileSync(phase === 'owner' ? descriptor.ownerTokenFile : descriptor.foreignTokenFile, 'utf8'))
  if (phase === 'owner') {
    const initial = await knowledgeMetadataAPI.getConfig(descriptor.dataset)
    assert.deepEqual(initial.metadata, descriptor.schema)
    const rows = settingsToTableData(metadataConfigToFields(initial.metadata))
    assert.deepEqual(rows.map((row: { valueType: string }) => row.valueType), ['number', 'list', 'time'])
    const before = responses.length
    for (const bad of ['abc', '', ' ', 'Infinity', '1e309']) {
      const invalid = structuredClone(rows)
      invalid[1].values = [bad]
      assert.throws(() => metadataFieldsToConfig(tableDataToSettings(invalid), initial.metadata), InvalidMetadataNumberError)
    }
    assert.equal(responses.length, before, 'Invalid metadata sent HTTP')
    rows[0].description = 'Edited count'
    rows[1].values = ['0', '2.5']
    const metadata = metadataFieldsToConfig(tableDataToSettings(rows), initial.metadata)
    await knowledgeMetadataAPI.updateKBSettings({ kb_id: descriptor.dataset, metadata })
    const reloaded = await knowledgeMetadataAPI.getConfig(descriptor.dataset)
    assert.deepEqual(reloaded.metadata, metadata)
    assert.deepEqual(reloaded.metadata.properties.scores.items.enum, [0, 2.5])
    assert.equal(reloaded.metadata.properties.count.type, 'integer')
    assert.equal(reloaded.metadata.properties.timestamp.type, 'time')
    assert.deepEqual(reloaded.metadata.required, ['scores'])
    assert.equal(reloaded.metadata.additionalProperties, false)
    assert.ok(responses.every((row) => row.status === 200 && row.code === 0))
    console.log(JSON.stringify({ phase, status: 'passed', responses, metadata }))
  } else {
    assert.equal(phase, 'foreign')
    await assert.rejects(() => knowledgeMetadataAPI.getConfig(descriptor.dataset))
    await assert.rejects(() => knowledgeMetadataAPI.updateKBSettings({ kb_id: descriptor.dataset, metadata: descriptor.schema }))
    assert.equal(responses.length, 2)
    assert.ok(responses.every((row) => row.code !== 0))
    console.log(JSON.stringify({ phase, status: 'passed', responses }))
  }
} finally {
  globalThis.fetch = originalFetch
}
