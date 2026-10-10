// Run: node --test frontend/tests/settings-upload-keys.test.mjs
// Execute the real settings page setup and drive the upload-key manager against a
// mocked API. Every request is local test data; no browser, server, or network is used.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { parse, compileScript } from "@vue/compiler-sfc";
import ts from "typescript";
import * as vue from "vue";

const app = new URL("../app/", import.meta.url);

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

const { descriptor } = parse(readFileSync(new URL("pages/settings.vue", app), "utf8"));
const settingsCode = compileScript(descriptor, { id: "settings-upload-keys-test" }).content;
const settle = async () => {
  await vue.nextTick();
  await setImmediate();
};

const KEY_ROW = {
  id: 7,
  label: "运营-张三",
  note: "",
  enabled: true,
  use_count: 3,
  last_used_at: "2024-05-01T10:00:00",
  masked: "uk_a…9999",
  display_suffix: "9999",
};

function settingsPage(t, { keys = [KEY_ROW], legacy = false, reply, clipboard } = {}) {
  const calls = [];
  const messages = [];
  const mounted = [];
  const unmounted = [];
  const clipboardApi = clipboard || { writeText: async () => {} };
  let items = keys.map((item) => ({ ...item }));
  const request = async (path, options = {}) => {
    calls.push({ path, ...options });
    if (reply) {
      const custom = reply(path, options);
      if (custom !== undefined) return custom;
    }
    if (path === "/api/config" && !options.method) return [];
    if (path === "/api/upload-keys" && !options.method)
      return { ok: true, items, legacy_env_configured: legacy };
    if (path === "/api/upload-keys" && options.method === "POST")
      return { ok: true, item: { ...KEY_ROW, label: options.body.label, secret: "uk_generated_secret_0001" } };
    if (path.startsWith("/api/upload-keys/") && path.endsWith("/rotate"))
      return { ok: true, item: { ...KEY_ROW, secret: "uk_rotated_secret_0002" } };
    if (path.startsWith("/api/upload-keys/")) return { ok: true };
    assert.fail(`Unexpected request: ${path}`);
  };
  const component = loadModule(settingsCode, {
    ...vue,
    definePageMeta() {},
    useHead() {},
    useApi: () => ({ request }),
    useToast: () => ({
      success: (message) => messages.push({ kind: "success", message }),
      error: (message) => messages.push({ kind: "error", message }),
      info: (message) => messages.push({ kind: "info", message }),
    }),
    useUpdateStatus: () => ({
      status: vue.ref({}),
      loading: vue.ref(false),
      applying: vue.ref(false),
      refresh: async () => {},
      checkNow: async () => {},
      applyUpdate: async () => ({ message: "" }),
    }),
    onMounted: (fn) => mounted.push(fn),
    onBeforeUnmount: (fn) => unmounted.push(fn),
    onBeforeRouteLeave: () => {},
    navigator: { clipboard: clipboardApi },
    window: { confirm: () => true, addEventListener() {}, removeEventListener() {} },
  }).default;
  const scope = vue.effectScope();
  const state = scope.run(() => component.setup({}, { expose() {} }));
  t.after(() => scope.stop());
  return {
    state,
    calls,
    messages,
    setItems: (next) => {
      items = next;
    },
    mount: async () => {
      mounted.forEach((fn) => fn());
      await settle();
    },
    posts: () => calls.filter((call) => call.method === "POST"),
  };
}

test("loads assigned keys and reports the legacy env key", async (t) => {
  const page = settingsPage(t, { legacy: true });
  await page.mount();
  assert.equal(page.state.uploadKeys.value.length, 1);
  assert.equal(page.state.uploadKeys.value[0].label, "运营-张三");
  assert.equal(page.state.legacyUploadKey.value, true);
  assert.equal(page.state.uploadKeysError.value, "");
});

test("creating a key shows the one-time secret and reloads the list", async (t) => {
  const page = settingsPage(t, { keys: [] });
  await page.mount();
  page.state.openUploadKey();
  page.state.uploadKeyForm.label = "新同事";
  page.state.uploadKeyForm.note = "临时";
  await page.state.saveUploadKey();
  const post = page.posts().find((call) => call.path === "/api/upload-keys");
  assert.deepEqual(post.body, { label: "新同事", note: "临时", enabled: true });
  assert.equal(page.state.uploadKeyOpen.value, false);
  assert.equal(page.state.issuedOpen.value, true);
  assert.equal(page.state.issuedKey.value, "uk_generated_secret_0001");
  assert.equal(page.state.issuedLabel.value, "新同事");
  assert.equal(page.calls.filter((call) => call.path === "/api/upload-keys" && !call.method).length, 2);
});

test("editing a key sends a PUT and does not show a secret", async (t) => {
  const page = settingsPage(t);
  await page.mount();
  page.state.openUploadKey(page.state.uploadKeys.value[0]);
  page.state.uploadKeyForm.enabled = false;
  await page.state.saveUploadKey();
  const put = page.calls.find((call) => call.method === "PUT");
  assert.equal(put.path, "/api/upload-keys/7");
  assert.deepEqual(put.body, { label: "运营-张三", note: "", enabled: false });
  assert.equal(page.state.issuedOpen.value, false);
});

test("rotate, toggle and delete hit the matching endpoints", async (t) => {
  const page = settingsPage(t);
  await page.mount();
  const row = page.state.uploadKeys.value[0];
  await page.state.rotateUploadKey(row);
  assert.equal(page.state.issuedKey.value, "uk_rotated_secret_0002");
  await page.state.toggleUploadKey(row, false);
  const toggle = page.calls.find((call) => call.method === "PUT" && call.body?.enabled === false);
  assert.equal(toggle.path, "/api/upload-keys/7");
  await page.state.removeUploadKey(row);
  const removed = page.calls.find((call) => call.method === "DELETE");
  assert.equal(removed.path, "/api/upload-keys/7");
  assert.ok(page.messages.some((item) => item.kind === "success"));
});

test("copying the issued key writes it to the clipboard", async (t) => {
  const written = [];
  const page = settingsPage(t, { clipboard: { writeText: async (value) => written.push(value) } });
  await page.mount();
  page.state.issuedKey.value = "uk_copy_me";
  page.state.issuedOpen.value = true;
  await page.state.copyIssuedKey();
  assert.deepEqual(written, ["uk_copy_me"]);
});
