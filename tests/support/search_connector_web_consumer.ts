/** Prepared evidence consumer. Execute only inside I's registered HTTP/SQL fixture. */
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { readFileSync } from 'node:fs'

type AuthMode = 'account_key' | 'connection_string' | 'sas_token'
type JsonObject = Record<string, unknown>
type Reservation = { id: string; name: string }

class CheckFailed extends Error {
  constructor(readonly check: string) {
    super('Consumer assertion failed')
  }
}
const check = (condition: unknown, label: string): void => {
  if (!condition) throw new CheckFailed(label)
}
const record = (value: unknown): value is JsonObject =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const required = (name: string): string => {
  const value = process.env[name]
  check(typeof value === 'string' && value.length > 0, `environment.${name}`)
  return value!
}
const readDescriptor = (): JsonObject => {
  try {
    const value: unknown = JSON.parse(
      readFileSync(required('WEB_CONSUMER_DESCRIPTOR'), 'utf8'),
    )
    check(record(value), 'descriptor.object')
    return value as JsonObject
  } catch (error) {
    if (error instanceof CheckFailed) throw error
    throw new CheckFailed('descriptor.private-file-json')
  }
}
const stringField = (descriptor: JsonObject, name: string): string => {
  const value = descriptor[name]
  check(typeof value === 'string' && value.length > 0, `descriptor.${name}`)
  return value as string
}
const numericIds = (values: unknown, name: string): number[] => {
  check(
    Array.isArray(values) &&
      values.length > 0 &&
      values.every((id) => Number.isSafeInteger(id) && id > 0) &&
      new Set(values).size === values.length,
    `descriptor.${name}.numeric-ids`,
  )
  return values as number[]
}
const safe = (event: string, data: JsonObject = {}): void => {
  process.stdout.write(`${JSON.stringify({ event, ...data })}\n`)
}
const canonical = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (record(value))
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  return JSON.stringify(value) ?? 'undefined'
}

async function main(): Promise<void> {
  const descriptor = readDescriptor()
  const webRoot = resolve(
    typeof descriptor.webRoot === 'string' ? descriptor.webRoot :
      '/Users/xldu/.codex/worktrees/after67-w/web',
  )
  const fixture = new URL(stringField(descriptor, 'baseURL'))
  check(
    fixture.protocol === 'http:' &&
      ['127.0.0.1', 'localhost', '[::1]'].includes(fixture.hostname) &&
      !fixture.username &&
      !fixture.password &&
      fixture.pathname === '/' &&
      !fixture.search &&
      !fixture.hash,
    'fixture.explicit-loopback-origin',
  )
  const token = stringField(descriptor, 'token')
  const searchId = stringField(descriptor, 'searchId')
  const teiModel = stringField(descriptor, 'teiModel')
  const expectedModelIds = numericIds(descriptor.modelIds, 'modelIds')
  const forbiddenModelIds = numericIds(descriptor.forbiddenModelIds, 'forbiddenModelIds')
  check(
    forbiddenModelIds.every((id) => !expectedModelIds.includes(id)),
    'fixture.model-identity-sets-disjoint',
  )
  const kbIds = descriptor.kbIds
  check(
    Array.isArray(kbIds) &&
      kbIds.length > 0 &&
      kbIds.every((id) => typeof id === 'string' && id.length > 0),
    'fixture.explicit-kb-ids',
  )
  const reservations = descriptor.connectors
  const modes: AuthMode[] = ['account_key', 'connection_string', 'sas_token']
  check(record(reservations), 'fixture.connector-reservations')
  const rows = modes.map((mode) => {
    const row = (reservations as JsonObject)[mode]
    check(
      record(row) &&
        typeof row.id === 'string' &&
        /^[a-f0-9]{32}$/.test(row.id) &&
        typeof row.name === 'string' &&
        row.name.startsWith('after67-w-'),
      `fixture.reserved-connector.${mode}`,
    )
    return row as Reservation
  })
  check(new Set(rows.map((row) => row.id)).size === 3, 'fixture.unique-ids')
  check(new Set(rows.map((row) => row.name)).size === 3, 'fixture.unique-names')

  // A browser storage adapter is required by the real askStream/APIClient.
  // It holds only this fixture credential in memory; no disk persistence.
  const oldStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  const memory = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => memory.get(key) ?? null,
      setItem: (key: string, value: string) => memory.set(key, String(value)),
      removeItem: (key: string) => memory.delete(key),
      clear: () => memory.clear(),
      key: (index: number) => [...memory.keys()][index] ?? null,
      get length() {
        return memory.size
      },
    },
  })
  memory.set('auth_token', token)

  const nativeFetch = globalThis.fetch.bind(globalThis)
  let requestCount = 0
  let rewrittenCount = 0
  let expectedWrite:
    | { method: string; path: string; body: JsonObject }
    | undefined
  // No response is mocked: every request is delegated to native fetch.
  // Only the exact product Node fallback origin may be translated to the
  // explicit fixture origin. Redirects and all other origins are refused.
  globalThis.fetch = async (input, init) => {
    check(!(input instanceof Request), 'transport.string-url-required')
    const original = new URL(String(input))
    let url = original
    if (original.origin === 'http://localhost:8000') {
      url = new URL(original.pathname + original.search, fixture)
      rewrittenCount++
    }
    check(url.origin === fixture.origin, 'transport.fixture-origin-only')
    check(url.pathname.startsWith('/api/v1/'), 'transport.canonical-rest-only')
    const headers = new Headers(init?.headers)
    check(headers.get('authorization') === `Bearer ${token}`, 'transport.bearer')
    if (expectedWrite) {
      check(init?.method === expectedWrite.method, 'transport.write-method')
      check(url.pathname === expectedWrite.path, 'transport.write-path')
      check(typeof init?.body === 'string', 'transport.json-body')
      let body: unknown
      try {
        body = JSON.parse(init!.body as string)
      } catch {
        throw new CheckFailed('transport.valid-json-body')
      }
      check(
        canonical(body) === canonical(expectedWrite.body),
        'transport.exact-serialized-body',
      )
    }
    requestCount++
    return nativeFetch(url, { ...init, redirect: 'error' })
  }

  let stage = 'imports'
  let clearAuth: (() => void) | undefined
  try {
    const load = (path: string) => import(pathToFileURL(join(webRoot, path)).href)
    const { apiClient, APIError } = await load('src/api/client.ts')
    const { modelProvidersAPI } = await load('src/api/llm-providers.ts')
    const { searchAPI } = await load('src/api/search.ts')
    const { datasourceAPI } = await load('src/api/datasource.ts')
    const { prepareAzureBlobConfig } = await load(
      'src/pages/settings/datasource/constants/azure-blob.ts',
    )
    const { DataSourceKey } = await load('src/pages/settings/datasource/types.ts')
    const { assertSSEResponse, readSSEStream } = await load(
      'src/lib/streaming/transport.ts',
    )
    apiClient.setAuthToken(token)
    clearAuth = () => apiClient.setAuthToken(null)

    stage = 'model-defaults-and-inventory'
    const defaults = await modelProvidersAPI.defaults()
    const inventory = await modelProvidersAPI.models()
    const embedding = defaults.find((row: JsonObject) => row.model_type === 'embedding')
    check(
      embedding?.model_id === null &&
        embedding?.model_provider === 'Builtin' &&
        embedding?.model_instance === 'default' &&
        embedding?.model_name === teiModel &&
        typeof embedding?.reference === 'string' &&
        embedding.reference.length > 0,
      'models.builtin-readonly-default-metadata',
    )
    check(
      inventory.every(
        (row: JsonObject) =>
          Number.isSafeInteger(row.id) &&
          Number(row.id) > 0 &&
          row.reference === String(row.id),
      ),
      'models.real-numeric-identities',
    )
    const actualModelIds = inventory.map((row: JsonObject) => Number(row.id)).sort((a: number, b: number) => a - b)
    check(
      JSON.stringify(actualModelIds) ===
        JSON.stringify([...expectedModelIds].sort((a, b) => a - b)),
      'models.exact-owner-inventory',
    )
    check(
      forbiddenModelIds.every((id) => !actualModelIds.includes(id)),
      'models.foreign-numeric-ids-excluded',
    )
    check(
      !inventory.some(
        (row: JsonObject) => row.provider_name === 'Builtin' && row.name === teiModel,
      ),
      'models.no-synthetic-builtin-selection',
    )
    safe('assertions_passed', { case: stage, count: 5 })

    stage = 'search-canonical-completion-sse'
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 30000)
    let frames = 0
    let terminalFrames = 0
    let payloadFrames = 0
    const beforeSearch = requestCount
    try {
      expectedWrite = {
        method: 'POST',
        path: `/api/v1/searches/${encodeURIComponent(searchId)}/completions`,
        body: { question: 'fixture-only search request', kb_ids: kbIds },
      }
      const response = await searchAPI.askStream({
        question: 'fixture-only search request',
        kb_ids: kbIds,
        search_id: searchId,
        signal: controller.signal,
      })
      expectedWrite = undefined
      await assertSSEResponse(response)
      const end = await readSSEStream(response, {
        signal: controller.signal,
        parseErrorMode: 'throw',
        onEvent: (event: unknown) => {
          check(record(event) && event.code === 0, 'search.successful-envelope')
          frames++
          if ((event as JsonObject).data === true) terminalFrames++
          else if (record((event as JsonObject).data)) payloadFrames++
        },
      })
      check(end.reason === 'eof', 'search.normal-stream-end')
      check(terminalFrames === 1 && payloadFrames > 0, 'search.payload-and-terminal')
      check(requestCount === beforeSearch + 1, 'search.single-canonical-request')
      safe('assertions_passed', { case: stage, count: 4, frame_count: frames })
    } finally {
      clearTimeout(timeout)
      expectedWrite = undefined
    }

    // These are deliberately synthetic validation-only values. No Azure SDK
    // listing, connector link, sync worker, vendor request or deletion is run.
    const credentials = {
      account_name: 'after67fixture',
      account_key: 'Zml4dHVyZS1vbmx5LWtleQ==',
      container_name: 'fixture-container',
      connection_string:
        'DefaultEndpointsProtocol=https;AccountName=after67fixture;AccountKey=Zml4dHVyZS1vbmx5LWtleQ==;EndpointSuffix=core.windows.net',
      container_url: 'https://after67fixture.blob.core.windows.net/fixture-container',
      sas_token: 'sv=2023-11-03&sp=rl&sig=fixture-only',
    }
    const keysByMode: Record<AuthMode, string[]> = {
      account_key: ['account_name', 'account_key', 'container_name'],
      connection_string: ['connection_string', 'container_name'],
      sas_token: ['container_url', 'sas_token'],
    }
    const configFor = (mode: AuthMode, updated: boolean): JsonObject => {
      const prepared = prepareAzureBlobConfig({
        auth_mode: mode,
        prefix: updated ? 'fixture/updated/' : 'fixture/created/',
        batch_size: updated ? 3 : 2,
        allow_images: updated,
        sync_deleted_files: true,
        credentials: { ...credentials },
      })
      check(prepared.sync_deleted_files === false, 'azure.serializer-prune-off')
      check(
        record(prepared.credentials) &&
          JSON.stringify(Object.keys(prepared.credentials).sort()) ===
            JSON.stringify([...keysByMode[mode]].sort()),
        'azure.serializer-active-credentials-only',
      )
      return prepared
    }
    for (let index = 0; index < modes.length; index++) {
      const mode = modes[index]
      const nextMode = modes[(index + 1) % modes.length]
      const reservation = rows[index]
      stage = `azure-create-${mode}`
      const createConfig = configFor(mode, false)
      const create = {
        name: reservation.name,
        source: DataSourceKey.AZURE_BLOB,
        config: createConfig,
        refresh_freq: 0,
        prune_freq: 0,
        timeout_secs: 60,
      }
      expectedWrite = { method: 'POST', path: '/api/v1/connectors', body: create }
      const created = await datasourceAPI.connector.set(create)
      expectedWrite = undefined
      check(created.id === reservation.id, 'azure.preregistered-created-id')
      check(created.source === DataSourceKey.AZURE_BLOB, 'azure.source')
      safe('resource_created', { resource: 'connector', id: reservation.id, mode })
      const firstRead = await datasourceAPI.connector.get(reservation.id)
      check(canonical(firstRead.config) === canonical(createConfig), 'azure.created-config-readback')
      safe('assertions_passed', { case: stage, count: 5 })

      stage = `azure-update-${nextMode}`
      const updateConfig = configFor(nextMode, true)
      const update = {
        config: updateConfig,
        refresh_freq: 0,
        prune_freq: 0,
        timeout_secs: 60,
        reschedule: false,
      }
      expectedWrite = {
        method: 'PATCH',
        path: `/api/v1/connectors/${encodeURIComponent(reservation.id)}`,
        body: update,
      }
      await datasourceAPI.connector.set({
        id: reservation.id,
        name: reservation.name,
        source: DataSourceKey.AZURE_BLOB,
        ...update,
      })
      expectedWrite = undefined
      const updated = await datasourceAPI.connector.get(reservation.id)
      check(canonical(updated.config) === canonical(updateConfig), 'azure.updated-config-readback')
      check(updated.refresh_freq === 0 && updated.prune_freq === 0, 'azure.legal-zero-schedules')
      safe('assertions_passed', { case: stage, count: 4 })

      stage = `azure-reject-prune-${nextMode}`
      let rejected = false
      try {
        const invalidUpdate = {
          config: { ...updateConfig, sync_deleted_files: true },
        }
        expectedWrite = {
          method: 'PATCH',
          path: `/api/v1/connectors/${encodeURIComponent(reservation.id)}`,
          body: invalidUpdate,
        }
        await datasourceAPI.connector.update(reservation.id, invalidUpdate)
      } catch (error) {
        check(
          error instanceof APIError && error.status === 200 && error.code === '102',
          'azure.expected-config-business-rejection',
        )
        rejected = true
      } finally {
        expectedWrite = undefined
      }
      check(rejected, 'azure.unsupported-prune-rejected')
      const afterRejected = await datasourceAPI.connector.get(reservation.id)
      check(canonical(afterRejected.config) === canonical(updateConfig), 'azure.rejected-update-no-config-change')
      safe('assertions_passed', { case: stage, count: 2 })
    }
    safe('consumer_complete', {
      request_count: requestCount,
      fallback_origin_rewrites: rewrittenCount,
      connector_count: rows.length,
      cleanup_owner: 'I',
      validation_layer: 'real-web-client-controlled-python-http',
    })
  } catch (error) {
    safe('consumer_failed', {
      case: stage,
      check: error instanceof CheckFailed ? error.check : 'request-or-runtime-failed',
    })
    process.exitCode = 1
  } finally {
    expectedWrite = undefined
    clearAuth?.()
    memory.clear()
    globalThis.fetch = nativeFetch
    if (oldStorage) Object.defineProperty(globalThis, 'localStorage', oldStorage)
    else Reflect.deleteProperty(globalThis, 'localStorage')
  }
}

void main().catch((error: unknown) => {
  safe('consumer_failed', {
    case: 'preflight',
    check: error instanceof CheckFailed ? error.check : 'environment-or-runtime-failed',
  })
  process.exitCode = 1
})
