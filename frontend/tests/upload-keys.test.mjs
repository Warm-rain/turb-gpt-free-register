// Run: node --test frontend/tests/upload-keys.test.mjs
// Execute the real upload page setup with Vue reactivity and a fake localStorage.
// Every request is local test data; no browser, server, or network is used.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { parse, compileScript } from "@vue/compiler-sfc";
import ts from "typescript";
import * as vue from "vue";

const app = new URL("../app/", import.meta.url);
const STORAGE_KEY = "registrator_upload_keys_v1";

function loadModule(source, globals = {}, imports = {}) {
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const exports = {};
  const allowedGlobals = Object.fromEntries(
    Object.entries(globals).filter(([name]) => /^[$A-Z_a-z][$\w]*$/.test(name)),
  );
  new Function("exports", "require", ...Object.keys(allowedGlobals), code)(
    exports,
    (name) => {
      if (name === "vue") return vue;
      assert.ok(name in imports, `Unexpected import: ${name}`);
      return imports[name];
    },
    ...Object.values(allowedGlobals),
  );
  return exports;
}

const { descriptor } = parse(readFileSync(new URL("pages/upload.vue", app), "utf8"));
const uploadCode = compileScript(descriptor, { id: "upload-keys-test" }).content;
const settle = async () => {
  await vue.nextTick();
  await setImmediate();
};

function memoryStorage() {
  const data = new Map();
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: (key) => data.delete(key),
  };
}

function uploadPage(t, { stored = null, request } = {}) {
  const calls = [];
  const mounted = [];
  const localStorage = memoryStorage();
  if (stored !== null) localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
  const component = loadModule(uploadCode, {
    ...vue,
    definePageMeta() {},
    useHead() {},
    useApi: () => ({
      request: async (path, options = {}) => {
        calls.push({ path, ...options });
        return request ? request(path, options) : { ok: true };
      },
    }),
    onMounted: (fn) => mounted.push(fn),
    localStorage,
    window: { confirm: () => true },
  }).default;
  const scope = vue.effectScope();
  const state = scope.run(() => component.setup({}, { expose() {} }));
  t.after(() => scope.stop());
  return {
    state,
    calls,
    localStorage,
    mount: async () => {
      mounted.forEach((fn) => fn());
      await settle();
    },
    saved: () => JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]"),
  };
}

test("saved keys load from localStorage, drop invalid rows, and prefill the newest", async (t) => {
  const page = uploadPage(t, {
    stored: [
      { label: "运营-张三", key: "uk_aaaa1111", saved_at: "2024-01-02T00:00:00Z" },
      { label: "", key: "", saved_at: "" },
      { key: "uk_bbbb2222" },
    ],
  });
  await page.mount();
  assert.deepEqual(page.state.savedKeys.value.map((item) => item.key), ["uk_aaaa1111", "uk_bbbb2222"]);
  assert.equal(page.state.savedKeys.value[0].label, "运营-张三");
  assert.equal(page.state.savedKeys.value[1].label, "Key …2222", "missing labels get a masked default");
  assert.equal(page.state.key.value, "uk_aaaa1111", "the newest saved key is prefilled");
});

test("saving trims the key, dedupes by value, and persists to localStorage", async (t) => {
  const page = uploadPage(t);
  await page.mount();
  page.state.key.value = "  uk_new1234  ";
  page.state.saveLabel.value = "运营-张三";
  page.state.saveCurrentKey();
  assert.equal(page.state.savedKeys.value.length, 1);
  assert.equal(page.state.savedKeys.value[0].label, "运营-张三");
  assert.equal(page.saved()[0].key, "uk_new1234", "stored value is trimmed");
  assert.match(page.state.storageNotice.value, /已在本机保存/);

  page.state.saveLabel.value = "改名";
  page.state.key.value = "uk_new1234";
  page.state.saveCurrentKey();
  assert.equal(page.state.savedKeys.value.length, 1, "same key is not stored twice");
  assert.equal(page.state.savedKeys.value[0].label, "改名");
  assert.equal(page.saved().length, 1);

  page.state.key.value = "   ";
  page.state.saveCurrentKey();
  assert.match(page.state.error.value, /请先输入上传 Key/);
});

test("use, remove and clear operate on the saved list", async (t) => {
  const page = uploadPage(t, {
    stored: [
      { label: "a", key: "uk_1111" },
      { label: "b", key: "uk_2222" },
    ],
  });
  await page.mount();
  page.state.useSavedKey(page.state.savedKeys.value[1]);
  assert.equal(page.state.key.value, "uk_2222");
  assert.match(page.state.storageNotice.value, /已填入/);

  const used = page.state.savedKeys.value.find((item) => item.key === "uk_2222");
  page.state.removeSavedKey(used);
  assert.deepEqual(page.state.savedKeys.value.map((item) => item.key), ["uk_1111"]);
  assert.equal(page.state.key.value, "", "removing the key in use clears the field");
  assert.equal(page.saved().length, 1);

  page.state.clearSavedKeys();
  assert.equal(page.state.savedKeys.value.length, 0);
  assert.equal(page.saved().length, 0);
});

test("a successful upload sends the saved key and moves it to the front", async (t) => {
  const page = uploadPage(t, {
    stored: [
      { label: "a", key: "uk_1111" },
      { label: "b", key: "uk_2222" },
    ],
    request: (path) => {
      assert.equal(path, "/api/public/accounts/import");
      return { ok: true, parsed: 1, inserted: 1, skipped: 0 };
    },
  });
  await page.mount();
  page.state.key.value = "uk_2222";
  page.state.text.value = "user@example.test--password--TOTP--token";
  await page.state.submit();
  assert.equal(page.calls.length, 1);
  assert.equal(page.calls[0].headers["X-Upload-Key"], "uk_2222");
  assert.equal(page.state.text.value, "", "uploaded text is cleared on success");
  assert.equal(page.state.savedKeys.value[0].key, "uk_2222", "last used saved key becomes the default");
  assert.equal(page.state.error.value, "");
});

test("masking helpers never expose the full key", async (t) => {
  const page = uploadPage(t);
  assert.equal(page.state.maskKey("uk_1234567890"), "uk_1…7890");
  assert.equal(page.state.maskKey("short"), "••••••");
  assert.equal(page.state.defaultKeyLabel("uk_1234567890"), "Key …7890");
  assert.equal(page.state.defaultKeyLabel("abc"), "已保存的 Key");
});
