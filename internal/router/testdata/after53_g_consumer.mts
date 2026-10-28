/** Actual independent Web API modules against the owned Go HTTP listener. */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
const root = process.env.MULTIRAG_AFTER53_G_WEB_CHECKOUT!
const load = (name: string) => import(pathToFileURL(`${root}/src/api/${name}.ts`).href)
const { apiClient } = await load('client')
const { knowledgeRestConfig } = await load('knowledge-config')
const { knowledgeIngestionAPI } = await load('knowledge-ingestions')
const { resolveDocumentImageUrl } = await load('document-images')
const base = process.env.AFTER53_G_HTTP!
const kb = process.env.AFTER53_G_KB!
knowledgeRestConfig.baseURL = `${base}/api`
apiClient.setAuthToken(process.env.AFTER53_G_JWT!)
assert.equal((await knowledgeIngestionAPI.getSummary(kb)).status.unstart_count, 4)
const datasetLogs = await knowledgeIngestionAPI.listDatasetLogs({ kb_id: kb, page_size: 100, desc: false })
assert.deepEqual(datasetLogs.logs.map((row: any) => row.id), ['log-0', 'log-1', 'log-2'])
const file = await knowledgeIngestionAPI.listFileLogs({ kb_id: kb, types: ['txt'], suffix: ['txt'] })
assert.equal(file.total, 1)
assert.equal(file.logs[0].document_id, 'doc')
assert.equal((await knowledgeIngestionAPI.get(kb, 'file')).dsl.secret, '[REDACTED]')
const selected = await knowledgeIngestionAPI.listDatasetLogs({ kb_id: kb, operation_status: ['0'] })
assert.equal(selected.total, 1)
const imageUrl = resolveDocumentImageUrl({ kind: 'dataset', imageId: `${kb}-folder/a-b image.png` })
const response = await fetch(imageUrl, { headers: { Authorization: `Bearer ${process.env.AFTER53_G_JWT}` } })
assert.equal(response.status, 200)
assert.equal(response.headers.get('content-type'), 'image/png')
assert.equal(response.headers.get('cache-control'), 'no-store')
assert.ok((await response.arrayBuffer()).byteLength > 0)
console.log('Actual Web ingestion API and image URL/fetch passed; browser native decoding not exercised')
