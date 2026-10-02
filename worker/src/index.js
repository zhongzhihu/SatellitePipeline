const FRAME_LIMIT = 6;

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

function validTimestamp(value) {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
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

function v2Root(env) {
  const root = (env.SATELLITE_STORAGE_PREFIX || "satellite").replace(/^\/+|\/+$/g, "");
  return `${root}/v2`;
}

function v2ManifestKey(env) {
  return `${v2Root(env)}/manifest.json`;
}
// 1024 px tiles: tile zoom z draws at map zoom z + 1.
const V2_TILE_SIZE = 1024;
const V2_ZOOM_MIN = 1;
const V2_ZOOM_MAX = 5;
const PACK_MAGIC = "WXSP";
const PACK_VERSION = 3;
const PACK_HEADER_BYTES = 20;
const PACK_ENTRY_BYTES = 8;
const PACK_PART_MAX_BYTES = 96 * 1024 * 1024;
const PACK_MAX_BYTES = 1024 * 1024 * 1024;
const PACK_RETENTION_MS = 3 * 3600 * 1000;
const V2_ENCODING = {
  format: "avif",
  layout: "ir-over-vis",
  tile_width: V2_TILE_SIZE,
  tile_height: V2_TILE_SIZE * 2,
  nodata: 0,
  ir: { quantity: "brightness_temperature", unit: "K", code_min: 1, code_max: 255, value_at_code_min: 321.25, value_at_code_max: 182.75 },
  vis: { quantity: "reflectance", curve: "sqrt", code_min: 1, code_max: 255, value_at_code_min: 0, value_at_code_max: 1 },
};
const packIndexCache = new Map();

function publisherAuthorized(request, env) {
  return Boolean(env.SATELLITE_PUBLISHER_TOKEN) &&
    constantTimeEqual(request.headers.get("Authorization"), `Bearer ${env.SATELLITE_PUBLISHER_TOKEN}`);
}

function validPackRevision(value) {
  return typeof value === "string" && /^[a-z0-9]{1,16}$/.test(value);
}

function packKey(env, id, revision) {
  return `${v2Root(env)}/packs/${id}-${revision}.pack`;
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

async function readV2Manifest(env) {
  const object = await env.SATELLITE_BUCKET.get(v2ManifestKey(env));
  if (!object) return null;
  return JSON.parse(await object.text());
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

async function readPackHeader(env, key) {
  const object = await env.SATELLITE_BUCKET.get(key, { range: { offset: 0, length: PACK_HEADER_BYTES } });
  if (!object) return null;
  const view = new DataView(await object.arrayBuffer());
  if (view.byteLength !== PACK_HEADER_BYTES) return null;
  const magic = String.fromCharCode(view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3));
  const header = {
    magic,
    version: view.getUint32(4, true),
    minZoom: view.getUint32(8, true),
    maxZoom: view.getUint32(12, true),
    count: view.getUint32(16, true),
    size: object.size,
  };
  if (header.magic !== PACK_MAGIC || header.version !== PACK_VERSION || header.minZoom !== V2_ZOOM_MIN ||
      header.maxZoom !== V2_ZOOM_MAX || header.count !== packTilesBelow(V2_ZOOM_MAX + 1)) return null;
  return header;
}

async function packIndex(env, key) {
  const cached = packIndexCache.get(key);
  if (cached) return cached;
  const header = await readPackHeader(env, key);
  if (!header) return null;
  const object = await env.SATELLITE_BUCKET.get(key, {
    range: { offset: PACK_HEADER_BYTES, length: header.count * PACK_ENTRY_BYTES },
  });
  if (!object) return null;
  const index = { entries: new DataView(await object.arrayBuffer()) };
  packIndexCache.set(key, index);
  // Packs are immutable (a new revision gets a new key); keep the isolate's
  // cache small: 11 KB per z1-5 index.
  while (packIndexCache.size > 24) packIndexCache.delete(packIndexCache.keys().next().value);
  return index;
}

async function createPackUpload(request, env, id, revision) {
  if (!publisherAuthorized(request, env)) return json({ error: "Unauthorized" }, 403);
  if (!validFrameID(id) || !validPackRevision(revision)) return json({ error: "Invalid pack id" }, 400);
  const upload = await env.SATELLITE_BUCKET.createMultipartUpload(packKey(env, id, revision), {
    httpMetadata: { contentType: "application/octet-stream" },
  });
  return json({ key: upload.key, upload_id: upload.uploadId }, 201);
}

async function uploadPackPart(request, env, id, revision, partText) {
  if (!publisherAuthorized(request, env)) return json({ error: "Unauthorized" }, 403);
  const uploadID = new URL(request.url).searchParams.get("upload_id");
  const partNumber = Number(partText);
  if (!validFrameID(id) || !validPackRevision(revision) || !uploadID ||
      !Number.isInteger(partNumber) || partNumber < 1 || partNumber > 64) {
    return json({ error: "Invalid pack part" }, 400);
  }
  const length = Number(request.headers.get("content-length") || "0");
  if (!request.body || length < 1 || length > PACK_PART_MAX_BYTES) return json({ error: "Invalid part size" }, 413);
  const upload = env.SATELLITE_BUCKET.resumeMultipartUpload(packKey(env, id, revision), uploadID);
  const part = await upload.uploadPart(partNumber, request.body);
  return json({ part_number: part.partNumber, etag: part.etag }, 201);
}

async function completePackUpload(request, env, id, revision) {
  if (!publisherAuthorized(request, env)) return json({ error: "Unauthorized" }, 403);
  const uploadID = new URL(request.url).searchParams.get("upload_id");
  if (!validFrameID(id) || !validPackRevision(revision) || !uploadID) return json({ error: "Invalid pack" }, 400);
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "Invalid JSON" }, 400);
  }
  const parts = Array.isArray(body?.parts) ? body.parts : [];
  if (!parts.length || parts.length > 64 || parts.some((part) =>
    !Number.isInteger(part?.part_number) || typeof part?.etag !== "string")) {
    return json({ error: "Invalid part list" }, 400);
  }
  const key = packKey(env, id, revision);
  const upload = env.SATELLITE_BUCKET.resumeMultipartUpload(key, uploadID);
  const object = await upload.complete(parts.map((part) => ({ partNumber: part.part_number, etag: part.etag })));
  const header = await readPackHeader(env, key);
  if (!header || header.size !== body.bytes || header.size > PACK_MAX_BYTES) {
    await env.SATELLITE_BUCKET.delete(key);
    return json({ error: "Uploaded pack is invalid" }, 400);
  }
  return json({ key, bytes: object.size }, 201);
}

async function abortPackUpload(request, env, id, revision) {
  if (!publisherAuthorized(request, env)) return json({ error: "Unauthorized" }, 403);
  const uploadID = new URL(request.url).searchParams.get("upload_id");
  if (!validFrameID(id) || !validPackRevision(revision) || !uploadID) return json({ error: "Invalid pack" }, 400);
  await env.SATELLITE_BUCKET.resumeMultipartUpload(packKey(env, id, revision), uploadID).abort();
  return new Response(null, { status: 204 });
}

function validateV2Frames(frames) {
  if (!Array.isArray(frames) || frames.length < 1 || frames.length > FRAME_LIMIT) {
    return `Manifest must contain between 1 and ${FRAME_LIMIT} frames`;
  }
  for (const frame of frames) {
    if (!validFrameID(frame?.id) || !validTimestamp(frame?.valid_time) || !validPackRevision(frame?.pack)) {
      return "Invalid frame metadata";
    }
    const date = new Date(frame.valid_time);
    if (`${date.toISOString().slice(0, 16).replace(/[-:]/g, "")}Z` !== frame.id) return "Frame id and timestamp differ";
    if (date.getUTCMinutes() % 10 || date.getUTCSeconds() || date.getUTCMilliseconds()) {
      return "Frame times must align to a 10-minute boundary";
    }
  }
  const ordered = [...frames].sort((left, right) => Date.parse(left.valid_time) - Date.parse(right.valid_time));
  if (new Set(ordered.map((frame) => frame.id)).size !== ordered.length) return "Frame ids must be unique";
  if (ordered.some((frame, index) => index > 0 &&
      Date.parse(frame.valid_time) - Date.parse(ordered[index - 1].valid_time) !== 10 * 60 * 1000)) {
    return "Frame times must be consecutive 10-minute steps";
  }
  return null;
}

async function deleteStalePacks(env, keep) {
  const cutoff = Date.now() - PACK_RETENTION_MS;
  let cursor;
  do {
    const listing = await env.SATELLITE_BUCKET.list({ prefix: `${v2Root(env)}/packs/`, cursor });
    const stale = listing.objects
      .filter((object) => !keep.has(object.key) && object.uploaded.getTime() < cutoff)
      .map((object) => object.key);
    if (stale.length) await env.SATELLITE_BUCKET.delete(stale);
    cursor = listing.truncated ? listing.cursor : undefined;
  } while (cursor);
}

async function publishV2Manifest(request, env, ctx) {
  if (!publisherAuthorized(request, env)) return json({ error: "Unauthorized" }, 403);
  if (Number(request.headers.get("content-length") || "0") > 64 * 1024) {
    return json({ error: "Manifest is too large" }, 413);
  }
  let incoming;
  try {
    incoming = await request.json();
  } catch {
    return json({ error: "Invalid JSON" }, 400);
  }
  if (incoming?.schema_version !== 2 || incoming?.crs !== "EPSG:3857" || incoming?.tile_size !== V2_TILE_SIZE ||
      incoming?.minimum_zoom_level !== V2_ZOOM_MIN || incoming?.maximum_zoom_level !== V2_ZOOM_MAX ||
      JSON.stringify(incoming?.encoding) !== JSON.stringify(V2_ENCODING)) {
    return json({ error: "Unsupported satellite manifest" }, 400);
  }
  const frameError = validateV2Frames(incoming.frames);
  if (frameError) return json({ error: frameError }, 400);
  const ordered = [...incoming.frames].sort((left, right) => Date.parse(left.valid_time) - Date.parse(right.valid_time));
  const headers = await Promise.all(ordered.map((frame) => readPackHeader(env, packKey(env, frame.id, frame.pack))));
  if (headers.some((header) => !header)) {
    return json({ error: "Frame packs are missing or invalid" }, 409);
  }

  const previous = await readV2Manifest(env).catch(() => null);
  // Overlapping publisher runs finish out of order; a slow run must not roll
  // the timeline back. Deliberate backfills of older frames pass allow_rewind.
  const previousLatest = Math.max(...(previous?.frames || []).map((frame) => Date.parse(frame?.valid_time) || 0), 0);
  if (previous?.minimum_zoom_level === V2_ZOOM_MIN && previous?.maximum_zoom_level === V2_ZOOM_MAX &&
      Date.parse(ordered[ordered.length - 1].valid_time) < previousLatest &&
      new URL(request.url).searchParams.get("allow_rewind") !== "1") {
    return json({ error: "Manifest is older than the published timeline" }, 409);
  }
  const manifest = {
    schema_version: 2,
    generated_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
    product: incoming.product,
    crs: "EPSG:3857",
    cadence_minutes: 10,
    minimum_zoom_level: V2_ZOOM_MIN,
    maximum_zoom_level: V2_ZOOM_MAX,
    tile_size: V2_TILE_SIZE,
    encoding: V2_ENCODING,
    frames: ordered.map(({ id, valid_time, pack, source_summary }, index) => ({
      id, valid_time, pack, pack_bytes: headers[index].size, source_summary,
    })),
  };
  await env.SATELLITE_BUCKET.put(v2ManifestKey(env), JSON.stringify(manifest), {
    httpMetadata: { contentType: "application/json; charset=utf-8", cacheControl: "no-store" },
  });
  const keep = new Set([...manifest.frames, ...(previous?.frames || [])]
    .filter((frame) => validFrameID(frame?.id) && validPackRevision(frame?.pack))
    .map((frame) => packKey(env, frame.id, frame.pack)));
  ctx.waitUntil(deleteStalePacks(env, keep).catch((error) => console.error("Pack cleanup failed", error)));
  return json({ published: true, frame_count: manifest.frames.length, generated_at: manifest.generated_at });
}

async function readPublishedV2Manifest(request, env) {
  if (!publisherAuthorized(request, env)) return json({ error: "Unauthorized" }, 403);
  const manifest = await readV2Manifest(env);
  return manifest ? json(manifest) : json({ error: "No satellite manifest" }, 404);
}

function clientAuthHeader(env) {
  return (env.SATELLITE_CLIENT_AUTH_HEADER || "X-Vista-Weather").trim();
}

async function clientV2Manifest(request, env) {
  if (!authorized(request, env, clientAuthHeader(env), "SATELLITE_CLIENT_AUTH_SECRET")) {
    return json({ error: "Unauthorized" }, 403);
  }
  let manifest;
  try {
    manifest = await readV2Manifest(env);
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
  const index = await packIndex(env, key);
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
  if (request.method === "GET" && path === "/v2/client-manifest") return clientV2Manifest(request, env);
  if (request.method === "GET" && path === "/v2/publish/manifest") return readPublishedV2Manifest(request, env);
  if (request.method === "PUT" && path === "/v2/publish/manifest") return publishV2Manifest(request, env, ctx);
  const pack = path.match(/^\/v2\/publish\/packs\/(\d{8}T\d{4}Z)\/([a-z0-9]{1,16})\/(uploads|parts\/(\d+)|complete|abort)$/);
  if (pack) {
    const [, id, revision, action, part] = pack;
    if (request.method === "POST" && action === "uploads") return createPackUpload(request, env, id, revision);
    if (request.method === "PUT" && part) return uploadPackPart(request, env, id, revision, part);
    if (request.method === "POST" && action === "complete") return completePackUpload(request, env, id, revision);
    if (request.method === "POST" && action === "abort") return abortPackUpload(request, env, id, revision);
  }
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
        "access-control-allow-methods": "GET, PUT, POST, OPTIONS",
        "access-control-allow-headers": `Authorization, Content-Type, ${clientAuthHeader(env)}`,
        "access-control-max-age": "86400",
      } });
    }
    if (request.method === "GET" && url.pathname === "/health") return json({ ok: true, service: "satellite-r2" });
    return (await routeV2(request, env, ctx, url)) || json({ error: "Not found" }, 404);
  },
};
