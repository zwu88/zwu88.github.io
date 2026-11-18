'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../assets/js/scholar-stats.js'), 'utf8');

class Element {
  constructor(kind, text = '') {
    this.kind = kind;
    this.textContent = text;
    this.attributes = {};
    this.children = [];
    this.style = {};
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  appendChild(child) { this.children.push(child); child.parent = this; }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  querySelector(selector) {
    return this.children.find(child => selector === '.periodical' ? child.kind === 'periodical' :
      Object.hasOwn(child.attributes, 'data-scholar-citations')) || null;
  }
}

function fixture() {
  return {
    name: 'Zhikai Wu', citedby: 1337, hindex: 9, status: 'ok',
    updated: '2026-11-18T00:00:00Z',
    publications: {
      p1: { author_pub_id: 'author:one', num_citations: 0 },
      'author:two': { num_citations: 1234 },
      p3: null,
      p4: { author_pub_id: 'author:three', num_citations: null }
    }
  };
}

function harness(payload, options = {}) {
  const elements = Object.fromEntries(['citations', 'h-index', 'update-time'].map(id =>
    [id, new Element('stat', 'Loading...')]));
  const publications = ['author:one', 'author:two', 'author:three', '', 'no-periodical'].map(id => {
    const publication = new Element('publication');
    publication.setAttribute('data-paper-id', id);
    if (id !== 'no-periodical') publication.appendChild(new Element('periodical'));
    return publication;
  });
  const listeners = [];
  const warnings = [];
  const timers = new Map();
  const requests = [];
  let timerId = 0;
  function citationSpans() {
    return publications.flatMap(publication => publication.children.flatMap(periodical => periodical.children));
  }
  const document = {
    readyState: options.readyState || 'loading',
    getElementById: id => options.noStats ? null : elements[id],
    createElement: kind => new Element(kind),
    querySelectorAll: selector => selector === '.publication-item' ? publications : citationSpans(),
    addEventListener: (type, callback, config) => listeners.push({ type, callback, config })
  };
  const context = vm.createContext({
    document,
    window: { location: { hostname: options.hostname || 'zwu88.github.io' } },
    AbortController,
    console: { warn: (...args) => warnings.push(args) },
    setTimeout: (callback, delay) => {
      timers.set(++timerId, { callback, delay });
      return timerId;
    },
    clearTimeout: id => timers.delete(id),
    fetch: async (url, config) => {
      requests.push({ url, config });
      if (options.fetch) return options.fetch(url, config);
      if (options.networkError) throw new Error('offline');
      return {
        ok: options.httpOk !== false,
        json: async () => {
          if (options.invalidJson) throw new SyntaxError('invalid JSON');
          return payload;
        }
      };
    }
  });
  vm.runInContext(source, context, { filename: 'scholar-stats.js' });
  return {
    elements, publications, listeners, warnings, timers, requests, citationSpans,
    load: () => listeners[0].callback(),
    display: () => Object.fromEntries(Object.entries(elements).map(([id, element]) => [id, element.textContent]))
  };
}

function assertUnavailable(page) {
  assert.deepEqual(page.display(), { citations: '—', 'h-index': '—', 'update-time': 'Unavailable' });
  assert.equal(page.citationSpans().length, 0);
  assert.equal(page.requests.length, 1);
  assert.equal(page.timers.size, 0);
  assert.equal(page.warnings.length, 1);
}

async function runTests() {
  let checked = 0;
  async function test(name, callback) {
    try { await callback(); checked++; }
    catch (error) { error.message = `${name}: ${error.message}`; throw error; }
  }

  await test('one fetch renders author and publication counts after DOM readiness', async () => {
    const page = harness(fixture());
    assert.equal(page.requests.length, 0);
    assert.equal(page.listeners.length, 1);
    assert.equal(page.listeners[0].type, 'DOMContentLoaded');
    assert.equal(page.listeners[0].config.once, true);
    await page.load();
    assert.equal(page.requests.length, 1);
    assert.equal(page.display().citations, '1,337');
    assert.equal(page.display()['h-index'], '9');
    assert.match(page.display()['update-time'], /Nov.*2026/);
    assert.deepEqual(page.citationSpans().map(span => span.textContent), [' • 0 citations', ' • 1,234 citations']);
    assert.equal(page.warnings.length, 0);
    assert.equal(page.timers.size, 0);
  });

  await test('genuine zero metrics are valid', async () => {
    const page = harness({ ...fixture(), citedby: 0, hindex: 0, publications: {} });
    await page.load();
    assert.equal(page.display().citations, '0');
    assert.equal(page.display()['h-index'], '0');
    assert.equal(page.warnings.length, 0);
  });

  await test('cached data retains its successful timestamp', async () => {
    const fresh = harness(fixture());
    await fresh.load();
    const data = { ...fixture(), status: 'cached', last_checked: '2027-01-01T00:00:00Z' };
    const page = harness(data);
    await page.load();
    assert.equal(page.display()['update-time'], fresh.display()['update-time'] + ' (cached)');
    assert.equal(data.updated, '2026-11-18T00:00:00Z');
    assert.equal(page.display().citations, '1,337');
  });

  await test('older cache message is identified as cached', async () => {
    const data = fixture();
    delete data.status;
    data.message = 'Using cached data - Google Scholar was unavailable during last update';
    const page = harness(data);
    await page.load();
    assert.match(page.display()['update-time'], / \(cached\)$/);
  });

  for (const updated of [undefined, null, '', 'not-a-date', 0]) {
    await test(`missing or invalid timestamp ${String(updated)}`, async () => {
      const page = harness({ ...fixture(), updated });
      await page.load();
      assert.equal(page.display()['update-time'], 'Unavailable');
      assert.equal(page.display().citations, '1,337');
    });
  }

  for (const data of [
    { ...fixture(), status: 'unavailable', citedby: 0, hindex: 0 },
    { ...fixture(), status: 'error' },
    { ...fixture(), message: 'Google Scholar data will be populated once publications are indexed' },
    { ...fixture(), name: '  test AUTHOR  ' },
    null,
    []
  ]) {
    await test('unavailable, fabricated and malformed payloads are rejected', async () => {
      const page = harness(data);
      await page.load();
      assertUnavailable(page);
    });
  }

  for (const value of [undefined, null, '12', -1, 1.5, NaN, Infinity]) {
    for (const field of ['citedby', 'hindex']) {
      await test(`invalid ${field} ${String(value)}`, async () => {
        const page = harness({ ...fixture(), [field]: value });
        await page.load();
        assertUnavailable(page);
      });
    }
  }

  for (const publications of [undefined, null, 'invalid', [null, { author_pub_id: 'author:one', num_citations: 2 }]]) {
    await test('missing publications and null publication entries do not break metrics', async () => {
      const page = harness({ ...fixture(), publications });
      await page.load();
      assert.equal(page.display().citations, '1,337');
      assert.equal(page.warnings.length, 0);
      assert.equal(page.citationSpans().length, Array.isArray(publications) ? 1 : 0);
    });
  }

  await test('invalid publication counts are omitted', async () => {
    for (const value of [null, undefined, '4', -1, 0.5]) {
      const page = harness({ ...fixture(), publications: { 'author:one': { num_citations: value } } });
      await page.load();
      assert.equal(page.citationSpans().length, 0);
      assert.equal(page.warnings.length, 0);
    }
  });

  for (const options of [{ networkError: true }, { httpOk: false }, { invalidJson: true }]) {
    await test('network, HTTP and JSON failures display unavailable', async () => {
      const page = harness(fixture(), options);
      await page.load();
      assertUnavailable(page);
    });
  }

  await test('slow requests abort and leave an unavailable display', async () => {
    const page = harness(fixture(), {
      fetch: (url, { signal }) => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true });
      })
    });
    const loading = page.load();
    assert.equal(page.timers.size, 1);
    const timeout = [...page.timers.values()][0];
    assert.equal(timeout.delay, 15000);
    timeout.callback();
    await loading;
    assertUnavailable(page);
  });

  await test('repeated rendering updates citation spans without duplicates or refetching', async () => {
    const data = fixture();
    const page = harness(data);
    await page.load();
    const firstSpan = page.citationSpans()[0];
    data.publications.p1.num_citations = 5;
    await page.load();
    assert.equal(page.requests.length, 1);
    assert.equal(page.citationSpans().length, 2);
    assert.equal(page.citationSpans()[0], firstSpan);
    assert.equal(firstSpan.textContent, ' • 5 citations');
    data.publications.p1.num_citations = null;
    await page.load();
    assert.equal(page.citationSpans().length, 1);
  });

  await test('localhost uses the same real data path', async () => {
    const page = harness(fixture(), { hostname: 'localhost' });
    await page.load();
    assert.equal(page.requests.length, 1);
    assert.equal(page.display().citations, '1,337');
    assert.notEqual(page.display()['update-time'], 'Loading...');
  });

  await test('loading after DOM readiness fetches immediately', async () => {
    const page = harness(fixture(), { readyState: 'complete' });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(page.listeners.length, 0);
    assert.equal(page.requests.length, 1);
    assert.equal(page.display().citations, '1,337');
  });

  await test('missing metric elements and publication metadata do not break rendering', async () => {
    const page = harness(fixture(), { noStats: true });
    await page.load();
    assert.equal(page.warnings.length, 0);
    assert.equal(page.citationSpans().length, 2);
  });

  console.log(`Passed ${checked} Scholar frontend checks.`);
  return { passed: checked };
}

module.exports = runTests;
if (require.main === module) {
  runTests().catch(error => { console.error(error); process.exitCode = 1; });
}
