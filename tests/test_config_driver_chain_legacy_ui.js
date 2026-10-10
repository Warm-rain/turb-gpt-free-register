// Run: node --test tests/test_config_driver_chain_legacy_ui.js
// 历史版顶部导航控制台（index_legacy.html）的「多途径优先级链」渲染与交互。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const root = path.join(__dirname, '..');
const legacy = fs.readFileSync(path.join(root, 'webui/templates/index_legacy.html'), 'utf8');

// 从 index_legacy.html 的内联脚本里抽取真实函数声明，保证测试跟着实现走。
function functionSource(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(legacy);
  assert.ok(match, `Missing function ${name}`);
  return legacy.slice(match.index, legacy.indexOf('\n}', match.index) + 2);
}
// vm 里创建的对象/数组跨 realm，比较前先转成本 realm 的普通结构。
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

const DRIVER_FIELDS = [
  {
    key: 'REGISTRATION_DRIVER', label: '注册驱动', help: '注册方式说明', type: 'str', chain: true,
    value: 'roxy, cloak, protocol',
    choices: [
      { value: 'protocol', label: '纯协议' },
      { value: 'roxy', label: 'RoxyBrowser' },
      { value: 'cloak', label: 'CloakBrowser' },
      { value: 'browser_use', label: 'Browser Use' },
      { value: 'skyvern', label: 'Skyvern' },
    ],
  },
  {
    key: 'LIVE_CHECK_DRIVER', label: '查活驱动', help: '', type: 'str', chain: true, value: 'protocol',
    choices: [{ value: 'protocol', label: '纯协议（默认）' }, { value: 'cloak', label: 'CloakBrowser' }],
  },
  {
    key: 'SMTP_SECURITY', label: '连接安全方式', help: '', type: 'str', value: 'ssl',
    choices: [{ value: 'ssl', label: 'SSL' }, { value: 'plain', label: '明文' }],
  },
];

function harness(pending = {}) {
  const renders = [];
  const context = {
    CONFIG: DRIVER_FIELDS.map(field => ({ ...field })),
    CONFIG_PENDING_UPDATES: { ...pending },
    CONFIG_ACTIVE_GROUP: '注册方式',
    renderConfigPanel: () => renders.push(Date.now()),
    document: { getElementById: () => null },
  };
  vm.createContext(context);
  vm.runInContext([
    'fmt', 'esc', 'attrEsc', 'isPlaceholderEmpty',
    'parseDriverChainText', 'driverChainFieldValue', 'driverChainItems', 'driverChainChoices',
    'driverChainLabel', 'driverChainRemaining', 'applyDriverChainAction', 'driverChainAppendValue',
    'setDriverChainValue', 'renderDriverChainField', 'renderConfigField',
    'renderConfigExternalLink', 'renderConfigRecommendedLinks',
  ].map(functionSource).join('\n'), context);
  return {
    c: context,
    renders,
    field: key => context.CONFIG.find(f => f.key === key),
    value: key => context.CONFIG_PENDING_UPDATES[key],
  };
}

test('legacy chain parser accepts the same separators as the modern console', () => {
  const { c } = harness();
  assert.deepEqual(plain(c.parseDriverChainText('roxy, cloak > protocol')), ['roxy', 'cloak', 'protocol']);
  assert.deepEqual(plain(c.parseDriverChainText('roxy；cloak　protocol')), ['roxy', 'cloak', 'protocol']);
  assert.deepEqual(plain(c.parseDriverChainText('')), []);
  assert.deepEqual(plain(c.driverChainItems({ key: 'X', value: 'cloak,protocol' })), ['cloak', 'protocol']);
});

test('legacy chain renderer lists layers in order and only offers unused drivers', () => {
  const ui = harness(), c = ui.c;
  const html = c.renderDriverChainField(ui.field('REGISTRATION_DRIVER'));
  assert.match(html, /data-chain-field="REGISTRATION_DRIVER"/);
  assert.match(html, /<input type="hidden" data-key="REGISTRATION_DRIVER" value="roxy, cloak, protocol">/);
  assert.ok(html.indexOf('RoxyBrowser') < html.indexOf('CloakBrowser'));
  assert.ok(html.indexOf('CloakBrowser') < html.indexOf('>纯协议<'));
  assert.deepEqual([...html.matchAll(/<option value="([^"]+)"/g)].map(m => m[1]), ['browser_use', 'skyvern']);
  const buttons = [...html.matchAll(/<button[^>]*data-chain-action="(up|down|remove)"[^>]*>/g)].map(m => m[0]);
  assert.equal(buttons.length, 9);
  assert.equal(buttons.filter(b => b.includes('"up"') && / disabled/.test(b)).length, 1);
  assert.equal(buttons.filter(b => b.includes('"down"') && / disabled/.test(b)).length, 1);
  // 链为空时给出提示，并允许从头添加
  const empty = harness({ REGISTRATION_DRIVER: '' });
  const emptyHtml = empty.c.renderDriverChainField(empty.field('REGISTRATION_DRIVER'));
  assert.match(emptyHtml, /config-chain-empty/);
  assert.match(emptyHtml, /data-chain-append="REGISTRATION_DRIVER"/);
});

test('legacy chain renderer escapes hostile values', () => {
  const ui = harness({ REGISTRATION_DRIVER: '"><script>alert(1)</script>' }), c = ui.c;
  const html = c.renderDriverChainField(ui.field('REGISTRATION_DRIVER'));
  assert.doesNotMatch(html, /<script/);
  assert.match(html, /value="&quot;, &lt;script, alert\(1\)&lt;\/script"/);
});

test('legacy renderConfigField uses the chain editor only for chain fields', () => {
  const ui = harness(), c = ui.c;
  const registration = c.renderConfigField(ui.field('REGISTRATION_DRIVER'));
  assert.match(registration, /config-chain-list/);
  const live = c.renderConfigField(ui.field('LIVE_CHECK_DRIVER'));
  assert.match(live, /data-chain-field="LIVE_CHECK_DRIVER"/);
  // 普通 choices 字段仍然是原来的下拉框
  const smtp = c.renderConfigField(ui.field('SMTP_SECURITY'));
  assert.match(smtp, /<select data-key="SMTP_SECURITY">/);
  assert.doesNotMatch(smtp, /config-chain/);
});

test('legacy chain actions rewrite the pending value and re-render the panel', () => {
  const ui = harness(), c = ui.c;
  const field = ui.field('REGISTRATION_DRIVER');
  c.setDriverChainValue(field.key, c.applyDriverChainAction(c.driverChainItems(field), 'down', 0));
  assert.equal(ui.value('REGISTRATION_DRIVER'), 'cloak, roxy, protocol');
  c.setDriverChainValue(field.key, c.applyDriverChainAction(c.driverChainItems(field), 'remove', 0));
  assert.equal(ui.value('REGISTRATION_DRIVER'), 'roxy, protocol');
  c.setDriverChainValue(field.key, c.driverChainAppendValue(c.driverChainItems(field), field.choices, 'skyvern'));
  assert.equal(ui.value('REGISTRATION_DRIVER'), 'roxy, protocol, skyvern');
  // 已进链的途径不会被重复追加
  c.setDriverChainValue(field.key, c.driverChainAppendValue(c.driverChainItems(field), field.choices, 'roxy'));
  assert.equal(ui.value('REGISTRATION_DRIVER'), 'roxy, protocol, skyvern, cloak');
  assert.equal(ui.renders.length, 4);
});
