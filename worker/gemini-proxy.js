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
 * - POST models/auto:streamGenerateContent tries, in order: every Gemini model with every Gemini key,
 *   then Groq, then Cloudflare Workers AI, and answers from the first that has quota left.
 *
 * Setup: secrets GEMINI_API_KEY and ADMIN_CODE; KV namespace bound as LOGS (optional: without it,
 * everything works except the admin log).
 * Optional extra capacity: secrets GEMINI_API_KEY_2 ... GEMINI_API_KEY_5 (keys from other Google projects),
 * secret GROQ_API_KEY (free at console.groq.com), and a Workers AI binding named AI.
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
    pc: (cf.postalCode || "").slice(0, 10), isp: (cf.asOrganization || "").slice(0, 40),   // IP-based, so approximate
    la: cf.latitude ? Math.round(parseFloat(cf.latitude) * 100) / 100 : undefined,        // for the admin map (city level)
    lo: cf.longitude ? Math.round(parseFloat(cf.longitude) * 100) / 100 : undefined,
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

/* ------------------------------------------------------------------ automatic fallback chain
   Google's free tier allows ~20 requests a day per model, so one question may need several models.
   Order: every usable Gemini model -> Groq (secret GROQ_API_KEY, free) -> Cloudflare Workers AI (binding AI, free).
   Models that just said "quota used up" are skipped for a while so later questions go straight to one that works. */
const exhausted = new Map();   // model -> time until which it is skipped
const skip = (model, ms) => exhausted.set(model, Date.now() + ms);
const usable = model => (exhausted.get(model) || 0) < Date.now();
const GROQ_MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b", "llama-3.1-8b-instant"];
const CF_MODELS = ["@cf/meta/llama-3.3-70b-instruct-fp8-fast", "@cf/openai/gpt-oss-120b", "@cf/meta/llama-3.1-8b-instruct-fast"];

async function geminiOrder(env, ctx) {
  const cache = caches.default;
  const ck = new Request("https://faultlines-cache/models");
  let data;
  const hit = await cache.match(ck);
  if (hit) data = await hit.json();
  else {
    const r = await fetch(`${GOOGLE}/models?pageSize=200`, { headers: { "x-goog-api-key": env.GEMINI_API_KEY } });
    if (!r.ok) return ["gemini-3.6-flash", "gemini-3.5-flash-lite"];
    const text = await r.text();
    ctx.waitUntil(cache.put(ck, new Response(text, { headers: { "Cache-Control": "max-age=3600" } })));
    data = JSON.parse(text);
  }
  const names = (data.models || []).filter(m => (m.supportedGenerationMethods || []).includes("generateContent")).map(m => m.name.split("/").pop());
  const ver = n => parseFloat((n.match(/\d+(?:\.\d+)?/) || ["0"])[0]);
  const stable = n => !/(preview|exp)/.test(n);
  const bad = /(image|tts|audio|live|8b|omni|embed)/;
  const newest = (a, b) => (stable(b) - stable(a)) || (ver(b) - ver(a));
  const flash = names.filter(n => n.startsWith("gemini-") && n.includes("flash") && !n.includes("lite") && !bad.test(n) && ver(n) >= 3).sort(newest);
  const lite = names.filter(n => n.startsWith("gemini-") && n.includes("flash-lite") && !bad.test(n) && (ver(n) >= 3 || n.includes("latest"))).sort(newest);
  const gemma = names.filter(n => n.startsWith("gemma-") && !bad.test(n)).sort((a, b) => (ver(b) - ver(a)) || (b.includes("31b") - a.includes("31b")));
  return [...new Set(["gemini-3.6-flash", ...flash, ...gemma.slice(0, 1), ...lite, ...gemma.slice(1)])].filter(n => names.includes(n) || !names.length);
}

// one SSE event in Gemini's shape, so the website reads every provider the same way
const asSSE = text => `data: ${JSON.stringify({ candidates: [{ content: { role: "model", parts: [{ text }] }, finishReason: "STOP", index: 0 }] })}\n\n`;

function plainPrompt(body) {
  const sys = ((body.systemInstruction || {}).parts || []).map(p => p.text || "").join("\n");
  const user = (body.contents || []).map(c => (c.parts || []).map(p => p.text || "").join("\n")).join("\n\n");
  return (sys ? sys + "\n\n" : "") + user;
}

async function answerAuto(request, env, ctx, ip, kind) {
  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) return deny(413, "Request too large.");
  let body;
  try { body = JSON.parse(raw); } catch { return deny(400, "Invalid JSON."); }
  body.generationConfig = { ...(body.generationConfig || {}) };
  body.generationConfig.maxOutputTokens = Math.min(body.generationConfig.maxOutputTokens || MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS);
  delete body.tools; delete body.cachedContent;
  const wantsJson = body.generationConfig.responseMimeType === "application/json";
  const started = Date.now();
  const log = (status, model, cached) => ctx.waitUntil(logEvent(env, request, { kind, status, model, cached, ms: Date.now() - started }));

  // 1. answered in the last 24 h: from memory
  const cache = caches.default;
  const ck = new Request(`https://faultlines-cache/auto/${await sha256(JSON.stringify(body))}`);
  const cached = await cache.match(ck);
  if (cached) { log(200, "memory", true); return new Response(cached.body, { status: 200, headers: cors({ "Content-Type": "text/event-stream", "X-Faultlines-Cache": "hit" }) }); }

  // 2. fair use per visitor
  if (limited(`ai:${ip}`, VISITOR_LIMIT)) {
    log(429, "visitor-limit");
    return deny(429, "You've asked a lot in the last few minutes. Please wait a moment.", { "X-Faultlines-Limit": "visitor" });
  }
  const remember = text => ctx.waitUntil(cache.put(ck, new Response(text, { headers: { "Content-Type": "text/event-stream", "Cache-Control": `max-age=${CACHE_SECONDS}` } })));

  // 3. Gemini models, streamed; every extra key (GEMINI_API_KEY_2 ... _5, each from its own Google project)
  //    has its own daily allowance, so each model is tried with each key
  const keys = [env.GEMINI_API_KEY, env.GEMINI_API_KEY_2, env.GEMINI_API_KEY_3, env.GEMINI_API_KEY_4, env.GEMINI_API_KEY_5].filter(Boolean);
  const order = keys.length ? await geminiOrder(env, ctx) : [];
  for (const [ki, key] of keys.entries()) {
    for (const model of order) {
      const slot = `k${ki}:${model}`;
      if (!usable(slot)) continue;
      const b = JSON.parse(JSON.stringify(body));
      if (model.startsWith("gemma")) {   // Gemma: no system instruction or JSON mode
        b.contents = [{ role: "user", parts: [{ text: plainPrompt(body) }] }];
        delete b.systemInstruction; delete b.generationConfig.responseMimeType;
      }
      let r;
      try {
        r = await fetch(`${GOOGLE}/models/${model}:streamGenerateContent?alt=sse`, {
          method: "POST", headers: { "x-goog-api-key": key, "Content-Type": "application/json" }, body: JSON.stringify(b),
        });
      } catch { continue; }
      if (r.ok && r.body) {
        log(200, keys.length > 1 ? `${model} #${ki + 1}` : model);
        const [toClient, toCache] = r.body.tee();
        ctx.waitUntil(new Response(toCache).text().then(t => { if (t.length > 20) remember(t); }));
        return new Response(toClient, { status: 200, headers: cors({ "Content-Type": "text/event-stream", "X-Faultlines-Cache": "miss", "X-Faultlines-Provider": model }) });
      }
      if (r.status === 400 || r.status === 401 || r.status === 403) { if (!model.startsWith("gemma")) break; }   // bad key: next key
      skip(slot, r.status === 429 ? 30 * 60_000 : r.status === 503 ? 60_000 : 6 * 3600_000);   // quota / busy / unsupported
    }
  }

  const prompt = plainPrompt(body);
  const maxTokens = Math.min(body.generationConfig.maxOutputTokens, 8192);

  // 4. Groq (free account; OpenAI-compatible API)
  if (env.GROQ_API_KEY) {
    for (const model of GROQ_MODELS.filter(m => usable("groq:" + m))) {
      try {
        const r = await fetch("https://api.groq.com/openai/v1/chat/completions", {
          method: "POST",
          headers: { Authorization: `Bearer ${env.GROQ_API_KEY}`, "Content-Type": "application/json" },
          body: JSON.stringify({ model, messages: [{ role: "user", content: prompt }], max_tokens: maxTokens, temperature: 0.4,
                                 ...(wantsJson ? { response_format: { type: "json_object" } } : {}) }),
        });
        if (!r.ok) { skip("groq:" + model, r.status === 429 ? 15 * 60_000 : 6 * 3600_000); continue; }
        const d = await r.json();
        const text = (((d.choices || [])[0] || {}).message || {}).content || "";
        if (!text.trim()) continue;
        log(200, "groq:" + model);
        const sse = asSSE(text); remember(sse);
        return new Response(sse, { status: 200, headers: cors({ "Content-Type": "text/event-stream", "X-Faultlines-Provider": "groq:" + model }) });
      } catch { continue; }
    }
  }

  // 5. Cloudflare Workers AI (binding AI; free daily allowance)
  if (env.AI) {
    for (const model of CF_MODELS.filter(m => usable("cf:" + m))) {
      try {
        const res = await env.AI.run(model, { messages: [{ role: "user", content: prompt }], max_tokens: Math.min(maxTokens, 4096) });
        const text = typeof res === "string" ? res : typeof res.response === "string" ? res.response
          : ((((res.choices || [])[0] || {}).message || {}).content || "");
        if (!text || !String(text).trim()) continue;
        log(200, "cf:" + model.split("/").pop());
        const sse = asSSE(String(text)); remember(sse);
        return new Response(sse, { status: 200, headers: cors({ "Content-Type": "text/event-stream", "X-Faultlines-Provider": "cf:" + model }) });
      } catch { skip("cf:" + model, 15 * 60_000); continue; }
    }
  }

  log(429, "all-used-up");
  return deny(429, "All free AI providers are used up for the moment. Please try again later.");
}

/* ------------------------------------------------------------------ live feed (Google News RSS, free) */
const NEWS_LANG = {
  en: "hl=en-US&gl=US&ceid=US:en", fr: "hl=fr&gl=FR&ceid=FR:fr", es: "hl=es&gl=ES&ceid=ES:es", ar: "hl=ar&gl=EG&ceid=EG:ar",
};
const NEWS_QUERIES = {
  en: ["war OR attack OR missile OR drone OR strike", "sanctions OR ceasefire OR coup OR talks OR summit"],
  fr: ["guerre OR attaque OR missile OR frappe", "sanctions OR cessez-le-feu OR coup OR sommet"],
  es: ["guerra OR ataque OR misil OR bombardeo", "sanciones OR alto el fuego OR golpe OR cumbre"],
  ar: ["حرب OR هجوم OR صاروخ OR غارة", "عقوبات OR وقف إطلاق النار OR انقلاب OR قمة"],
};
const unxml = s => s.replace(/<!\[CDATA\[|\]\]>/g, "").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
  .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&#(\d+);/g, (_, n) => String.fromCharCode(+n)).trim();
function parseRss(xml){
  const out = [];
  for (const m of xml.matchAll(/<item>([\s\S]*?)<\/item>/g)){
    const b = m[1], g = tag => { const r = b.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`)); return r ? unxml(r[1]) : ""; };
    let t = g("title"); const s = g("source");
    if (s && t.endsWith(" - " + s)) t = t.slice(0, -(s.length + 3));
    const d = new Date(g("pubDate"));
    if (t) out.push({ t: t.slice(0, 300), u: g("link").slice(0, 2000), s: s.slice(0, 80), d: isNaN(d) ? null : d.toISOString() });
  }
  return out;
}
async function liveFeed(lang, ctx){
  if (!NEWS_LANG[lang]) lang = "en";
  const cache = caches.default, ck = new Request(`https://faultlines-cache/live/${lang}`);
  const hit = await cache.match(ck);
  if (hit) return new Response(hit.body, { status: 200, headers: cors({ "Content-Type": "application/json", "X-Faultlines-Cache": "hit" }) });
  const base = "https://news.google.com/rss", p = NEWS_LANG[lang];
  const urls = [`${base}/headlines/section/topic/WORLD?${p}`, ...NEWS_QUERIES[lang].map(q => `${base}/search?q=${encodeURIComponent(q + " when:1d")}&${p}`)];
  const lists = await Promise.all(urls.map(u => fetch(u, { headers: { "User-Agent": "Mozilla/5.0 (Faultlines live feed)" } })
    .then(r => r.ok ? r.text() : "").then(parseRss).catch(() => [])));
  const seen = new Set(), items = [];
  for (const it of lists.flat()){ const k = it.t.toLowerCase().slice(0, 80); if (!seen.has(k)){ seen.add(k); items.push(it); } }
  items.sort((a, b) => (b.d || "").localeCompare(a.d || ""));
  const body = JSON.stringify({ lang, generated: new Date().toISOString(), items: items.slice(0, 80) });
  if (items.length) ctx.waitUntil(cache.put(ck, new Response(body, { headers: { "Content-Type": "application/json", "Cache-Control": "max-age=600" } })));
  return new Response(body, { status: items.length ? 200 : 502, headers: cors({ "Content-Type": "application/json", "X-Faultlines-Cache": "miss" }) });
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

    // --- live feed: latest world headlines from Google News, cached 10 minutes per language ---
    if (path === "live") return liveFeed(url.searchParams.get("lang") || "en", ctx);

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

    // --- generate with automatic fallback: Gemini keys/models -> Groq -> Cloudflare Workers AI ---
    if (request.method === "POST" && path === "models/auto:streamGenerateContent") {
      return answerAuto(request, env, ctx, ip, kind);
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
    const m = path.match(/^models\/(gemini-[a-z0-9.\-]*flash[a-z0-9.\-]*|gemma-[a-z0-9.\-]+):(streamGenerateContent|generateContent)$/);
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
