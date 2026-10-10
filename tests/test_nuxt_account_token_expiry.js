// Run: node --test tests/test_nuxt_account_token_expiry.js
// 账号列表「AT 到期时间」展示规则（Nuxt 版）：后端自动推算，前端只负责渲染。
const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');

const modulePromise = import(
  path.join(__dirname, '../frontend/app/utils/accounts.ts')
);

async function token(account) {
  const { accountTokenInfo } = await modulePromise;
  return accountTokenInfo(account);
}

test('未过期的 AT 显示自动推算的到期时间', async () => {
  const info = await token({
    id: 1,
    at_state: 'valid',
    at_expires_at: '2026-07-17T17:38:38Z',
    at_expired: false,
  });
  assert.equal(info.state, 'valid');
  assert.match(info.label, /^有效至 /);
  assert.ok(info.expiresLabel);
  assert.match(info.title, /未过期/);
  assert.match(info.title, /到期时间/);
});

test('已过期的 AT 同时给出状态与到期时间', async () => {
  const info = await token({
    id: 2,
    at_state: 'expired',
    at_expires_at: '2020-01-01T00:00:00Z',
    at_expired: true,
  });
  assert.equal(info.state, 'expired');
  assert.match(info.label, /^已过期/);
  assert.ok(info.expiresLabel);
  assert.match(info.title, /需要重新查活刷新 AT/);
});

test('信息不足时不编造到期时间', async () => {
  const info = await token({ id: 3, access_token: 'not-a-jwt' });
  assert.equal(info.state, 'unknown');
  assert.equal(info.expiresLabel, '');
  assert.equal(info.label, '信息不足');
  assert.match(info.title, /无法判断/);
});

test('缺少 at_state 时按 at_expired / at_expires_at 回退推断', async () => {
  const expired = await token({ id: 4, at_expired: true });
  assert.equal(expired.state, 'expired');
  assert.equal(expired.label, '已过期');

  const valid = await token({ id: 5, at_expires_at: '2026-07-17T17:38:38Z' });
  assert.equal(valid.state, 'valid');
  assert.match(valid.label, /^有效至 /);

  const unknown = await token({ id: 6 });
  assert.equal(unknown.state, 'unknown');
});
