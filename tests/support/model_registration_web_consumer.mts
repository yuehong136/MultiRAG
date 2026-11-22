/** Register and reload through the pinned Web provider client. */
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const root = process.env.WEB_COMPLETION_CHECKOUT;
const base = process.env.DOMAIN_ACCEPTANCE_BASE;
const token = process.env.DOMAIN_ACCEPTANCE_TOKEN;
const providerBase = process.env.DOMAIN_ACCEPTANCE_PROVIDER;
assert.ok(root && base && token && providerBase);

const { apiClient } = await import(pathToFileURL(`${root}/src/api/client.ts`).href);
const { createModelProvidersAPI } = await import(
  pathToFileURL(`${root}/src/api/llm-providers.ts`).href
);
apiClient.setAuthToken(token);
const providers = createModelProvidersAPI(base);
const origin = new URL(base).origin;
const fetchOriginal = globalThis.fetch;
const paths: string[] = [];
globalThis.fetch = (input, options) => {
  const url = new URL(String(input));
  assert.equal(url.origin, origin);
  paths.push(url.pathname);
  return fetchOriginal(input, options);
};
try {
  const flags: boolean[] = [];
  const saved: Array<{ id: number; instance: string; is_tools: boolean }> = [];
  for (const enabled of [false, true, false]) {
    const instance = `combined-${flags.length}`;
    await providers.createInstance("Ollama", {
      instance_name: instance,
      api_key: "fixture-key",
      base_url: providerBase,
      region: "",
      model_ids: [],
      custom_model: {
        model_name: "combined-fixture",
        model_types: ["chat"],
        max_tokens: 4096,
        is_tools: enabled,
      },
    });
    const models = await providers.models();
    const model = models.find((item) => item.name === "combined-fixture" && item.instance_name === instance);
    assert.ok(model);
    assert.equal(model.is_tools, enabled);
    assert.equal(model.reference, String(model.id));
    assert.deepEqual(model.model_types, ["chat"]);
    const instances = await providers.instances("Ollama");
    assert.equal(instances.find((item) => item.instance_name === instance)?.has_api_key, true);
    assert.ok(!JSON.stringify({ models, instances }).includes("fixture-key"));
    flags.push(model.is_tools);
    saved.push({ id: model.id, instance, is_tools: model.is_tools });
  }
  assert.equal(new Set(saved.map((item) => item.id)).size, 3);
  apiClient.setAuthToken(null);
  await assert.rejects(providers.models());
  console.log(JSON.stringify({ flags, models: saved, paths, anonymous_denied: true }));
} finally {
  globalThis.fetch = fetchOriginal;
}
