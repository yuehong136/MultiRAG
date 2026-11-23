/** Exercise the pinned Web save serializers against registered backend routes. */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const root = process.env.WEB_COMPLETION_CHECKOUT;
const base = process.env.DOMAIN_ACCEPTANCE_BASE;
const token = process.env.DOMAIN_ACCEPTANCE_TOKEN;
const cache = process.env.WEB_CONSUMER_VITE_CACHE;
assert.ok(root && base && token && cache);
// Run the original hook's import graph through the Web toolchain, including
// Vite asset URLs and import.meta.glob. No UI module or data mapper is replaced.
const require = createRequire(pathToFileURL(`${root}/package.json`));
const { createServer } = await import(
  pathToFileURL(require.resolve("vite")).href
);
const vite = await createServer({
  root,
  configFile: false,
  cacheDir: cache,
  resolve: { alias: { "@": `${root}/src` } },
  server: { middlewareMode: true, hmr: false, watch: null },
  appType: "custom",
  logLevel: "error",
});
try {
  const load = (path: string) => vite.ssrLoadModule(`${root}/${path}`);
  const { apiClient } = await load("src/api/client.ts");
  const { dialogAPI } = await load("src/api/dialog.ts");
  const { llmAPI } = await load("src/api/llm.ts");
  const { getLLMModelValue } = await load("src/stores/model.ts");
  const { buildChatSaveRequest } = await load(
    "src/pages/studio/create-app/save-payload.ts",
  );
  const { createInitialConfig } = await load(
    "src/pages/studio/create-app/constants.ts",
  );
  const { settingsToDialogUpdate } = await load(
    "src/hooks/chat-settings-payload.ts",
  );
  const { dialogToSettings } = await load("src/hooks/use-chat-settings.ts");
  apiClient.setAuthToken(token);
  const originalFetch = globalThis.fetch;
  const receipts: Array<{
    path: string;
    method: string;
    status: number;
    code?: number;
  }> = [];
  globalThis.fetch = async (url, init) => {
    const parsed = new URL(String(url));
    const response = await originalFetch(
      `${base}${parsed.pathname}${parsed.search}`,
      init,
    );
    const body = await response
      .clone()
      .json()
      .catch(() => null);
    receipts.push({
      path: parsed.pathname,
      method: init?.method || "GET",
      status: response.status,
      code: body?.code,
    });
    return response;
  };
  try {
    const models = await llmAPI.getMyLLMs();
    const ids: string[] = [];
    const references: string[] = [];
    for (const [name, type] of [
      ["vision", "image2text"],
      ["text", "chat"],
    ]) {
      const model = models.Ollama.llm.find(
        (row: { name: string; type: string }) =>
          row.name === name && row.type === type,
      );
      assert.ok(model);
      const reference = getLLMModelValue(model, "Ollama");
      assert.match(reference, /^[1-9]\d*$/);
      references.push(reference);
      const config = createInitialConfig({
        name: `model-type-consumer-${name}`,
      });
      config.llm_id = reference;
      config.llm_setting = {
        temperature: 0,
        temperature_enabled: true,
        top_p_enabled: false,
      };
      config.prompt_config.quote = false;
      const created = await dialogAPI.createChat(
        buildChatSaveRequest(config, "", models),
      );
      ids.push(created.id);
      const detail = await dialogAPI.getDetail(created.id);
      assert.deepEqual(detail.llm_setting, {
        temperature: 0,
        model_type: type,
      });
      const settings = dialogToSettings(detail, "");
      assert.equal(settings.topPEnabled, false);
      assert.equal(settings.temperature, 0);
      await dialogAPI.updateChat(
        created.id,
        settingsToDialogUpdate(settings, "", models),
      );
      const reloaded = await dialogAPI.getDetail(created.id);
      assert.deepEqual(reloaded.llm_setting, {
        temperature: 0,
        model_type: type,
      });
      assert.equal(reloaded.prompt_config.quote, false);
    }
    const sibling = await dialogAPI.getDetail(ids[0]);
    assert.equal(sibling.llm_id, references[0]);
    const count = receipts.length;
    assert.throws(
      () =>
        settingsToDialogUpdate(
          { ...dialogToSettings(sibling, ""), llmId: "unknown" },
          "",
          models,
        ),
      /unavailable/,
    );
    assert.equal(receipts.length, count);
    console.log(
      JSON.stringify({
        ids,
        references,
        receipts,
        zeroPreserved: true,
        falsePreserved: true,
        unknownHasNoHTTP: true,
      }),
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
} finally {
  await vite.close();
}
