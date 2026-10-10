// Run: node --test tests/test_config_driver_chain_ui.js
// Exercise the real "多途径优先级链" UI helpers from the modern console without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'webui/static/console.js'), 'utf8');

function functionSource(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, `Missing function ${name}`);
  return source.slice(match.index, source.indexOf('\n}', match.index) + 2);
}

const DRIVER_FIELDS = [
  {
    key: 'REGISTRATION_DRIVER', label: '注册驱动', help: '', type: 'str', chain: true, value: 'roxy, cloak, protocol',
    choices: [
      { value: 'protocol', label: '纯协议注册' },
      { value: 'roxy', label: 'RoxyBrowser' },
      { value: 'cloak', label: '本地指纹浏览器' },
      { value: 'browser_use', label: 'browser_use' },
      { value: 'skyvern', label: 'skyvern' },
    ],
  },
  {
    key: 'LIVE_CHECK_DRIVER', label: '查活驱动', help: '', type: 'str', chain: true, value: 'protocol',
    choices: [
      { value: 'protocol', label: '纯协议（默认）' },
      { value: 'cloak', label: 'CloakBrowser' },
    ],
  },
  {
    key: 'CODEX_OAUTH_DRIVER', label: 'Codex授权驱动', help: '', type: 'str', chain: true, value: 'roxy',
    choices: [
      { value: 'protocol', label: '纯协议授权' },
      { value: 'roxy', label: 'RoxyBrowser' },
      { value: 'same_as_registration', label: '跟随注册驱动' },
    ],
  },
  { key: 'LIVE_CHECK_WORKERS', label: '查活并发', help: '', type: 'int', value: 3 },
];

function harness(pending = {}) {
  const rerenders = [];
  const context = {
    CONFIG: DRIVER_FIELDS.map(field => ({ ...field })),
    CONFIG_PENDING_UPDATES: { ...pending },
    document: { getElementById: () => null, querySelectorAll: () => [] },
    renderConfigLayoutV2: () => rerenders.push(Date.now()),
    closeConfigEpSelects() {},
    bindRoxyWorkspaceToolsV2() {},
    bindCloudMailToolsV2() {},
  };
  vm.createContext(context);
  const names = [
    'fmt', 'esc', 'attrEsc', 'isPlaceholderEmpty',
    'parseDriverChainText', 'driverChainChoices', 'driverChainFieldValue', 'driverChainItems',
    'driverChainLabel', 'driverChainRemaining', 'applyDriverChainAction', 'driverChainAppendValue',
    'setDriverChainValue', 'renderDriverChainList',
    'registrationDriverChoices', 'registrationDriverLabel', 'setRegistrationDriverValue', 'renderRegistrationDriverField',
    'codexOauthDriverChoices', 'codexOauthDriverLabel', 'setCodexOauthDriverValue', 'renderCodexOauthDriverField',
    'renderConfigExternalLink', 'renderConfigRecommendedLinks', 'renderConfigPlainFieldV2',
  ];
  vm.runInContext(names.map(functionSource).join('\n'), context);
  return {
    c: context,
    rerenders,
    field: key => context.CONFIG.find(f => f.key === key),
    value: key => context.CONFIG_PENDING_UPDATES[key],
  };
}

// vm 里的数组属于另一个 realm，比较前先转成本 realm 的数组。
const arr = value => Array.from(value);

test('chain text parses commas, arrows, semicolons and whitespace in order', () => {
  const ui = harness(), c = ui.c;
  assert.deepEqual(arr(c.parseDriverChainText('roxy, cloak, protocol')), ['roxy', 'cloak', 'protocol']);
  assert.deepEqual(arr(c.parseDriverChainText('roxy -> cloak; protocol')), ['roxy', 'cloak', 'protocol']);
  assert.deepEqual(arr(c.parseDriverChainText('roxy → cloak → protocol')), ['roxy', 'cloak', 'protocol']);
  assert.deepEqual(arr(c.parseDriverChainText('  Roxy ，Cloak　protocol ')), ['roxy', 'cloak', 'protocol']);
  assert.deepEqual(arr(c.parseDriverChainText('roxy||cloak')), ['roxy', 'cloak']);
  assert.deepEqual(arr(c.parseDriverChainText('')), []);
  assert.deepEqual(arr(c.parseDriverChainText(null)), []);
});

test('rendered chain lists every layer with labels and only offers unused drivers', () => {
  const ui = harness(), c = ui.c;
  const field = ui.field('REGISTRATION_DRIVER');
  const html = c.renderDriverChainList(field);
  assert.match(html, /data-chain-field="REGISTRATION_DRIVER"/);
  assert.match(html, /<input type="hidden" data-key="REGISTRATION_DRIVER" value="roxy, cloak, protocol">/);
  assert.deepEqual([...html.matchAll(/config-chain-order">(\d+)</g)].map(m => m[1]), ['1', '2', '3']);
  assert.ok(html.indexOf('RoxyBrowser') < html.indexOf('本地指纹浏览器'));
  assert.ok(html.indexOf('本地指纹浏览器') < html.indexOf('纯协议注册'));
  // 已进链的途径不再出现在“添加为下一层”的下拉里
  assert.deepEqual([...html.matchAll(/<option value="([^"]+)"/g)].map(m => m[1]), ['browser_use', 'skyvern']);
  // 第一层不能上移，最后一层不能下移
  const buttons = [...html.matchAll(/<button[^>]*data-chain-action="(up|down|remove)"[^>]*>/g)].map(m => m[0]);
  const disabled = action => buttons.filter(b => b.includes(`data-chain-action="${action}"`) && / disabled/.test(b));
  assert.equal(buttons.length, 9);
  assert.equal(disabled('up').length, 1);
  assert.equal(disabled('down').length, 1);
  assert.equal(disabled('remove').length, 0);
});

test('chain list escapes hostile values and explains the empty chain', () => {
  const ui = harness({ REGISTRATION_DRIVER: '"><script>alert(1)</script>' }), c = ui.c;
  const html = c.renderDriverChainList(ui.field('REGISTRATION_DRIVER'));
  assert.doesNotMatch(html, /<script/);
  assert.match(html, /&lt;script/);
  assert.match(html, /&quot;/);
  // ">" 是箭头分隔符，脏值会被拆层，但每一层都必须转义后再进 HTML。
  assert.match(html, /value="&quot;, &lt;script, alert\(1\)&lt;\/script"/);
  // 空格是分隔符：带空白的脏值会被拆成多层，不会漏进属性里。
  const split = harness({ REGISTRATION_DRIVER: 'roxy cloak' });
  assert.deepEqual(arr(split.c.driverChainItems(split.field('REGISTRATION_DRIVER'))), ['roxy', 'cloak']);
  const empty = harness({ REGISTRATION_DRIVER: '' });
  assert.match(empty.c.renderDriverChainList(empty.field('REGISTRATION_DRIVER')), /config-chain-empty/);
  assert.match(empty.c.renderDriverChainList(empty.field('REGISTRATION_DRIVER')), /<option value="protocol"/);
});

test('move / remove / append helpers are pure and guard the edges', () => {
  const ui = harness(), c = ui.c;
  const chain = ['roxy', 'cloak', 'protocol'];
  assert.deepEqual(arr(c.applyDriverChainAction(chain, 'up', 1)), ['cloak', 'roxy', 'protocol']);
  assert.deepEqual(arr(c.applyDriverChainAction(chain, 'down', 0)), ['cloak', 'roxy', 'protocol']);
  assert.deepEqual(arr(c.applyDriverChainAction(chain, 'remove', 1)), ['roxy', 'protocol']);
  // 越界操作原样返回，且不修改传入数组
  assert.deepEqual(arr(c.applyDriverChainAction(chain, 'up', 0)), chain);
  assert.deepEqual(arr(c.applyDriverChainAction(chain, 'down', 2)), chain);
  assert.deepEqual(arr(c.applyDriverChainAction(chain, 'remove', 9)), chain);
  assert.deepEqual(chain, ['roxy', 'cloak', 'protocol']);

  const choices = ui.field('REGISTRATION_DRIVER').choices;
  assert.deepEqual(arr(c.driverChainAppendValue(chain, choices, 'skyvern')), ['roxy', 'cloak', 'protocol', 'skyvern']);
  // 选中项已在链里（或下拉没选中）时退回落第一个未使用途径，不产生重复层
  assert.deepEqual(arr(c.driverChainAppendValue(chain, choices, 'roxy')), ['roxy', 'cloak', 'protocol', 'browser_use']);
  assert.deepEqual(arr(c.driverChainAppendValue(chain, choices, '')), ['roxy', 'cloak', 'protocol', 'browser_use']);
  const full = ['roxy', 'cloak', 'protocol', 'browser_use', 'skyvern'];
  assert.deepEqual(arr(c.driverChainAppendValue(full, choices, 'roxy')), full);
});

test('setting a chain writes the comma string and re-renders the config layout', () => {
  const ui = harness(), c = ui.c;
  c.setDriverChainValue('REGISTRATION_DRIVER', ['cloak', 'protocol']);
  assert.equal(ui.value('REGISTRATION_DRIVER'), 'cloak, protocol');
  assert.equal(ui.rerenders.length, 1);

  c.setRegistrationDriverValue('roxy -> protocol');
  assert.equal(ui.value('REGISTRATION_DRIVER'), 'roxy, protocol');
  c.setCodexOauthDriverValue('cloak');
  assert.equal(ui.value('CODEX_OAUTH_DRIVER'), 'cloak');
});

test('dashboard labels render every layer instead of a raw string', () => {
  const ui = harness(), c = ui.c;
  assert.equal(c.registrationDriverLabel('roxy, cloak, protocol'), 'RoxyBrowser → 本地指纹浏览器 → 纯协议注册');
  assert.equal(c.codexOauthDriverLabel('roxy, protocol'), 'RoxyBrowser → 纯协议授权');
  assert.equal(c.registrationDriverLabel('roxybrowser, typo'), 'roxybrowser → typo');
  assert.equal(c.registrationDriverLabel(''), '—');
});

test('registration and Codex sections render chain editors, plain fields still render normally', () => {
  const ui = harness(), c = ui.c;
  const registration = c.renderRegistrationDriverField(ui.field('REGISTRATION_DRIVER'));
  assert.match(registration, /config-chain-list/);
  assert.match(registration, /data-chain-field="REGISTRATION_DRIVER"/);
  const codex = c.renderCodexOauthDriverField(ui.field('CODEX_OAUTH_DRIVER'));
  assert.match(codex, /data-chain-field="CODEX_OAUTH_DRIVER"/);
  // 查活驱动走通用渲染器：chain 字段渲染排序器，普通字段仍是输入框
  const live = c.renderConfigPlainFieldV2(ui.field('LIVE_CHECK_DRIVER'));
  assert.match(live, /data-chain-field="LIVE_CHECK_DRIVER"/);
  const workers = c.renderConfigPlainFieldV2(ui.field('LIVE_CHECK_WORKERS'));
  assert.match(workers, /data-key="LIVE_CHECK_WORKERS"/);
  assert.doesNotMatch(workers, /config-chain/);
});
