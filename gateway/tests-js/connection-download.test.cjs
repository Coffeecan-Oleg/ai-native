// Execute the shipped script against a small DOM and controllable fetch, no network.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../gateway_mcp/help_assets/connection-download.js'), 'utf8');

function fixture(fetcher, reportUrl = '/platforma/connection/report?check=synthetic') {
  const calls = [], blobs = [], revoked = [], timers = new Map(), events = {};
  let nextTimer = 0;
  function element() {
    return {hidden: true, disabled: false, attrs: {}, textContent: '',
      setAttribute(k, v) {this.attrs[k] = v;},
      removeAttribute(k) {delete this.attrs[k]; if (k === 'href') delete this.href;},
      addEventListener(k, v) {this[k] = v;}, focus() {this.focused = true;}};
  }
  const prepare = element(), download = element(), status = element(), preview = element(), reportText = element();
  prepare.dataset = {reportUrl};
  class LocalURL extends URL {}
  LocalURL.createObjectURL = blob => {blobs.push(blob); return 'blob:https://copy.invalid/' + blobs.length;};
  LocalURL.revokeObjectURL = url => revoked.push(url);
  vm.runInNewContext(source, {
    document: {getElementById: id => ({'connection-report-prepare': prepare,
      'connection-report-download': download, 'connection-report-status': status,
      'connection-report-preview': preview, 'connection-report-text': reportText})[id]},
    window: {location: {href: 'https://copy.invalid/platforma/connection', origin: 'https://copy.invalid'},
      addEventListener: (name, handler) => {events[name] = handler;}},
    URL: LocalURL, Blob, AbortController,
    fetch: (...args) => {calls.push(args); return fetcher(...args);},
    setTimeout: (fn, delay) => {timers.set(++nextTimer, {fn, delay}); return nextTimer;},
    clearTimeout: id => timers.delete(id)
  });
  return {prepare, download, status, preview, reportText, calls, blobs, revoked, timers, events};
}
function response(text = 'Синтетический отчёт\n', status = 200, headers = {}) {
  return new Response(text, {status, headers: {'Content-Type': 'text/plain; charset=utf-8',
    'Content-Disposition': 'attachment; filename="platforma-connection.txt"', ...headers}});
}

test('does nothing until clicked; exact bytes downloaded via same-origin fetch, no navigation', async () => {
  const f = fixture(async () => response());
  assert.equal(f.calls.length, 0);
  await f.prepare.click();
  const [url, options] = f.calls[0];
  assert.equal(url, 'https://copy.invalid/platforma/connection/report?check=synthetic');
  for (const [key, value] of Object.entries({credentials: 'same-origin', mode: 'same-origin', redirect: 'error', cache: 'no-store', referrerPolicy: 'no-referrer'})) assert.equal(options[key], value);
  assert.equal(options.headers, undefined);
  assert.equal(options.body, undefined);
  assert.equal(await f.blobs[0].text(), 'Синтетический отчёт\n');
  assert.equal(f.download.hidden, false);
  assert.equal(f.download.focused, true);
  assert.match(f.download.href, /^blob:/);
  assert.equal(f.prepare.disabled, false);
  assert.match(f.status.textContent, /никому не отправлен/);
});

for (const code of [400, 401, 403, 503]) test('HTTP ' + code + ' gives inline recovery without response body leak', async () => {
  const f = fixture(async () => response('PRIVATE ERROR DETAILS', code));
  await f.prepare.click();
  assert.equal(f.blobs.length, 0);
  assert.equal(f.download.hidden, true);
  assert.equal(f.prepare.disabled, false);
  assert.doesNotMatch(f.status.textContent, /PRIVATE/);
  assert.match(f.status.textContent, code === 400 ? /новую проверку/ : code === 503 ? /позже/ : /заново/);
});

test('redirect/network failure is not followed or displayed as successful download', async () => {
  const f = fixture(async () => {throw new TypeError('PRIVATE redirect destination');});
  await f.prepare.click();
  assert.equal(f.blobs.length, 0);
  assert.match(f.status.textContent, /Не удалось/);
  assert.doesNotMatch(f.status.textContent, /PRIVATE/);
});

for (const headers of [{'Content-Type': 'text/html'}, {'Content-Disposition': 'attachment; filename="foreign.txt"'}]) {
  test('rejects login HTML or an unexpected attachment', async () => {
    const f = fixture(async () => response('LOGIN PRIVATE', 200, headers));
    await f.prepare.click();
    assert.equal(f.blobs.length, 0);
    assert.equal(f.download.hidden, true);
    assert.doesNotMatch(f.status.textContent, /LOGIN PRIVATE/);
  });
}

test('empty and oversized responses are never made downloadable', async () => {
  for (const text of ['', 'x'.repeat(65537)]) {
    const f = fixture(async () => response(text));
    await f.prepare.click();
    assert.equal(f.blobs.length, 0);
    assert.equal(f.download.hidden, true);
  }
});

test('foreign destinations or paths cannot receive session requests', async () => {
  for (const url of ['https://foreign.invalid/platforma/connection/report', '/admin', '//foreign.invalid/x']) {
    const f = fixture(async () => response(), url);
    await f.prepare.click();
    assert.equal(f.calls.length, 0);
    assert.equal(f.blobs.length, 0);
  }
});

test('busy state prevents duplicate requests; timeout aborts and permits retry', async () => {
  const f = fixture((url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  }));
  const pending = f.prepare.click();
  assert.equal(f.prepare.disabled, true);
  assert.match(f.status.textContent, /Подготавливаем/);
  await f.prepare.click();
  assert.equal(f.calls.length, 1);
  [...f.timers.values()].find(t => t.delay === 15000).fn();
  await pending;
  assert.equal(f.prepare.disabled, false);
  assert.equal(f.blobs.length, 0);
  assert.equal(f.calls[0][1].signal.aborted, true);
});

test('new preparation revokes old file; expiry clears the link', async () => {
  const f = fixture(async () => response());
  await f.prepare.click();
  const old = f.download.href;
  await f.prepare.click();
  assert.deepEqual(f.revoked, [old]);
  [...f.timers.values()].find(t => t.delay === 300000).fn();
  assert.equal(f.download.hidden, true);
  assert.equal(f.download.href, undefined);
  assert.equal(f.revoked.length, 2);
});

test('leaving clears in-memory report and pending request; returning can retry', async () => {
  const f = fixture(async () => response());
  await f.prepare.click();
  f.events.pagehide();
  assert.equal(f.download.hidden, true);
  assert.equal(f.revoked.length, 1);
  await f.prepare.click();
  assert.equal(f.download.hidden, false);
});

test('late request after leaving does not invalidate a new prepared file', async () => {
  let release;
  const f = fixture(() => f.calls.length === 1 ? new Promise(resolve => {release = resolve;}) : response());
  const pending = f.prepare.click();
  f.events.pagehide();
  await f.prepare.click();
  const latest = f.download.href;
  release(response());
  await pending;
  assert.equal(f.download.href, latest);
  assert.equal(f.download.hidden, false);
  assert.equal(f.blobs.length, 1);
});

test('preview uses exact report text as a value, including markup, without another fetch', async () => {
  const text = 'Отчёт\n</textarea><img src=x onerror=alert(1)>\n';
  const f = fixture(async () => response(text));
  await f.prepare.click();
  assert.equal(f.reportText.value, text);
  assert.equal(f.preview.hidden, false);
  assert.equal(f.reportText.innerHTML, undefined);
  assert.equal(await f.blobs[0].text(), f.reportText.value);
  assert.equal(f.calls.length, 1);
});

test('retry failure clears a previous report preview, without displaying the error body', async () => {
  const f = fixture(() => f.calls.length === 1 ? response() : response('PRIVATE ERROR',403));
  await f.prepare.click();
  f.preview.open = true;
  await f.prepare.click();
  assert.equal(f.preview.hidden, true);
  assert.equal(f.preview.open, false);
  assert.equal(f.reportText.value, '');
});

test('expiry and leaving clear the report text as well as the download link', async () => {
  const f = fixture(async () => response());
  await f.prepare.click();
  [...f.timers.values()].find(t => t.delay === 300000).fn();
  assert.equal(f.reportText.value, '');
  assert.equal(f.preview.hidden, true);
  await f.prepare.click();
  f.events.pagehide();
  assert.equal(f.reportText.value, '');
  assert.equal(f.preview.hidden, true);
});
