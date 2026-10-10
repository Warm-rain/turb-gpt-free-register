// Run: node --test frontend/tests/settings-driver-chain.test.mjs
// Drives the real settings page chain editor (registration / liveness / Codex
// priority chains) against a mocked API. No browser, server or network is used.
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
const settingsCode = compileScript(descriptor, { id: "settings-driver-chain-test" }).content;
const settle = async () => {
  await vue.nextTick();
  await setImmediate();
};

const DRIVER_CHOICES = [
  { value: "protocol", label: "纯协议" },
  { value: "roxy", label: "RoxyBrowser" },
  { value: "cloak", label: "CloakBrowser" },
  { value: "browser_use", label: "Browser Use" },
  { value: "skyvern", label: "Skyvern" },
];

function configFields(registration = "roxy, cloak, protocol", live = "protocol") {
  return [
    {
      key: "REGISTRATION_DRIVER",
      group: "注册方式",
      label: "注册驱动",
      help: "",
      type: "str",
      chain: true,
      choices: DRIVER_CHOICES,
      value: registration,
    },
    {
      key: "LIVE_CHECK_DRIVER",
      group: "账号查活",
      label: "查活驱动",
      help: "",
      type: "str",
      chain: true,
      choices: DRIVER_CHOICES.slice(0, 2),
      value: live,
    },
  ];
}

function settingsPage(t, { fields = configFields(), reply } = {}) {
  const calls = [];
  const messages = [];
  const mounted = [];
  const request = async (path, options = {}) => {
    calls.push({ path, ...options });
    if (reply) {
      const custom = reply(path, options, fields);
      if (custom !== undefined) return custom;
    }
    if (path === "/api/config" && !options.method) return fields.map((f) => ({ ...f }));
    if (path === "/api/config" && options.method === "POST") return { updated: Object.keys(options.body.updates) };
    if (path === "/api/upload-keys" && !options.method) return { ok: true, items: [], legacy_env_configured: false };
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
    onBeforeUnmount() {},
    onBeforeRouteLeave() {},
    navigator: { clipboard: { writeText: async () => {} } },
    window: { confirm: () => true, addEventListener() {}, removeEventListener() {} },
  }).default;
  const scope = vue.effectScope();
  const state = scope.run(() => component.setup({}, { expose() {} }));
  t.after(() => scope.stop());
  return {
    state,
    calls,
    messages,
    mount: async () => {
      mounted.forEach((fn) => fn());
      await settle();
    },
    field: (key) => state.fields.value.find((f) => f.key === key),
    posts: () => calls.filter((call) => call.method === "POST"),
  };
}

test("renders each configured layer in priority order with labels", async (t) => {
  const page = settingsPage(t);
  await page.mount();
  const field = page.field("REGISTRATION_DRIVER");
  assert.deepEqual(page.state.chainItems(field), ["roxy", "cloak", "protocol"]);
  assert.deepEqual(
    page.state.chainItems(field).map((item) => page.state.chainLabel(field, item)),
    ["RoxyBrowser", "CloakBrowser", "纯协议"],
  );
  // 剩余候选只列出还没进链的途径，避免同一个途径被排两次。
  assert.deepEqual(
    page.state.chainRemaining(field).map((choice) => choice.value),
    ["browser_use", "skyvern"],
  );
  assert.equal(page.state.chainItems(page.field("LIVE_CHECK_DRIVER")).length, 1);
});

test("parses arrow / semicolon / alias spellings from the stored value", async (t) => {
  const page = settingsPage(t, { fields: configFields("roxybrowser -> cloak; protocol") });
  await page.mount();
  const field = page.field("REGISTRATION_DRIVER");
  assert.deepEqual(page.state.chainItems(field), ["roxybrowser", "cloak", "protocol"]);
  // 别名原样展示，保存时由后端归一为规范写法。
  assert.equal(page.state.chainLabel(field, "roxybrowser"), "roxybrowser");
});

test("move / remove / append rewrite the stored chain", async (t) => {
  const page = settingsPage(t);
  await page.mount();
  const field = page.field("REGISTRATION_DRIVER");

  page.state.chainMove(field, 0, 1);
  assert.equal(page.state.values.REGISTRATION_DRIVER, "cloak, roxy, protocol");

  // 第一层再上移是禁用操作，不应改变顺序。
  page.state.chainMove(field, 0, -1);
  assert.equal(page.state.values.REGISTRATION_DRIVER, "cloak, roxy, protocol");

  page.state.chainMove(field, 1, -1);
  assert.equal(page.state.values.REGISTRATION_DRIVER, "roxy, cloak, protocol");

  page.state.chainRemove(field, 1);
  assert.equal(page.state.values.REGISTRATION_DRIVER, "roxy, protocol");

  page.state.chainDraft.REGISTRATION_DRIVER = "skyvern";
  page.state.chainAppend(field);
  assert.equal(page.state.values.REGISTRATION_DRIVER, "roxy, protocol, skyvern");
  assert.equal(page.state.chainDraft.REGISTRATION_DRIVER, "");
  assert.deepEqual(page.state.chainRemaining(field).map((c) => c.value), ["cloak", "browser_use"]);
});

test("append falls back to the first remaining choice and ignores duplicates", async (t) => {
  const page = settingsPage(t);
  await page.mount();
  const field = page.field("REGISTRATION_DRIVER");
  page.state.chainAppend(field);
  assert.equal(page.state.values.REGISTRATION_DRIVER, "roxy, cloak, protocol, browser_use");
  page.state.chainDraft.REGISTRATION_DRIVER = "roxy";
  page.state.chainAppend(field);
  // roxy 已在链里：chainRemaining 不会提供它，重复添加不会写回重复层。
  assert.equal(page.state.values.REGISTRATION_DRIVER.split("roxy").length - 1, 1);
});

test("clearing every layer and saving posts the empty chain", async (t) => {
  const page = settingsPage(t, { fields: configFields("roxy", "protocol") });
  await page.mount();
  const field = page.field("REGISTRATION_DRIVER");
  page.state.chainRemove(field, 0);
  assert.equal(page.state.values.REGISTRATION_DRIVER, "");
  await page.state.save();
  const post = page.posts().find((call) => call.path === "/api/config");
  assert.deepEqual(post.body.updates, { REGISTRATION_DRIVER: "" });
  assert.ok(!("LIVE_CHECK_DRIVER" in post.body.updates), "未改动的字段不应提交");
  assert.ok(page.messages.some((item) => item.kind === "success"));
});

test("reordering and adding is submitted as a canonical comma string", async (t) => {
  const page = settingsPage(t);
  await page.mount();
  const field = page.field("REGISTRATION_DRIVER");
  page.state.chainMove(field, 2, -1);
  page.state.chainDraft.REGISTRATION_DRIVER = "skyvern";
  page.state.chainAppend(field);
  await page.state.save();
  const post = page.posts().find((call) => call.path === "/api/config");
  assert.equal(post.body.updates.REGISTRATION_DRIVER, "roxy, protocol, cloak, skyvern");
});
