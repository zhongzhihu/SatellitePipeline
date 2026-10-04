import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const source = await readFile(new URL('../src/index.js', import.meta.url), 'utf8');
const { default: worker, V2_ENCODING: encoding } = await import(`data:text/javascript;base64,${Buffer.from(source + "\nexport { V2_ENCODING };").toString('base64')}`);

for (const [prefix, root] of [[undefined, ''], ['', ''], ['///', ''], ['/custom/path/', 'custom/path/']]) {
  test(`manifest reads and multipart uploads use literal storage prefix ${JSON.stringify(prefix)}`, async () => {
    const keys = [];
    const env = {
      SATELLITE_STORAGE_PREFIX: prefix,
      SATELLITE_PUBLISHER_TOKEN: 'test',
      SATELLITE_BUCKET: {
        async get(key) { keys.push(key); return { text: async () => '{"frames":[]}' }; },
        async createMultipartUpload(key) { keys.push(key); return { key, uploadId: 'upload' }; },
      },
    };
    const headers = { Authorization: 'Bearer test' };
    const manifest = await worker.fetch(new Request('https://example.com/v2/publish/manifest', { headers }), env, {});
    assert.equal(manifest.status, 200);
    const upload = await worker.fetch(new Request('https://example.com/v2/publish/packs/20261004T2200Z/abc123/uploads', { method: 'POST', headers }), env, {});
    assert.equal(upload.status, 201);
    assert.deepEqual(keys, [`${root}manifest.json`, `${root}packs/20261004T2200Z-abc123.pack`]);
    assert.equal((await upload.json()).key, keys[1]);
  });
}

test('retention scans root packs and preserves referenced packs', async () => {
  // Exercise publication, header validation, and asynchronous retention together.
  const id = '20261004T2200Z', pack = 'abc123', key = `packs/${id}-${pack}.pack`;
  const header = new Uint8Array(20);
  header.set(new TextEncoder().encode('WXSP'));
  const view = new DataView(header.buffer);
  [3, 1, 5, 1364].forEach((value, i) => view.setUint32(4 + i * 4, value, true));
  const reads = [], writes = [], lists = [], deletes = [], pending = [];
  const manifest = { schema_version: 2, crs: 'EPSG:3857', tile_size: 1024, minimum_zoom_level: 1, maximum_zoom_level: 5, encoding, frames: [{ id, pack, valid_time: '2026-10-04T22:00:00Z' }] };
  const env = { SATELLITE_PUBLISHER_TOKEN: 'test', SATELLITE_BUCKET: {
    async get(k) { reads.push(k); return k === key ? { arrayBuffer: async () => header.buffer, size: 20000 } : null; },
    async put(k) { writes.push(k); },
    async list(options) { lists.push(options.prefix); return { objects: [key, 'packs/obsolete.pack'].map(k => ({ key: k, uploaded: new Date(0) })), truncated: false }; },
    async delete(keys) { deletes.push(...keys); },
  } };
  const response = await worker.fetch(new Request('https://example.com/v2/publish/manifest', { method: 'PUT', headers: { Authorization: 'Bearer test' }, body: JSON.stringify(manifest) }), env, { waitUntil(promise) { pending.push(promise); } });
  assert.equal(response.status, 200, await response.text());
  await Promise.all(pending);
  assert.deepEqual(reads, [key, 'manifest.json']);
  assert.deepEqual(writes, ['manifest.json']);
  assert.deepEqual(lists, ['packs/']);
  assert.deepEqual(deletes, ['packs/obsolete.pack']);
});
