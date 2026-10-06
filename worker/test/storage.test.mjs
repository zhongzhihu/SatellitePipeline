import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const source = await readFile(new URL('../src/index.js', import.meta.url), 'utf8');
const { default: worker } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function memoryCache() {
  const store = new Map();
  globalThis.caches = { default: {
    async match(request) { return store.get(request.url)?.clone(); },
    async put(request, response) { store.set(request.url, response); },
  } };
  return store;
}

for (const [prefix, root] of [[undefined, ''], ['', ''], ['///', ''], ['/custom/path/', 'custom/path/']]) {
  test(`client manifest reads use literal storage prefix ${JSON.stringify(prefix)}`, async () => {
    memoryCache();
    const keys = [];
    const env = { SATELLITE_STORAGE_PREFIX: prefix, SATELLITE_CLIENT_AUTH_SECRET: 'client', SATELLITE_TILE_SIGNING_SECRET: 's', SATELLITE_BUCKET: {
      async get(key) { keys.push(key); return { text: async () => '{"frames":[{"id":"20261004T2200Z","pack":"abc123"}]}' }; },
    } };
    const response = await worker.fetch(new Request('https://example.com/v2/client-manifest', { headers: { 'X-Client-Auth': 'client' } }), env, { waitUntil() {} });
    assert.equal(response.status, 200);
    assert.deepEqual(keys, [`${root}manifest.json`]);
    delete globalThis.caches;
  });
}

test('publishing routes are not served', async () => {
  for (const [method, path] of [['GET', '/v2/publish/manifest'], ['PUT', '/v2/publish/manifest'], ['POST', '/v2/publish/packs/20261004T2200Z/abc123/uploads']]) {
    const response = await worker.fetch(new Request(`https://example.com${path}`, { method }), {}, {});
    assert.equal(response.status, 404);
  }
});

test('tile reads load header and index in one R2 read and share it through the cache', async () => {
  const id = '20261004T2200Z', pack = 'abc123', key = `packs/${id}-${pack}.pack`;
  const indexBytes = 20 + 1364 * 8, tile = new Uint8Array([7, 8, 9]);
  const head = new Uint8Array(indexBytes);
  head.set(new TextEncoder().encode('WXSP'));
  const view = new DataView(head.buffer);
  [3, 1, 5, 1364].forEach((value, i) => view.setUint32(4 + i * 4, value, true));
  view.setUint32(20, indexBytes, true); // z1/0/0
  view.setUint32(24, tile.length, true);
  const store = memoryCache(), reads = [], pending = [];
  const env = { SATELLITE_TILE_SIGNING_SECRET: 's', SATELLITE_BUCKET: {
    async get(k, { range }) {
      reads.push(range);
      const bytes = range.offset === 0 ? head.slice(0, range.length) : tile;
      return { arrayBuffer: async () => bytes.buffer, body: bytes, size: indexBytes + tile.length };
    },
  } };
  const ctx = { waitUntil(promise) { pending.push(promise); } };
  const manifestKey = 'satellite-tiles-v2';
  const exp = Math.floor(Date.now() / 1000) + 600;
  const signingKey = await crypto.subtle.importKey('raw', new TextEncoder().encode('s'), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sig = Buffer.from(await crypto.subtle.sign('HMAC', signingKey, new TextEncoder().encode(`${manifestKey}\n${id}\n${pack}\n${exp}`))).toString('base64url');
  const response = await worker.fetch(new Request(`https://example.com/v2/tiles/${id}/${pack}/1/0/0.avif?exp=${exp}&sig=${sig}`), env, ctx);
  assert.equal(response.status, 200);
  assert.deepEqual([...new Uint8Array(await response.arrayBuffer())], [7, 8, 9]);
  await Promise.all(pending);
  assert.deepEqual(reads, [{ offset: 0, length: indexBytes }, { offset: indexBytes, length: 3 }]);
  assert.ok(store.has(`https://example.com/v2/pack-index/${key}`));
  delete globalThis.caches;
});

test('client manifest is read from R2 once and then served from the colo cache', async () => {
  const store = memoryCache(), reads = [], pending = [];
  const manifest = { frames: [{ id: '20261004T2200Z', pack: 'abc123', valid_time: '2026-10-04T22:00:00Z' }] };
  const env = { SATELLITE_CLIENT_AUTH_SECRET: 'client', SATELLITE_TILE_SIGNING_SECRET: 's', SATELLITE_BUCKET: {
    async get(k) { reads.push(k); return { text: async () => JSON.stringify(manifest) }; },
  } };
  const ctx = { waitUntil(promise) { pending.push(promise); } };
  for (let i = 0; i < 2; i += 1) {
    const response = await worker.fetch(new Request('https://example.com/v2/client-manifest', { headers: { 'X-Client-Auth': 'client' } }), env, ctx);
    assert.equal(response.status, 200);
    assert.match((await response.json()).frames[0].tile_url_template, /\/v2\/tiles\/20261004T2200Z\/abc123\//);
    await Promise.all(pending);
  }
  assert.deepEqual(reads, ['manifest.json']);
  delete globalThis.caches;
});
