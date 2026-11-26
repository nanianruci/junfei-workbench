const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('collector accepts the documented default loopback address only', () => {
  const source = fs.readFileSync(path.join(__dirname, 'extension', 'popup.js'), 'utf8')
    .split(/\r?\n/).slice(0, 3).join('\n');
  const values = {serverUrl: {value: ''}};
  const context = {URL, document: {getElementById: id => values[id]}};
  vm.runInNewContext(source, context);
  assert.equal(context.collectorServerUrl(), 'http://127.0.0.1:18769');
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, 'extension', 'manifest.json'), 'utf8'));
  for (const pattern of manifest.host_permissions) {
    values.serverUrl.value = pattern.slice(0, -2);
    assert.equal(context.collectorServerUrl(), values.serverUrl.value);
  }
  values.serverUrl.value = 'http://127.0.0.1:18767';
  assert.throws(() => context.collectorServerUrl(), /请输入HTTPS工作台/);
});
