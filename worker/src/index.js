function json(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...headers,
    },
  });
}

function constantTimeEqual(left, right) {
  if (typeof left !== "string" || typeof right !== "string") return false;
  const length = Math.max(left.length, right.length);
  let difference = left.length ^ right.length;
  for (let index = 0; index < length; index += 1) {
    difference |= (left.charCodeAt(index) || 0) ^ (right.charCodeAt(index) || 0);
  }
  return difference === 0;
}

function authorized(request, env, header, secretName) {
  const expected = env[secretName];
  const received = request.headers.get(header);
  return Boolean(expected && received && constantTimeEqual(received, expected));
}

function validFrameID(value) {
  return /^\d{8}T\d{4}Z$/.test(value);
}

async function signingKey(env) {
  if (!env.SATELLITE_TILE_SIGNING_SECRET) throw new Error("Satellite signing secret is not configured");
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(env.SATELLITE_TILE_SIGNING_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

function toBase64URL(bytes) {
  let binary = "";
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function storageKey(env, path) {
  const prefix = (env.SATELLITE_STORAGE_PREFIX || "").replace(/^\/+|\/+$/g, "");
  return prefix ? `${prefix}/${path}` : path;
}

function v2ManifestKey(env) {
  return storageKey(env, "manifest.json");
}

const V2_ZOOM_MIN = 1;
const V2_ZOOM_MAX = 5;
const PACK_MAGIC = "WXSP";
const PACK_VERSION = 3;
const PACK_HEADER_BYTES = 20;
const PACK_ENTRY_BYTES = 8;
const PACK_INDEX_COUNT = packTilesBelow(V2_ZOOM_MAX + 1);
const PACK_INDEX_BYTES = PACK_HEADER_BYTES + PACK_INDEX_COUNT * PACK_ENTRY_BYTES;
const packIndexCache = new Map();

function validPackRevision(value) {
  return typeof value === "string" && /^[a-z0-9]{1,16}$/.test(value);
}

function packKey(env, id, revision) {
  return storageKey(env, `packs/${id}-${revision}.pack`);
}

// Tiles in zooms V2_ZOOM_MIN..z-1.
function packTilesBelow(z) {
  return (4 ** z - 4 ** V2_ZOOM_MIN) / 3;
}

function packEntryIndex(z, x, y) {
  return packTilesBelow(z) + y * 2 ** z + x;
}

function validV2TileAddress(z, x, y) {
  if (!Number.isInteger(z) || z < V2_ZOOM_MIN || z > V2_ZOOM_MAX) return false;
  const count = 2 ** z;
  return Number.isInteger(x) && Number.isInteger(y) && x >= 0 && y >= 0 && x < count && y < count;
}

// Clients refresh every few minutes and frames arrive every 10, so one R2 read
// per colo per minute serves every client there.
const CLIENT_MANIFEST_CACHE_SECONDS = 60;

async function readClientV2Manifest(env, origin, ctx) {
  const cacheKey = new Request(`${origin}/v2/manifest-cache/${v2ManifestKey(env)}`);
  const hit = await caches.default.match(cacheKey);
  if (hit) return hit.json();
  const object = await env.SATELLITE_BUCKET.get(v2ManifestKey(env));
  if (!object) return null;
  const text = await object.text();
  const manifest = JSON.parse(text);
  ctx.waitUntil(caches.default.put(cacheKey, new Response(text, {
    headers: { "cache-control": `public, max-age=${CLIENT_MANIFEST_CACHE_SECONDS}` },
  })));
  return manifest;
}

async function v2TileSignature(env, frameID, revision, expires) {
  const key = await signingKey(env);
  const payload = new TextEncoder().encode(`satellite-tiles-v2\n${frameID}\n${revision}\n${expires}`);
  return toBase64URL(await crypto.subtle.sign("HMAC", key, payload));
}

async function validV2TileToken(request, env, frameID, revision) {
  const url = new URL(request.url);
  const expires = Number(url.searchParams.get("exp"));
  const signature = url.searchParams.get("sig");
  const now = Math.floor(Date.now() / 1000);
  if (!Number.isInteger(expires) || expires < now || expires > now + 3 * 3600 || !signature) return false;
  return constantTimeEqual(signature, await v2TileSignature(env, frameID, revision, expires));
}

function parsePackHeader(view) {
  if (view.byteLength < PACK_HEADER_BYTES) return null;
  const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
  const header = {
    magic,
    version: view.getUint32(4, true),
    minZoom: view.getUint32(8, true),
    maxZoom: view.getUint32(12, true),
    count: view.getUint32(16, true),
  };
  if (header.magic !== PACK_MAGIC || header.version !== PACK_VERSION || header.minZoom !== V2_ZOOM_MIN ||
      header.maxZoom !== V2_ZOOM_MAX || header.count !== PACK_INDEX_COUNT) return null;
  return header;
}

// Header and index in one R2 read (the index size is fixed), shared through
// the colo's cache so new isolates don't re-read it. Packs are immutable
// (a new revision gets a new key).
async function packIndex(env, key, origin, ctx) {
  const cached = packIndexCache.get(key);
  if (cached) return cached;
  const cacheKey = new Request(`${origin}/v2/pack-index/${key}`);
  let buffer;
  const hit = await caches.default.match(cacheKey);
  if (hit) {
    buffer = await hit.arrayBuffer();
  } else {
    const object = await env.SATELLITE_BUCKET.get(key, { range: { offset: 0, length: PACK_INDEX_BYTES } });
    if (!object) return null;
    buffer = await object.arrayBuffer();
  }
  if (buffer.byteLength !== PACK_INDEX_BYTES || !parsePackHeader(new DataView(buffer))) return null;
  if (!hit) {
    ctx.waitUntil(caches.default.put(cacheKey, new Response(buffer.slice(0), {
      headers: { "cache-control": "public, max-age=31536000, immutable" },
    })));
  }
  const index = { entries: new DataView(buffer, PACK_HEADER_BYTES) };
  packIndexCache.set(key, index);
  // Keep the isolate's cache small: 11 KB per z1-5 index.
  while (packIndexCache.size > 24) packIndexCache.delete(packIndexCache.keys().next().value);
  return index;
}

function clientAuthHeader(env) {
  return (env.SATELLITE_CLIENT_AUTH_HEADER || "X-Client-Auth").trim();
}

async function clientV2Manifest(request, env, ctx) {
  if (!authorized(request, env, clientAuthHeader(env), "SATELLITE_CLIENT_AUTH_SECRET")) {
    return json({ error: "Unauthorized" }, 403);
  }
  let manifest;
  try {
    manifest = await readClientV2Manifest(env, new URL(request.url).origin, ctx);
  } catch (error) {
    console.error("Satellite v2 manifest read failed", error);
    return json({ error: "Satellite manifest unavailable" }, 503);
  }
  if (!manifest || !Array.isArray(manifest.frames) || manifest.frames.length === 0) {
    return json({ error: "Satellite observations are not published" }, 503);
  }
  const expires = Math.floor(Date.now() / 3_600_000) * 3600 + 2 * 3600;
  const origin = new URL(request.url).origin;
  return json({
    ...manifest,
    frames: await Promise.all(manifest.frames.map(async (frame) => ({
      ...frame,
      tile_url_template: `${origin}/v2/tiles/${frame.id}/${frame.pack}/{z}/{x}/{y}.avif?exp=${expires}&sig=${await v2TileSignature(env, frame.id, frame.pack, expires)}`,
    }))),
  }, 200, { "cache-control": "private, max-age=30" });
}

async function readV2Tile(request, env, frameID, revision, zText, xText, yText, ctx) {
  const z = Number(zText), x = Number(xText), y = Number(yText);
  if (!validFrameID(frameID) || !validPackRevision(revision) || !validV2TileAddress(z, x, y)) {
    return new Response("Not found", { status: 404 });
  }
  if (!(await validV2TileToken(request, env, frameID, revision))) return new Response("Unauthorized", { status: 403 });

  const url = new URL(request.url);
  const cacheKey = new Request(`${url.origin}${url.pathname}`);
  const cache = caches.default;
  const cached = await cache.match(cacheKey);
  if (cached) return cached;

  const key = packKey(env, frameID, revision);
  const index = await packIndex(env, key, url.origin, ctx);
  if (!index) return new Response("Not found", { status: 404 });
  const entry = packEntryIndex(z, x, y) * PACK_ENTRY_BYTES;
  const offset = index.entries.getUint32(entry, true);
  const length = index.entries.getUint32(entry + 4, true);
  const headers = new Headers({
    "cache-control": "public, max-age=31536000, immutable",
    "access-control-allow-origin": "*",
    "x-content-type-options": "nosniff",
  });
  let response;
  if (length === 0) {
    // No satellite sees this tile (polar caps); an empty 204 is cached too.
    response = new Response(null, { status: 204, headers });
  } else {
    const object = await env.SATELLITE_BUCKET.get(key, { range: { offset, length } });
    if (!object) return new Response("Not found", { status: 404 });
    headers.set("content-type", "image/avif");
    response = new Response(object.body, { headers });
  }
  ctx.waitUntil(cache.put(cacheKey, response.clone()));
  return response;
}

async function routeV2(request, env, ctx, url) {
  const path = url.pathname;
  if (request.method === "GET" && path === "/v2/client-manifest") return clientV2Manifest(request, env, ctx);
  const tile = path.match(/^\/v2\/tiles\/(\d{8}T\d{4}Z)\/([a-z0-9]{1,16})\/(\d+)\/(\d+)\/(\d+)\.avif$/);
  if (request.method === "GET" && tile) return readV2Tile(request, env, ...tile.slice(1), ctx);
  return null;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: {
        "access-control-allow-origin": "*",
        "access-control-allow-methods": "GET, OPTIONS",
        "access-control-allow-headers": clientAuthHeader(env),
        "access-control-max-age": "86400",
      } });
    }
    if (request.method === "GET" && url.pathname === "/health") return json({ ok: true, service: "satellite-r2" });
    return (await routeV2(request, env, ctx, url)) || json({ error: "Not found" }, 404);
  },
};
