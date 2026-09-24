/**
 * Faultlines AI proxy (Cloudflare Worker, free plan).
 *
 * - Holds the Gemini key as a Worker secret (GEMINI_API_KEY): the website offers AI answers
 *   on every device without anyone typing a key, and the key never appears in the page.
 * - Only serves the site's own address, only Gemini Flash models, caps answer length.
 * - Remembers answers for 24 hours (Cloudflare cache), so repeated questions cost no quota.
 * - Limits each visitor to a fair number of AI requests, so one person can't use up the free quota.
 * - Logs usage (time, country/city, device, kind, cache hit, status) to a KV namespace bound as LOGS,
 *   readable only with the admin code (Worker secret ADMIN_CODE).
 *
 * Setup: secrets GEMINI_API_KEY and ADMIN_CODE; KV namespace bound as LOGS (optional: without it,
 * everything works except the admin log).
 */
const ALLOWED_ORIGIN = "https://omarezz0709-gif.github.io";
const GOOGLE = "https://generativelanguage.googleapis.com/v1beta";
const MAX_OUTPUT_TOKENS = 8192;
const MAX_BODY_BYTES = 200_000;
const CACHE_SECONDS = 24 * 3600;
const LOG_DAYS = 30;
const VISITOR_LIMIT = { max: 12, windowMs: 5 * 60_000 };      // AI requests per visitor per 5 minutes
const ADMIN_LIMIT = { max: 5, windowMs: 15 * 60_000 };        // wrong admin codes per visitor per 15 minutes

const hits = new Map();        // in-memory counters (per Worker instance; good enough to stop floods)
function limited(key, { max, windowMs }) {
  const now = Date.now();
  const list = (hits.get(key) || []).filter(t => now - t < windowMs);
  if (list.length >= max) { hits.set(key, list); return true; }
  list.push(now); hits.set(key, list);
  if (hits.size > 5000) hits.clear();
  return false;
}

function cors(extra = {}) {
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Faultlines-Kind, X-Admin-Code",
    "Access-Control-Expose-Headers": "X-Faultlines-Cache, X-Faultlines-Limit",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
    ...extra,
  };
}
const json = (status, obj, extra = {}) =>
  new Response(JSON.stringify(obj), { status, headers: cors({ "Content-Type": "application/json", ...extra }) });
const deny = (status, message, extra) => json(status, { error: { code: status, message } }, extra);

async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function device(ua) {
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad" : /Android/.test(ua) ? "Android"
    : /Windows/.test(ua) ? "Windows" : /Mac OS X|Macintosh/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux" : "Other";
  const br = /Edg\//.test(ua) ? "Edge" : /OPR\//.test(ua) ? "Opera" : /Firefox\//.test(ua) ? "Firefox"
    : /CriOS|Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Other";
  return `${os} · ${br}`;
}

async function logEvent(env, request, rec) {
  if (!env.LOGS) return;
  const cf = request.cf || {};
  const ip = request.headers.get("CF-Connecting-IP") || "";
  const day = new Date().toISOString().slice(0, 10);
  const meta = {
    t: Date.now(), k: rec.kind || "?", s: rec.status || 0, c: rec.cached ? 1 : 0, m: (rec.model || "").replace("gemini-", ""),
    co: cf.country || "", ci: (cf.city || "").slice(0, 40), re: (cf.region || "").slice(0, 40),
    d: device(request.headers.get("User-Agent") || ""), v: (await sha256(ip + day)).slice(0, 8),   // visitor id, rotates daily
    ms: rec.ms || 0,
  };
  const key = `l:${String(9999999999999 - meta.t).padStart(13, "0")}:${Math.random().toString(36).slice(2, 7)}`;
  try { await env.LOGS.put(key, "", { metadata: meta, expirationTtl: LOG_DAYS * 86400 }); } catch (e) { /* free KV write limit reached */ }
}

async function readLogs(env, max = 3000) {
  const out = [];
  let cursor;
  do {
    const page = await env.LOGS.list({ prefix: "l:", limit: 1000, cursor });
    for (const k of page.keys) if (k.metadata) out.push(k.metadata);
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor && out.length < max);
  return out;
}

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors() });
    if (origin !== ALLOWED_ORIGIN) return deny(403, "This AI proxy only serves the Faultlines site.");

    const url = new URL(request.url);
    const path = url.pathname.replace(/^\/+/, "");
    const ip = request.headers.get("CF-Connecting-IP") || "?";
    const kind = (request.headers.get("X-Faultlines-Kind") || "ai").slice(0, 20);

    // --- visit ping (one per browser session) ---
    if (path === "hit") {
      ctx.waitUntil(logEvent(env, request, { kind: "visit", status: 200 }));
      return new Response(null, { status: 204, headers: cors() });
    }

    // --- admin: usage log ---
    if (path === "admin/logs") {
      if (!env.ADMIN_CODE) return deny(500, "ADMIN_CODE secret is not set on the Worker.");
      const code = request.headers.get("X-Admin-Code") || "";
      const wrongKey = `adminfail:${ip}`;
      const fails = (hits.get(wrongKey) || []).filter(t => Date.now() - t < ADMIN_LIMIT.windowMs);
      if (fails.length >= ADMIN_LIMIT.max) return deny(429, "Too many wrong codes. Try again in 15 minutes.");
      if (code !== env.ADMIN_CODE) { fails.push(Date.now()); hits.set(wrongKey, fails); return deny(401, "Wrong code."); }
      if (!env.LOGS) return json(200, { logs: [], note: "No KV namespace bound as LOGS, so nothing is being logged yet." });
      return json(200, { logs: await readLogs(env), generated: new Date().toISOString() }, { "Cache-Control": "no-store" });
    }

    if (!env.GEMINI_API_KEY) return deny(500, "The GEMINI_API_KEY secret is not set on this Worker.");

    // --- list models ---
    if (request.method === "GET" && path === "models") {
      const cache = caches.default;
      const ck = new Request("https://faultlines-cache/models");
      const hit = await cache.match(ck);
      if (hit) return new Response(hit.body, { status: 200, headers: cors({ "Content-Type": "application/json" }) });
      const r = await fetch(`${GOOGLE}/models?pageSize=200`, { headers: { "x-goog-api-key": env.GEMINI_API_KEY } });
      const body = await r.text();
      if (r.ok) ctx.waitUntil(cache.put(ck, new Response(body, { headers: { "Cache-Control": "max-age=3600" } })));
      return new Response(body, { status: r.status, headers: cors({ "Content-Type": "application/json" }) });
    }

    // --- generate: models/<flash model>:streamGenerateContent or :generateContent ---
    const m = path.match(/^models\/(gemini-[a-z0-9.\-]*flash[a-z0-9.\-]*):(streamGenerateContent|generateContent)$/);
    if (request.method !== "POST" || !m) return deny(404, "Unsupported request.");
    const raw = await request.text();
    if (raw.length > MAX_BODY_BYTES) return deny(413, "Request too large.");
    let body;
    try { body = JSON.parse(raw); } catch { return deny(400, "Invalid JSON."); }
    body.generationConfig = { ...(body.generationConfig || {}) };
    body.generationConfig.maxOutputTokens = Math.min(body.generationConfig.maxOutputTokens || MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS);
    delete body.tools;
    delete body.cachedContent;
    const stream = m[2] === "streamGenerateContent";
    const started = Date.now();

    // 1. the same question was answered in the last 24 h: serve it from the cache (no quota used)
    const cache = caches.default;
    const ck = new Request(`https://faultlines-cache/${m[2]}/${await sha256(JSON.stringify(body))}`);
    const cached = await cache.match(ck);
    if (cached) {
      ctx.waitUntil(logEvent(env, request, { kind, status: 200, cached: true, model: m[1], ms: Date.now() - started }));
      return new Response(cached.body, { status: 200, headers: cors({ "Content-Type": cached.headers.get("Content-Type") || "application/json", "X-Faultlines-Cache": "hit" }) });
    }

    // 2. fair use per visitor
    if (limited(`ai:${ip}`, VISITOR_LIMIT)) {
      ctx.waitUntil(logEvent(env, request, { kind, status: 429, model: m[1] }));
      return deny(429, "You've asked a lot in the last few minutes. Please wait a moment.", { "X-Faultlines-Limit": "visitor" });
    }

    // 3. ask Gemini, stream the answer back and keep a copy in the cache
    const r = await fetch(`${GOOGLE}/models/${m[1]}:${m[2]}${stream ? "?alt=sse" : ""}`, {
      method: "POST",
      headers: { "x-goog-api-key": env.GEMINI_API_KEY, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const type = r.headers.get("Content-Type") || (stream ? "text/event-stream" : "application/json");
    ctx.waitUntil(logEvent(env, request, { kind, status: r.status, model: m[1], ms: Date.now() - started }));
    if (!r.ok || !r.body) {
      return new Response(r.body, { status: r.status, headers: cors({ "Content-Type": type, "Cache-Control": "no-store" }) });
    }
    const [toClient, toCache] = r.body.tee();
    ctx.waitUntil((async () => {
      const text = await new Response(toCache).text();
      if (text.length > 20) {
        await cache.put(ck, new Response(text, { headers: { "Content-Type": type, "Cache-Control": `max-age=${CACHE_SECONDS}` } }));
      }
    })());
    return new Response(toClient, { status: 200, headers: cors({ "Content-Type": type, "Cache-Control": "no-store", "X-Faultlines-Cache": "miss" }) });
  },
};
