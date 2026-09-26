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
const VISITOR_LIMIT = { max: 12, windowMs: 5 * 60_000 };      // AI requests per visitor per 5 minutes
const ADMIN_LIMIT = { max: 5, windowMs: 15 * 60_000 };        // (older per-visitor limit, kept for reference)
const ADMIN_MAX_TRIES = 5;                                     // wrong admin codes in total before the login locks for everyone
const BACKUP_LIMIT = { max: 5, windowMs: 60 * 60_000 };       // wrong backup codes per visitor per hour
// the admin lock lives in KV (survives restarts, shared by all Cloudflare locations); memory is the fallback
let memLock = { fails: 0, locked: false };
async function getAdminLock(env){
  if (!env.LOGS) return memLock;
  try { return (await env.LOGS.get("sec:admin", "json")) || { fails: 0, locked: false }; } catch { return memLock; }
}
async function setAdminLock(env, v){
  memLock = v;
  if (env.LOGS) try { await env.LOGS.put("sec:admin", JSON.stringify(v)); } catch {}
}
// compares two codes without leaking how many characters matched (both are hashed first)
async function sameSecret(given, real){
  if (!given || !real) return false;
  const [a, b] = await Promise.all([sha256("fl|" + given), sha256("fl|" + real)]);
  let d = 0; for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0 && a.length === b.length;
}

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
    "Access-Control-Allow-Headers": "Content-Type, X-Faultlines-Kind, X-Admin-Code, X-Backup-Code",
    "Access-Control-Expose-Headers": "X-Faultlines-Cache, X-Faultlines-Limit, X-Faultlines-Provider, X-Faultlines-Search, X-Faultlines-Locked, X-Faultlines-Tries",
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

// crawlers, link-preview fetchers and automated browsers: marked as bots in the admin log
const BOT_UA = /bot|crawl|spider|slurp|preview|facebookexternalhit|whatsapp|telegram|discord|slack|headless|lighthouse|pagespeed|phantom|puppeteer|playwright|selenium|python|curl|wget|go-http|java\/|node-fetch|axios/i;
const DATACENTER = /google|amazon|aws|microsoft|azure|cloudflare|digitalocean|ovh|hetzner|linode|akamai|vultr|oracle|alibaba|tencent|contabo|leaseweb|scaleway|choopa|m247|datacamp|fastly|meta platforms|facebook/i;
function isBot(request){
  const ua = request.headers.get("User-Agent") || "", isp = (request.cf && request.cf.asOrganization) || "";
  return BOT_UA.test(ua) || DATACENTER.test(isp) || !ua;
}

// the log resets every night at 00:00 Berlin time: each entry expires at the next Berlin midnight
const berlinWall = ts => {   // milliseconds since Berlin midnight, by the wall clock
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Berlin", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" })
    .formatToParts(new Date(ts)).map(x => [x.type, x.value]));
  return ((+p.hour * 60 + +p.minute) * 60 + +p.second) * 1000 + (ts % 1000);
};
function berlinMidnightBefore(ts){
  let g = ts - berlinWall(ts);
  const w = berlinWall(g);                              // on clock-change days the first guess is an hour off
  return w === 0 ? g : w > 12 * 3600e3 ? g + (24 * 3600e3 - w) : g - w;
}
// +26 h always lands on the next Berlin day (also on 23/25-hour daylight-saving days)
const nextBerlinMidnight = ts => berlinMidnightBefore(berlinMidnightBefore(ts) + 26 * 3600e3);

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
    b: isBot(request) ? 1 : undefined,                                                    // crawler / preview / data-centre visit
    o: (request.headers.get("Origin") || "").replace(/^https:\/\//, "").slice(0, 40),       // which site address (GitHub or Cloudflare Pages)
  };
  const key = `l:${String(9999999999999 - meta.t).padStart(13, "0")}:${Math.random().toString(36).slice(2, 7)}`;
  // expires at the next 00:00 Berlin (KV needs at least 60 s ahead)
  // kept until 01:00 after the next midnight, so the nightly archive (00:10) can still copy the finished day;
  // the admin panel itself only shows entries since 00:00
  const expiration = Math.max(Math.floor((nextBerlinMidnight(meta.t) + 3600e3) / 1000), Math.floor(meta.t / 1000) + 60);
  try { await env.LOGS.put(key, "", { metadata: meta, expiration }); } catch (e) { /* free KV write limit reached */ }
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

/* ------------------------------------------------------------------ daily archive (backup)
   A Cron Trigger shortly after 00:00 Berlin saves the finished day as ONE KV entry "a:YYYY-MM-DD" (one write a day),
   kept for a year. The admin panel can open any archived day. */
const ARCHIVE_DAYS = 365;
const berlinDay = ts => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(ts));
async function archiveYesterday(env){
  if (!env.LOGS) return "no LOGS binding";
  const end = berlinMidnightBefore(Date.now()), start = berlinMidnightBefore(end - 3600e3), day = berlinDay(start);
  const entries = (await readLogs(env, 20000)).filter(x => x.t >= start && x.t < end).sort((a, b) => a.t - b.t);
  const key = `a:${day}`;
  // both summer/winter cron times may fire: never replace an archive with a smaller one (entries may have expired meanwhile)
  const old = await env.LOGS.get(key, "json").catch(() => null);
  if (old && (old.entries || []).length >= entries.length) return `${day}: kept existing archive (${old.entries.length})`;
  await env.LOGS.put(key, JSON.stringify({ day, saved: new Date().toISOString(), entries }), { expirationTtl: ARCHIVE_DAYS * 86400,
    metadata: { n: entries.length, visits: entries.filter(x => x.k === "visit" && !x.b).length } });
  return `${day}: archived ${entries.length}`;
}
async function listArchive(env){
  const out = []; let cursor;
  do {
    const page = await env.LOGS.list({ prefix: "a:", limit: 1000, cursor });
    for (const k of page.keys) out.push({ day: k.name.slice(2), n: (k.metadata || {}).n || 0, visits: (k.metadata || {}).visits || 0 });
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  return out.sort((a, b) => b.day.localeCompare(a.day));
}

/* ------------------------------------------------------------------ automatic fallback chain
   Google's free tier allows ~20 requests a day per model, so one question may need several models.
   Order: every usable Gemini model -> Groq (secret GROQ_API_KEY, free) -> Cloudflare Workers AI (binding AI, free).
   Models that just said "quota used up" are skipped for a while so later questions go straight to one that works. */
const exhausted = new Map();   // model -> time until which it is skipped
let lastSearchRefusal = "";    // why Google last refused a Google Search request (shown in a response header)
// models whose free plan includes Google Search (the newest Flash models refuse it without billing)
const SEARCH_MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite"];
const skip = (model, ms) => exhausted.set(model, Date.now() + ms);
const usable = model => (exhausted.get(model) || 0) < Date.now();
// (the small 8B models were dropped: they invent facts, and a "busy, try again" is better than a wrong answer)
const GROQ_MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b"];
const CF_MODELS = ["@cf/openai/gpt-oss-120b", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"];

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
  const ck = new Request(`https://faultlines-cache/auto2/${await sha256(JSON.stringify(body))}`);   // auto2: answers since Google Search was added
  // the private chat (opened with the backup code) is never kept: no cache, nothing stored
  const keep = kind !== "private";
  const cached = keep ? await cache.match(ck) : null;
  if (cached) { log(200, "memory", true); return new Response(cached.body, { status: 200, headers: cors({ "Content-Type": "text/event-stream", "X-Faultlines-Cache": "hit" }) }); }

  // 2. fair use per visitor
  if (limited(`ai:${ip}`, VISITOR_LIMIT)) {
    log(429, "visitor-limit");
    return deny(429, "You've asked a lot in the last few minutes. Please wait a moment.", { "X-Faultlines-Limit": "visitor" });
  }
  const remember = text => { if (keep) ctx.waitUntil(cache.put(ck, new Response(text, { headers: { "Content-Type": "text/event-stream", "Cache-Control": `max-age=${CACHE_SECONDS}` } }))); };

  // 3. Gemini models, streamed; every extra key (GEMINI_API_KEY_2 ... _5, each from its own Google project)
  //    has its own daily allowance, so each model is tried with each key
  const keys = [env.GEMINI_API_KEY, env.GEMINI_API_KEY_2, env.GEMINI_API_KEY_3, env.GEMINI_API_KEY_4, env.GEMINI_API_KEY_5].filter(Boolean);
  const order = keys.length ? await geminiOrder(env, ctx) : [];
  // written answers (questions, explanations) may look things up on Google Search, so recent events come out right;
  // the search has its own free daily allowance: when Google refuses it, the same model answers without it
  const wantsSearch = !wantsJson;
  // 3a. with Google Search: on the free plan only some models include it (the newest ones answer "check your plan"),
  //     so written answers first try the models that do; a refusal pauses that model's search for a while
  if (wantsSearch) {
    for (const [ki, key] of keys.entries()) {
      for (const model of SEARCH_MODELS) {
        const slot = `search:k${ki}:${model}`;
        if (!usable(slot)) continue;
        let r;
        try {
          r = await fetch(`${GOOGLE}/models/${model}:streamGenerateContent?alt=sse`, {
            method: "POST", headers: { "x-goog-api-key": key, "Content-Type": "application/json" },
            body: JSON.stringify({ ...body, tools: [{ google_search: {} }] }),
          });
        } catch { continue; }
        if (r.ok && r.body) {
          log(200, (keys.length > 1 ? `${model} #${ki + 1}` : model) + " +search");
          const [toClient, toCache] = r.body.tee();
          ctx.waitUntil(new Response(toCache).text().then(t => { if (t.length > 20) remember(t); }));
          return new Response(toClient, { status: 200, headers: cors({ "Content-Type": "text/event-stream", "X-Faultlines-Cache": "miss",
            "X-Faultlines-Provider": model, "X-Faultlines-Search": "on" }) });
        }
        const why = (await r.text().catch(() => "")).replace(/\s+/g, " ");
        lastSearchRefusal = `${r.status} on ${model}: ${why.slice(0, 140)}`.replace(/[^\x20-\x7e]/g, "");
        // "check your plan and billing" = search isn't in the free plan at all: don't ask again for 12 hours
        skip(slot, /billing|plan/i.test(why) ? 12 * 3600_000 : r.status === 429 ? 30 * 60_000 : r.status === 503 ? 60_000 : 6 * 3600_000);
      }
    }
  }
  // 3b. every model without search
  for (const [ki, key] of keys.entries()) {
    for (const model of order) {
      const slot = `k${ki}:${model}`;
      if (!usable(slot)) continue;
      const b = JSON.parse(JSON.stringify(body));
      if (model.startsWith("gemma")) {   // Gemma: no system instruction or JSON mode
        b.contents = [{ role: "user", parts: [{ text: plainPrompt(body) }] }];
        delete b.systemInstruction; delete b.generationConfig.responseMimeType;
      }
      const searchNote = wantsSearch ? `unavailable (${lastSearchRefusal || "no search model"})` : "off";
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
        return new Response(toClient, { status: 200, headers: cors({ "Content-Type": "text/event-stream", "X-Faultlines-Cache": "miss",
          "X-Faultlines-Provider": model, "X-Faultlines-Search": searchNote }) });
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
  de: "hl=de&gl=DE&ceid=DE:de",
};
const NEWS_QUERIES = {
  en: ["war OR attack OR missile OR drone OR strike", "sanctions OR ceasefire OR coup OR talks OR summit"],
  fr: ["guerre OR attaque OR missile OR frappe", "sanctions OR cessez-le-feu OR coup OR sommet"],
  es: ["guerra OR ataque OR misil OR bombardeo", "sanciones OR alto el fuego OR golpe OR cumbre"],
  ar: ["حرب OR هجوم OR صاروخ OR غارة", "عقوبات OR وقف إطلاق النار OR انقلاب OR قمة"],
  de: ["Krieg OR Angriff OR Rakete OR Drohne OR Luftangriff", "Sanktionen OR Waffenruhe OR Putsch OR Gespräche OR Gipfel"],
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
/* Trusted outlets' own world-news feeds. Google News blocks requests from Cloudflare, these don't; Google is still
   tried as a bonus. The combined pool is cached for 10 minutes per language and shared by /live and /top. */
const OUTLETS = {
  en: [["BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"], ["Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"],
       ["Sky News", "https://feeds.skynews.com/feeds/rss/world.xml"], ["The Guardian", "https://www.theguardian.com/world/rss"],
       ["DW", "https://rss.dw.com/rdf/rss-en-world"], ["France 24", "https://www.france24.com/en/rss"], ["NPR", "https://feeds.npr.org/1004/rss.xml"],
       ["New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"], ["Euronews", "https://www.euronews.com/rss?level=theme&name=news"],
       ["CBS News", "https://www.cbsnews.com/latest/rss/world"], ["ABC News", "https://abcnews.go.com/abcnews/internationalheadlines"],
       ["Washington Post", "https://feeds.washingtonpost.com/rss/world"]],
  fr: [["France 24", "https://www.france24.com/fr/rss"], ["Le Monde", "https://www.lemonde.fr/international/rss_full.xml"], ["RFI", "https://www.rfi.fr/fr/rss"],
       ["Euronews", "https://fr.euronews.com/rss"], ["franceinfo", "https://www.francetvinfo.fr/monde.rss"], ["Le Figaro", "https://www.lefigaro.fr/rss/figaro_international.xml"],
       ["Libération", "https://www.liberation.fr/arc/outboundfeeds/rss-all/category/international/?outputType=xml"], ["20 Minutes", "https://www.20minutes.fr/feeds/rss-monde.xml"],
       ["Le Parisien", "https://feeds.leparisien.fr/leparisien/rss/international"], ["La Presse", "https://www.lapresse.ca/international/rss"],
       ["Le Temps", "https://www.letemps.ch/articles.rss"], ["TV5Monde", "https://information.tv5monde.com/rss.xml"]],
  es: [["El País", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/section/internacional/portada"], ["BBC Mundo", "https://feeds.bbci.co.uk/mundo/rss.xml"],
       ["France 24", "https://www.france24.com/es/rss"], ["DW", "https://rss.dw.com/rdf/rss-sp-all"], ["Euronews", "https://es.euronews.com/rss"]],
  ar: [["BBC Arabic", "https://feeds.bbci.co.uk/arabic/rss.xml"], ["France 24", "https://www.france24.com/ar/rss"], ["DW", "https://rss.dw.com/rdf/rss-ar-all"],
       ["Sky News Arabia", "https://www.skynewsarabia.com/web/rss"], ["Al Jazeera", "https://www.aljazeera.net/aljazeerarss/a7c186be-1baa-4bd4-9d80-a84db769f779/73d0e1b4-532f-45ef-b135-bfdff8b8cab9"],
       ["Euronews", "https://arabic.euronews.com/rss"], ["Asharq Al-Awsat", "https://aawsat.com/feed"], ["CNN Arabic", "https://arabic.cnn.com/api/v1/rss/rss.xml"],
       ["UN News", "https://news.un.org/feed/subscribe/ar/news/all/rss.xml"]],
  de: [["tagesschau", "https://www.tagesschau.de/ausland/index~rss2.xml"], ["DW", "https://rss.dw.com/rdf/rss-de-all"],
       ["Der Spiegel", "https://www.spiegel.de/ausland/index.rss"], ["Zeit", "https://newsfeed.zeit.de/politik/ausland/index"],
       ["FAZ", "https://www.faz.net/rss/aktuell/politik/ausland/"], ["Süddeutsche", "https://rss.sueddeutsche.de/rss/Politik"],
       ["ZDF", "https://www.zdf.de/rss/zdf/nachrichten"], ["NZZ", "https://www.nzz.ch/international.rss"],
       ["Der Standard", "https://www.derstandard.at/rss/international"], ["Euronews", "https://de.euronews.com/rss"]],
};
const stripTags = s => unxml(s).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
function parseFeed(xml, source){
  const out = [];
  for (const m of xml.matchAll(/<(item|entry)[\s>]([\s\S]*?)<\/\1>/g)){
    const b = m[2], g = tag => { const r = b.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`)); return r ? r[1] : ""; };
    const t = stripTags(g("title")); if (!t) continue;
    const link = stripTags(g("link")) || ((b.match(/<link[^>]+href="([^"]+)"/) || [])[1] || "");
    const d = new Date(stripTags(g("pubDate") || g("dc:date") || g("published") || g("updated")));
    const img = (b.match(/<media:(?:content|thumbnail)[^>]+url="([^"]+)"/) || b.match(/<enclosure[^>]+url="([^"]+\.(?:jpe?g|png|webp)[^"]*)"/i) || [])[1] || "";
    out.push({ t: t.slice(0, 300), d: isNaN(d) ? null : d.toISOString(),
      members: [{ u: link.slice(0, 2000), t: t.slice(0, 300), s: source, img: unxml(img).slice(0, 1000), x: stripTags(g("description") || g("summary")).slice(0, 400) }] });
    if (out.length >= 40) break;
  }
  return out;
}
async function newsPool(lang, ctx){
  const cache = caches.default, ck = new Request(`https://faultlines-cache/pool/${lang}`);
  const hit = await cache.match(ck);
  if (hit) return hit.json();
  // each feed gets 5 seconds; a slow outlet is skipped rather than holding everything up
  // (Google News is not used here: it blocks requests from Cloudflare and only made the first load slow)
  const get = (u, h = {}) => fetch(u, { headers: { "User-Agent": "Mozilla/5.0 (compatible; Faultlines news reader)", ...h }, cf: { cacheTtl: 300 },
    signal: AbortSignal.timeout(5000) }).then(r => r.ok ? r.text() : "").catch(() => "");
  const outlets = await Promise.all(OUTLETS[lang].map(([name, u]) => get(u).then(x => parseFeed(x, name))));
  const pool = { items: outlets.flat(), at: Date.now() };
  if (pool.items.length) ctx.waitUntil(cache.put(ck, new Response(JSON.stringify(pool), { headers: { "Content-Type": "application/json", "Cache-Control": "max-age=600" } })));
  return pool;
}
async function liveFeed(lang, ctx){
  if (!NEWS_LANG[lang]) lang = "en";
  const pool = await newsPool(lang, ctx);
  // confirmed stories only (2+ independent outlets), newest first
  const recent = pool.items.filter(it => !it.d || Date.now() - Date.parse(it.d) < 2 * 864e5);
  const items = scoreStories(recent, null).map(st => {
    const lead = st.members.find(m => trusted(m.s)) || st.lead;
    return { t: lead.t || st.t, u: lead.u, s: lead.s, d: st.d, n: st.n, also: st.srcs.filter(x => x !== lead.s).slice(0, 4) };
  });
  items.sort((a, b) => (b.d || "").localeCompare(a.d || ""));
  const body = JSON.stringify({ lang, generated: new Date(pool.at).toISOString(), items: items.slice(0, 80) });
  return new Response(body, { status: 200, headers: cors({ "Content-Type": "application/json" }) });   // an empty list is a normal answer
}

/* ------------------------------------------------------------------ real-time search (the question bar's ⚡ mode)
   The newest reports on a question from several free sources at once: Google News and Bing News searches, GDELT
   (a global news index updated every 15 minutes) and the trusted-outlet feeds above. State propaganda outlets are
   dropped, the same story from several outlets is kept once (with how many carry it), newest first. Cached 5 min. */
const RT_STOP = new Set(("who what when where why how which whom whose is are was were be been being do does did the a an and or of to in on for with at by "
  + "from as about into over after before between its it this that these those will would could should can may might has have had not no than then there "
  + "their they them his her he she we you i me my our your current currently now today latest recent recently news tell explain happening going happen "
  + "think right qui que quoi quel quelle pourquoi comment est sont le la les un une des du de et ou en au aux avec pour sur dans ce cette quién qué "
  + "cuál por cómo es son el los las unos unas del y o con para sobre este esta من ما ماذا لماذا كيف هل في على عن مع إلى هذا هذه "
  + "wer was wann wo warum wieso weshalb wie welche welcher welches ist sind war waren der die das den dem des ein eine einen und oder mit von zu im in "
  + "auf für über nach bei aus gerade jetzt heute aktuell aktuelle neueste passiert gibt es sich nicht auch").split(" "));
const GDELT_LANG = { en: "english", fr: "french", es: "spanish", ar: "arabic", de: "german" };
function parseBing(xml){
  const out = [];
  for (const m of xml.matchAll(/<item>([\s\S]*?)<\/item>/g)){
    const b = m[1], g = tag => { const r = b.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`)); return r ? unxml(r[1]) : ""; };
    let u = g("link"); try { const real = new URL(u).searchParams.get("url"); if (real) u = real; } catch {}
    const d = new Date(g("pubDate"));
    const t = stripTags(g("title"));
    if (t && /^https?:\/\//.test(u)) out.push({ t: t.slice(0, 300), u: u.slice(0, 2000), s: stripTags(g("News:Source")).slice(0, 80), d: isNaN(d) ? null : d.toISOString(), x: stripTags(g("description")).slice(0, 300) });
  }
  return out;
}
async function realtimeSearch(q, lang, ctx){
  if (!NEWS_LANG[lang]) lang = "en";
  const kws = [...new Set(q.toLowerCase().replace(/[^\p{L}\p{N}\s-]/gu, " ").split(/\s+/).filter(w => w.length > 1 && !RT_STOP.has(w)))].slice(0, 8);
  if (!kws.length) return json(200, { q, items: [], used: {} });
  const cache = caches.default, ck = new Request(`https://faultlines-cache/rt/${lang}/${encodeURIComponent(kws.join(" "))}`);
  const hit = await cache.match(ck);
  if (hit) return new Response(hit.body, { status: 200, headers: cors({ "Content-Type": "application/json", "X-Faultlines-Cache": "hit" }) });
  // 3.5 seconds per source: a slow one is left out rather than making the answer wait
  // (GDELT was tried too, but it never answered in time from Cloudflare and only made every search 3 s slower)
  const get = (u, type = "text") => fetch(u, { headers: { "User-Agent": "Mozilla/5.0 (compatible; Faultlines news reader)", "Accept-Language": lang },
    signal: AbortSignal.timeout(3500) }).then(r => r.ok ? (type === "json" ? r.json() : r.text()) : null).catch(() => null);
  const query = kws.join(" ");
  const gdelt = [];
  const [google, bing, pool] = await Promise.all([
    get(`https://news.google.com/rss/search?q=${encodeURIComponent(query + " when:3d")}&${NEWS_LANG[lang]}`).then(x => x ? parseRss(x) : []),
    get(`https://www.bing.com/news/search?q=${encodeURIComponent(query)}&format=rss&qft=${encodeURIComponent('sortbydate="1"')}&setlang=${lang}`).then(x => x ? parseBing(x) : []),
    // the trusted outlets' feeds: stories whose title or summary has most of the question's key words
    newsPool(lang, ctx).then(p => p.items.map(it => it.members[0] && { t: it.members[0].t, u: it.members[0].u, s: it.members[0].s, d: it.d, x: it.members[0].x })
      .filter(it => it && kws.filter(k => (it.t + " " + (it.x || "")).toLowerCase().includes(k)).length >= Math.min(2, kws.length))).catch(() => []),
  ]);
  const cutoff = Date.now() - 4 * 864e5;
  const all = [...pool, ...google, ...bing, ...gdelt].filter(it => it.t && /^https?:\/\//.test(it.u || "") && !STATE_MEDIA.test((it.s || "").trim())
    && (!it.d || Date.parse(it.d) > cutoff));
  // the same story from several outlets: keep the first (newest), count the others. The searched words themselves
  // don't count as shared (every result says "Strait of Hormuz"), or all the results would melt into one story.
  const stories = [];
  for (const it of all.sort((a, b) => (b.d || "").localeCompare(a.d || ""))){
    const w = new Set([...words(it.t)].filter(x => !kws.some(k => x.startsWith(k) || k.startsWith(x))));
    const same = stories.find(s => { let n = 0; w.forEach(x => { if (s.w.has(x)) n++; }); return n >= 3 && n / Math.min(w.size, s.w.size) >= 0.5; });
    if (same){ if (it.s && !same.srcs.includes(it.s)) same.srcs.push(it.s); if (!same.x && it.x) same.x = it.x; continue; }
    stories.push({ ...it, w, srcs: it.s ? [it.s] : [] });
  }
  const items = stories.slice(0, 15).map(({ w, srcs, ...it }) => ({ ...it, n: srcs.filter(s => !AGGREGATOR.test(s)).length || 1, also: srcs.slice(1, 4) }));
  const body = JSON.stringify({ q, lang, generated: new Date().toISOString(), items,
    used: { outlets: pool.length, google: google.length, bing: bing.length, gdelt: gdelt.length } });
  if (items.length) ctx.waitUntil(cache.put(ck, new Response(body, { headers: { "Content-Type": "application/json", "Cache-Control": "max-age=300" } })));
  return new Response(body, { status: 200, headers: cors({ "Content-Type": "application/json" }) });
}

/* ------------------------------------------------------------------ top stories
   Google News topic feeds already group articles into stories (the <ol> in each item lists other outlets on the
   same story); country searches return single articles, which are grouped here by shared title words.
   A story's score = how many outlets cover it + trusted outlets + how political/foreign-policy it is + recency. */
const TRUSTED = ["reuters", "associated press", "ap news", "afp", "agence france", "bbc", "al jazeera", "sky news", "the guardian",
  "new york times", "nytimes", "washington post", "financial times", "ft.com", "bloomberg", "the economist", "wall street journal", "wsj",
  "dw", "deutsche welle", "france 24", "france24", "rfi", "npr", "pbs", "cnn", "abc news", "cbs news", "nbc news", "politico", "axios",
  "the times", "the telegraph", "the independent", "euronews", "le monde", "le figaro", "libération", "el país", "el pais", "el mundo",
  "la vanguardia", "bbc mundo", "bbc arabic", "al arabiya", "العربية", "الجزيرة", "sky news arabia", "سكاي نيوز", "asharq al-awsat",
  "الشرق الأوسط", "cnn arabic", "nikkei", "south china morning post", "the hindu", "haaretz", "times of israel", "kyiv independent",
  "the moscow times", "der spiegel", "spiegel", "zeit", "süddeutsche", "faz", "frankfurter allgemeine", "tagesschau", "orf", "swissinfo", "cbc", "abc.net.au", "cna", "channel newsasia", "guardian", "channel 4",
  "zdf", "nzz", "neue zürcher", "der standard", "handelsblatt", "welt", "n-tv", "ntv", "deutschlandfunk", "br24", "srf",
  "franceinfo", "20 minutes", "le parisien", "la presse", "le temps", "tv5monde", "bfmtv", "un news"];
const trusted = s => { const l = (s || "").toLowerCase(); return TRUSTED.some(t => l === t || l.includes(t)); };
// state-controlled propaganda outlets: never shown, never counted as a confirming source
const STATE_MEDIA = /^(rt|rt news|rt\.com|russia today\b.*|sputnik\b.*|tass|tass\.com|ria novosti\b.*|ria\.ru|press ?tv\b.*|presstv\.ir|tasnim\b.*|fars news\b.*|farsnews\b.*|mehr news\b.*|irna\b.*|global ?times|globaltimes\.cn|cgtn\b.*|china daily\b.*|xinhua\b.*|people'?s daily\b.*|kcna|kcna\.kp|telesur\b.*|al mayadeen\b.*|sana|syrian arab news agency|belta\b.*|granma|pravda\b.*|izvestia|belarus\.by|azertac\b.*|سانا|وكالة سانا|روسيا اليوم|آر تي|سبوتنيك|برس تي في|تسنيم|وكالة تسنيم|فارس|وكالة فارس|مهر|الميادين|شينخوا|وكالة شينخوا|ارنا|إرنا)$/i;
// republishers copy other outlets, so they don't count as an independent confirmation
const AGGREGATOR = /(yahoo|msn|newsbreak|ground ?news|flipboard|head ?topics|newsnow|inkl|devdiscourse|latestly)/i;
const MIN_SOURCES = 2;   // a story is only shown when at least two independent outlets report it
const POLITICAL = [
  [/\b(war|invasion|offensive|front ?line|missiles?|drones?|air ?strikes?|strikes?|shelling|troops|military|army|attacks?|killed|nuclear|guerre|frappes?|armée|militaire|guerra|ataques?|misil(es)?|ejército|militar|krieg\w*|angriff\w*|raketen?\w*|drohnen?\w*|luftangriff\w*|truppen|armee|bundeswehr|militär\w*|getötet|atom\w*)\b|حرب|هجوم|غارة|صاروخ|قصف|جيش|عسكري/i, 3],
  [/\b(sanctions?|ceasefire|truce|peace talks?|talks|negotiations?|summit|treaty|deal|diplomat\w*|embassy|ambassador|foreign minister|un security council|nato|sanctions|cessez-le-feu|négociations?|sommet|accord|diplomat\w*|sanciones|alto el fuego|negociaciones|cumbre|acuerdo|sanktion\w*|waffenruhe|waffenstillstand|verhandlung\w*|gespräche|gipfel\w*|abkommen|botschaft\w*|außenminister\w*|sicherheitsrat)\b|عقوبات|وقف إطلاق النار|مفاوضات|قمة|اتفاق|دبلوماسي/i, 2],
  [/\b(president|prime minister|government|parliament|election|opposition|protests?|coup|minister|kremlin|white house|président|gouvernement|élection|manifestations?|presidente|gobierno|elecciones|protestas?|golpe|präsident\w*|regierung\w*|kanzler\w*|parlament\w*|wahl\w*|opposition|proteste?|putsch|minister\w*|kreml)\b|رئيس|حكومة|انتخابات|احتجاج|برلمان|انقلاب/i, 1],
];
const SPORT = /\b(football|soccer|league|cup|match|tennis|nba|nfl|nhl|hockey|olympic|goal|coach|striker|forward|goalkeeper|player|players|season|transfer|box office|celebrity|actor|singer|concert|film|movie|recipe|weather|horoscope|lottery|fashion|bear|zoo|animal|fußball|bundesliga|trainer|spieler|wetter|rezept|promi\w*|sänger\w*|schauspieler\w*|lotto)\b|كرة|مباراة|دوري/i;
// other countries and blocs named in a story: the more foreign actors, the more "foreign policy" it is
const ACTORS = /\b(united states|u\.s\.|us|america|washington|russia|moscow|kremlin|ukraine|kyiv|china|beijing|taiwan|iran|tehran|israel|gaza|palestin\w*|lebanon|hezbollah|syria|iraq|yemen|houthis?|saudi|emirates|uae|qatar|turkey|türkiye|egypt|libya|sudan|ethiopia|somalia|india|pakistan|afghanistan|north korea|south korea|japan|germany|france|britain|uk|poland|baltic|estonia|latvia|lithuania|finland|belarus|georgia|armenia|azerbaijan|venezuela|cuba|mexico|nato|european union|eu|united nations|un|g7|brics|sahel|mali|niger)\b|روسيا|أوكرانيا|الصين|إيران|إسرائيل|غزة|أمريكا|الولايات المتحدة|تركيا|السعودية|الناتو/gi;
function relItems(descHtml){
  const out = [];
  for (const m of unxml(descHtml).matchAll(/<li><a href="([^"]+)"[^>]*>([\s\S]*?)<\/a>(?:&nbsp;|\s)*<font[^>]*>([\s\S]*?)<\/font><\/li>/g))
    out.push({ u: m[1], t: unxml(m[2]), s: unxml(m[3]) });
  return out;
}
function parseRssClusters(xml){
  const out = [];
  for (const m of xml.matchAll(/<item>([\s\S]*?)<\/item>/g)){
    const b = m[1], g = tag => { const r = b.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`)); return r ? r[1] : ""; };
    let t = unxml(g("title")); const s = unxml(g("source")), d = new Date(unxml(g("pubDate")));
    if (s && t.endsWith(" - " + s)) t = t.slice(0, -(s.length + 3));
    const rel = relItems(g("description"));
    const members = rel.length ? rel : [{ u: unxml(g("link")), t, s }];
    if (t) out.push({ t, d: isNaN(d) ? null : d.toISOString(), members });
  }
  return out;
}
const STOP = new Set("the a an and or of to in on for with at by from as is are was were be been after over into says said amid new more than its his her their this that will would could about against between under what how why who latest live update updates news report reports les des une pour dans sur avec par est son ses qui que del los las por para con una sobre der die das und mit von für auf ist den dem des ein eine einen nach über bei wie zum zur sich nicht auch als oder wird werden hat haben".split(" "));
const words = t => new Set(t.toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, " ").split(/\s+/).filter(w => w.length > 3 && !STOP.has(w)));
// names in a headline (capitalised words after the first): outlets word the same story differently but name the same actors
const names = t => new Set((t.match(/(?<!^)\b\p{Lu}[\p{L}'’-]{2,}/gu) || []).map(x => x.toLowerCase().replace(/[’']s$/, "")).filter(x => !STOP.has(x)));
function scoreStories(items, focus, minSources = MIN_SOURCES){
  // merge items that share enough title words, or enough of the same names, into one story
  const stories = [];
  for (let it of items){
    const members = it.members.filter(m => !STATE_MEDIA.test((m.s || "").trim()));
    if (!members.length) continue;
    it = { ...it, members };
    const w = words(it.t), nm = names(it.t);
    let best = null, bestSim = 0;
    for (const st of stories){
      let inter = 0; w.forEach(x => st.w.has(x) && inter++);
      let ni = 0; nm.forEach(x => st.nm.has(x) && ni++);
      const sim = Math.max(inter >= 2 ? inter / Math.max(1, Math.min(w.size, st.w.size)) : 0, ni >= 2 ? ni / Math.max(2, Math.min(nm.size, st.nm.size)) * 0.9 : 0);
      if (sim > bestSim){ bestSim = sim; best = st; }
    }
    // join a story without widening its words, so stories can't chain into unrelated ones
    if (best && bestSim >= 0.45){ best.members.push(...it.members); if (it.d && (!best.d || it.d > best.d)) best.d = it.d; }
    else stories.push({ t: it.t, d: it.d, w, nm, members: [...it.members] });
  }
  const now = Date.now();
  for (const st of stories){
    const srcs = [...new Set(st.members.map(x => x.s).filter(s => s && !AGGREGATOR.test(s)))];   // independent outlets only
    const trustedSrcs = srcs.filter(trusted);
    const text = st.t + " " + st.members.map(x => x.t).join(" ");
    const pol = POLITICAL.reduce((a, [re, w]) => a + (re.test(text) ? w : 0), 0);
    const hours = st.d ? (now - Date.parse(st.d)) / 36e5 : 48;
    const lead = st.members.find(x => trusted(x.s)) || st.members[0];
    const actors = new Set((text.match(ACTORS) || []).filter(a => !focus || !focus.test(a)).map(a => a.toLowerCase()));   // other countries than the selected one
    Object.assign(st, { lead, srcs, n: srcs.length, trustedN: trustedSrcs.length, political: pol + actors.size > 0 && !SPORT.test(text),
      score: srcs.length * 2 + trustedSrcs.length * 3 + pol * 2 + Math.min(actors.size, 4) * 2.5
        - Math.min(hours, 72) / 8 - (SPORT.test(text) ? 20 : 0)
        + (focus && !focus.test(text) ? -15 : 0) });
  }
  // confirmed stories only: several independent outlets, at least one of them trusted
  return stories.filter(st => st.n >= minSources && st.trustedN >= 1).sort((a, b) => b.score - a.score);
}
async function topStories(lang, q, en, ctx){
  if (!NEWS_LANG[lang]) lang = "en";
  const key = `https://faultlines-cache/top/${lang}/${encodeURIComponent(q.toLowerCase())}`;
  const cache = caches.default, hit = await cache.match(key);
  if (hit) return new Response(hit.body, { status: 200, headers: cors({ "Content-Type": "application/json", "X-Faultlines-Cache": "hit" }) });
  const pool = await newsPool(lang, ctx);
  // a country or place: only stories that name it (page language or English, plus capital/leader/"US"-style aliases)
  const focus = focusRegex(q, en);
  const recent = it => !it.d || Date.now() - Date.parse(it.d) < (focus ? 5 : 2) * 864e5;
  const items = pool.items.filter(it => recent(it) && (!focus || focus.test(it.t + " " + it.members.map(m => m.t + " " + (m.x || "")).join(" "))));
  let ranked = scoreStories(items, focus);
  if (focus) ranked = ranked.filter(st => st.political);   // a country: politics and foreign affairs only (else the site uses its own headlines)
  const stories = ranked.slice(0, 6).map(st => {
    const lead = st.members.find(m => trusted(m.s) && m.img) || st.lead;   // prefer a trusted outlet with a photo
    return { t: lead.t || st.t, u: lead.u, s: lead.s, d: st.d, n: st.n, img: (st.members.find(m => m.img) || {}).img || "",
             also: st.srcs.filter(x => x !== lead.s).slice(0, 6) };
  });
  const body = JSON.stringify({ lang, q, generated: new Date().toISOString(), stories });
  if (stories.length) ctx.waitUntil(cache.put(key, new Response(body, { headers: { "Content-Type": "application/json", "Cache-Control": "max-age=600" } })));
  return new Response(body, { status: 200, headers: cors({ "Content-Type": "application/json", "X-Faultlines-Cache": "miss" }) });   // an empty list is a normal answer
}

/* how headlines name the major countries besides their official name (capital, leader, "US", "Kremlin" ...) */
const COUNTRY_ALIASES = {
  "United States": ["U\\.S\\.", "US", "USA", "America", "American", "Washington", "White House", "Trump", "Pentagon"],
  "United Kingdom": ["UK", "U\\.K\\.", "Britain", "British", "London", "Downing Street", "Starmer"],
  "Russia": ["Russian", "Kremlin", "Moscow", "Putin"], "China": ["Chinese", "Beijing", "Xi Jinping", "Xi"],
  "Ukraine": ["Ukrainian", "Kyiv", "Zelensky", "Zelenskyy"], "Israel": ["Israeli", "Netanyahu", "IDF", "Jerusalem"],
  "Palestine": ["Palestinian", "Gaza", "West Bank", "Hamas"], "Iran": ["Iranian", "Tehran", "Khamenei", "Pezeshkian"],
  "Germany": ["German", "Berlin", "Merz", "Bundeswehr"], "France": ["French", "Paris", "Macron", "Élysée", "Elysee"],
  "Egypt": ["Egyptian", "Cairo", "Sisi", "Suez"], "Türkiye": ["Turkey", "Turkish", "Ankara", "Erdogan", "Erdoğan"],
  "India": ["Indian", "New Delhi", "Delhi", "Modi"], "Pakistan": ["Pakistani", "Islamabad"], "Japan": ["Japanese", "Tokyo"],
  "Saudi Arabia": ["Saudi", "Riyadh"], "North Korea": ["Pyongyang", "Kim Jong Un"], "South Korea": ["Seoul", "South Korean"],
  "Taiwan": ["Taiwanese", "Taipei"], "Syria": ["Syrian", "Damascus"], "Lebanon": ["Lebanese", "Beirut", "Hezbollah"],
  "Yemen": ["Yemeni", "Houthi", "Houthis", "Sanaa"], "Sudan": ["Sudanese", "Khartoum", "RSF"], "Venezuela": ["Venezuelan", "Caracas", "Maduro"],
  "Poland": ["Polish", "Warsaw"], "Italy": ["Italian", "Rome", "Meloni"], "Iraq": ["Iraqi", "Baghdad"], "Afghanistan": ["Afghan", "Kabul", "Taliban"],
};
function focusRegex(q, en){
  const names = [q, en].filter(Boolean).map(x => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).concat(COUNTRY_ALIASES[en] || []);
  return names.length ? new RegExp(`(^|[^\\p{L}])(${names.join("|")})(?![\\p{L}])`, (COUNTRY_ALIASES[en] ? "u" : "iu")) : null;   // aliases like "US" are case-sensitive
}

/* ------------------------------------------------------------------ news videos
   Trusted news channels' public YouTube feeds (latest ~15 videos each, no key needed). Videos are grouped into
   stories and ranked like the top stories; world videos that match today's biggest stories get a boost. */
const CHANNELS = {
  en: [["Al Jazeera English", "UCNye-wNBqNL5ZzHSJj3l8Bg"], ["Sky News", "UCoMdktPbSTixAyNGwb-UYkQ"], ["DW News", "UCknLrEdhRCp1aegoMqRaCZg"],
       ["France 24 English", "UCQfwfsi5VrQ8yKZ-UWmAEFg"], ["BBC News", "UC16niRr50-MSBwiO3YDb3RA"], ["Reuters", "UChqUTb7kYRX8-EiaN3XFrSQ"],
       ["Associated Press", "UC52X5wxOL_s5yw0dQk7NtgA"], ["CNA", "UC83jt4dlz1Gjl58fzQrrKZg"], ["Guardian News", "UCIRYBXDze5krPDzAEOxFGVA"],
       ["PBS NewsHour", "UC6ZFN9Tx6xh-skXCuRHCDpQ"], ["Channel 4 News", "UCTrQ7HXWRRxr7OsOtodr2_w"], ["euronews", "UCSrZ3UV4jOidv8ppoVuvW9Q"],
       ["CBS News", "UC8p1vwvWtl6T73JiExfWs1g"], ["NBC News", "UCeY0bbntWzzVIaj2z3QigXg"], ["ABC News", "UCBi2mrWuNuyYy4gbM6fU18Q"]],
  fr: [["France 24", "UCCCPCZNChQdGa9EkATeye4g"], ["Euronews", "UCW2QcKZiU8aUGg4yxCIditg"], ["DW News", "UCknLrEdhRCp1aegoMqRaCZg"],
       ["BFMTV", "UCXwDLMDV86ldKoFVc_g8P0g"], ["Le Monde", "UCYpRDnhk5H8h16jpS84uqsA"]],
  es: [["France 24 Español", "UCUdOoVWuWmgo1wByzcsyKDQ"], ["DW Español", "UCT4Jg8h03dD0iN3Pb5L0PMA"]],
  ar: [["Al Jazeera Arabic", "UCfiwzLy-8yKzIbsmZTzxDgw"], ["Sky News Arabia", "UCIJXOvggjKtCagMfxvcCzAA"], ["France 24 Arabic", "UCdTyuXgmJkG_O8_75eqej-w"],
       ["DW Arabic", "UC30ditU5JI16o5NbFsHde_Q"], ["BBC News Arabic", "UCelk6aHijZq-GJBBB9YpReA"], ["Al Arabiya", "UCahpxixMCwoANAftn6IxkTg"]],
  de: [["tagesschau", "UC5NOEUbkLheQcaaRldYW5GA"], ["ZDFheute Nachrichten", "UCeqKIgPQfNInOswGRWt48kQ"], ["DW Deutsch", "UCMIgOXM2JEQ2Pv2d0_PVfcg"],
       ["WELT Nachrichtensender", "UCZMsvbAhhRblVGXmEXW8TSA"], ["phoenix", "UCwyiPnNlT8UABRmGmU0T9jg"], ["ntv Nachrichten", "UCSeil5V81-mEGB1-VNR7YEA"]],
};
// 24/7 live news streams per language (the "LIVE TV" button)
const LIVE_TV = { en: "UCNye-wNBqNL5ZzHSJj3l8Bg", fr: "UCCCPCZNChQdGa9EkATeye4g", es: "UCUdOoVWuWmgo1wByzcsyKDQ", ar: "UCfiwzLy-8yKzIbsmZTzxDgw", de: "UCZMsvbAhhRblVGXmEXW8TSA" };
function parseYouTube(xml, channel){
  const out = [];
  for (const m of xml.matchAll(/<entry>([\s\S]*?)<\/entry>/g)){
    const b = m[1], g = tag => { const r = b.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`)); return r ? unxml(r[1]) : ""; };
    const id = g("yt:videoId"), t = g("title"), d = new Date(g("published"));
    if (!id || !t || /#shorts?\b/i.test(t)) continue;
    out.push({ t: t.slice(0, 300), d: isNaN(d) ? null : d.toISOString(),
      members: [{ u: `https://www.youtube.com/watch?v=${id}`, t: t.slice(0, 300), s: channel, id, d: isNaN(d) ? null : d.toISOString(), img: `https://i.ytimg.com/vi/${id}/hqdefault.jpg`, x: g("media:description").slice(0, 400) }] });
  }
  return out;
}
async function videoPool(lang, ctx){
  const cache = caches.default, ck = new Request(`https://faultlines-cache/vpool/${lang}`);
  const hit = await cache.match(ck);
  if (hit) return hit.json();
  const lists = await Promise.all(CHANNELS[lang].map(([name, id]) => fetch(`https://www.youtube.com/feeds/videos.xml?channel_id=${id}`, { cf: { cacheTtl: 300 }, signal: AbortSignal.timeout(5000) })
    .then(r => r.ok ? r.text() : "").then(x => parseYouTube(x, name)).catch(() => [])));
  const pool = { items: lists.flat(), at: Date.now() };
  if (pool.items.length) ctx.waitUntil(cache.put(ck, new Response(JSON.stringify(pool), { headers: { "Content-Type": "application/json", "Cache-Control": "max-age=600" } })));
  return pool;
}
async function topVideos(lang, q, en, ctx){
  if (!CHANNELS[lang]) lang = "en";
  const key = `https://faultlines-cache/videos/${lang}/${encodeURIComponent(q.toLowerCase())}`;
  const cache = caches.default, hit = await cache.match(key);
  if (hit) return new Response(hit.body, { status: 200, headers: cors({ "Content-Type": "application/json", "X-Faultlines-Cache": "hit" }) });
  const pool = await videoPool(lang, ctx);
  const focus = focusRegex(q, en);
  const recent = it => !it.d || Date.now() - Date.parse(it.d) < (focus ? 7 : 2) * 864e5;
  // which country a video is about: its title only, without the channel's brand ("• FRANCE 24 English" is not about France;
  // descriptions carry boilerplate like "DW is Germany's international broadcaster")
  const unbrand = t => t.replace(/\s*[•|]\s*[^•|]*(france\s*24|dw|euronews|sky news|al jazeera|bbc|cna)[^•|]*$/i, "").replace(/france\s*24|france24|radio france|deutsche welle/gi, " ");
  let items = pool.items.filter(it => recent(it) && (!focus || focus.test(unbrand(it.t))));
  // A video is shown only if its story is confirmed by 2+ independent organisations: other channels, or news outlets
  // reporting the same story (the channel and the same outlet's website count once: "Al Jazeera English" = "Al Jazeera").
  const news = await newsPool(lang, ctx);
  const newsItems = news.items.filter(it => (!it.d || Date.now() - Date.parse(it.d) < (focus ? 5 : 2) * 864e5)
    && (!focus || focus.test(it.t + " " + it.members.map(m => m.t + " " + (m.x || "")).join(" "))));
  const org = s => (s || "").toLowerCase().replace(/\b(english|news|arabic|español|en français|tv|online|world|international)\b/g, "").replace(/[^\p{L}\p{N}]/gu, "");
  const newsStories = scoreStories(newsItems, focus, 1);
  const topWords = scoreStories(newsItems, focus).slice(0, 8).map(st => st.nm);
  const overlap = (tw, st) => { let names = 0, ws = 0; st.nm.forEach(x => tw.has(x) && names++); st.w.forEach(x => tw.has(x) && ws++); return names >= 2 || ws >= 3; };
  const stories = scoreStories(items, focus, 1).filter(st => {
    const tw = words(st.t + " " + st.members.map(m => m.t).join(" "));
    const orgs = new Set(st.srcs.map(org));
    newsStories.filter(ns => overlap(tw, ns)).forEach(ns => ns.srcs.forEach(s => orgs.add(org(s))));
    st.confirmedBy = orgs.size;
    return orgs.size >= MIN_SOURCES;
  }).map(st => {
    const tw = words(st.t + " " + st.members.map(m => m.t).join(" "));
    const hot = focus ? 0 : topWords.reduce((a, nms) => { let k = 0; nms.forEach(x => tw.has(x) && k++); return Math.max(a, k); }, 0);
    const format = /\b(replay|highlights|podcast|full episode|compilation)\b/i.test(st.t) ? 6 : 0;   // prefer news reports over recordings
    return Object.assign(st, { score: st.score + Math.min(hot, 3) * 4 - format });
  }).sort((a, b) => b.score - a.score).slice(0, 5).map(st => {
    const lead = st.members.slice().sort((a, b) => (b.d || "").localeCompare(a.d || ""))[0];   // newest video on the story
    return { id: lead.id, t: lead.t, s: lead.s, d: lead.d || st.d, n: st.confirmedBy || st.n, also: st.srcs.filter(x => x !== lead.s).slice(0, 4) };
  });
  const body = JSON.stringify({ lang, q, live: LIVE_TV[lang], generated: new Date().toISOString(), videos: stories });
  if (stories.length) ctx.waitUntil(cache.put(key, new Response(body, { headers: { "Content-Type": "application/json", "Cache-Control": "max-age=600" } })));
  return new Response(body, { status: 200, headers: cors({ "Content-Type": "application/json", "X-Faultlines-Cache": "miss" }) });
}

// the site's addresses: GitHub Pages and Cloudflare Pages (faultlines*.pages.dev, including preview builds)
const ALLOWED_ORIGINS = [/^https:\/\/omarezz0709-gif\.github\.io$/, /^https:\/\/([a-z0-9-]+\.)?faultlines(-[a-z0-9]+)?\.pages\.dev$/];
const allowedOrigin = o => ALLOWED_ORIGINS.some(re => re.test(o || ""));

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get("Origin") || "";
    const res = await handle(request, env, ctx, origin);
    if (!allowedOrigin(origin)) return res;
    // answer with the caller's own address, so both site addresses work
    const out = new Response(res.body, res);
    out.headers.set("Access-Control-Allow-Origin", origin);
    return out;
  },
  // Cron Trigger (Worker → Settings → Triggers): "10 22,23 * * *" = 00:10 Berlin in summer and winter
  async scheduled(event, env, ctx) {
    const berlinHour = +new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Berlin", hour: "2-digit", hourCycle: "h23" }).format(new Date(event.scheduledTime));
    if (berlinHour !== 0) return;   // the other (summer/winter) trigger: not our slot
    ctx.waitUntil(archiveYesterday(env));
  },
};

async function handle(request, env, ctx, origin) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors() });
    if (!allowedOrigin(origin)) return deny(403, "This AI proxy only serves the Faultlines site.");

    const url = new URL(request.url);
    const path = url.pathname.replace(/^\/+/, "");
    const ip = request.headers.get("CF-Connecting-IP") || "?";
    const kind = (request.headers.get("X-Faultlines-Kind") || "ai").slice(0, 20);

    // --- visit ping (one per browser session) ---
    if (path === "hit") {
      ctx.waitUntil(logEvent(env, request, { kind: "visit", status: 200 }));
      return new Response(null, { status: 204, headers: cors() });
    }

    // --- real-time search for the question bar's ⚡ mode (fair use: 20 searches per visitor per 5 minutes) ---
    if (path === "search") {
      if (limited(`rt:${ip}`, { max: 20, windowMs: 5 * 60_000 })) return deny(429, "Too many searches in a few minutes. Please wait a moment.", { "X-Faultlines-Limit": "visitor" });
      return realtimeSearch((url.searchParams.get("q") || "").slice(0, 300), url.searchParams.get("lang") || "en", ctx);
    }

    // --- live feed: latest world headlines from Google News, cached 10 minutes per language ---
    if (path === "live") return liveFeed(url.searchParams.get("lang") || "en", ctx);

    // --- news videos: the top stories as videos from trusted news channels (world, or one country) ---
    if (path === "videos") return topVideos(url.searchParams.get("lang") || "en", (url.searchParams.get("q") || "").slice(0, 60),
                                            (url.searchParams.get("en") || "").slice(0, 60), ctx);

    // --- top stories: the biggest stories right now (world, or one country), ranked by how widely they're covered ---
    if (path === "top") return topStories(url.searchParams.get("lang") || "en", (url.searchParams.get("q") || "").slice(0, 60),
                                          (url.searchParams.get("en") || "").slice(0, 60), ctx);

    // --- the backup code (secret ADMIN_BACKUP_CODE): unlocks a locked admin login, and opens the private chat ---
    // 5 wrong backup codes per visitor per hour, so it can't be guessed
    const backup = request.headers.get("X-Backup-Code");
    const checkBackup = async () => {   // null = right; otherwise the refusal to send
      if (!env.ADMIN_BACKUP_CODE) return deny(500, "ADMIN_BACKUP_CODE secret is not set on the Worker.");
      const failKey = `backupfail:${ip}`;
      const fails = (hits.get(failKey) || []).filter(t => Date.now() - t < BACKUP_LIMIT.windowMs);
      if (fails.length >= BACKUP_LIMIT.max) return deny(429, "Too many wrong backup codes. Try again in an hour.");
      if (await sameSecret(backup || "", env.ADMIN_BACKUP_CODE)) return null;
      fails.push(Date.now()); hits.set(failKey, fails);
      ctx.waitUntil(logEvent(env, request, { kind: path === "private/verify" ? "private" : "unlock", status: 401 }));
      return deny(401, "Wrong backup code.", { "X-Faultlines-Tries": String(BACKUP_LIMIT.max - fails.length) });
    };
    if (path === "private/verify") {
      const no = await checkBackup(); if (no) return no;
      ctx.waitUntil(logEvent(env, request, { kind: "private", status: 200 }));
      return json(200, { ok: true });
    }

    // --- admin: usage log. 5 wrong codes in total (from anyone) lock it for everyone; only the backup code
    //     (sent instead of the admin code) unlocks it, and it opens the logs too ---
    if (path === "admin/logs" || path === "admin/archive") {
      if (!env.ADMIN_CODE) return deny(500, "ADMIN_CODE secret is not set on the Worker.");
      const code = request.headers.get("X-Admin-Code") || "";
      const lock = await getAdminLock(env);
      if (backup) {
        const no = await checkBackup(); if (no) return no;
        if (lock.locked || lock.fails){ await setAdminLock(env, { fails: 0, locked: false }); ctx.waitUntil(logEvent(env, request, { kind: "unlock", status: 200 })); }
      }
      else if (lock.locked) return deny(423, "Locked after 5 wrong codes. Enter the backup code to unlock.", { "X-Faultlines-Locked": "1" });
      else if (!code) return deny(401, "Enter the admin code.");   // no code at all (a bot or a crawler): not counted as a try
      else if (!(await sameSecret(code, env.ADMIN_CODE))) {
        const n = (lock.fails || 0) + 1;
        await setAdminLock(env, { fails: n, locked: n >= ADMIN_MAX_TRIES, at: new Date().toISOString() });
        ctx.waitUntil(logEvent(env, request, { kind: "admin", status: n >= ADMIN_MAX_TRIES ? 423 : 401 }));
        if (n >= ADMIN_MAX_TRIES) return deny(423, "Locked after 5 wrong codes. Enter the backup code to unlock.", { "X-Faultlines-Locked": "1" });
        return deny(401, `Wrong code. ${ADMIN_MAX_TRIES - n} ${ADMIN_MAX_TRIES - n === 1 ? "try" : "tries"} left.`, { "X-Faultlines-Tries": String(ADMIN_MAX_TRIES - n) });
      }
      if (lock.fails) await setAdminLock(env, { fails: 0, locked: false });   // a right code resets the count
      if (!env.LOGS) return json(200, { logs: [], note: "No KV namespace bound as LOGS, so nothing is being logged yet." });
      if (path === "admin/archive"){   // ?day=YYYY-MM-DD opens one archived day; without it: the list of archived days
        const day = url.searchParams.get("day") || "";
        if (!day) return json(200, { days: await listArchive(env) }, { "Cache-Control": "no-store" });
        if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return deny(400, "Bad day.");
        const a = await env.LOGS.get(`a:${day}`, "json");
        return a ? json(200, { day, logs: a.entries || [], saved: a.saved }, { "Cache-Control": "no-store" }) : deny(404, "No archive for that day.");
      }
      return json(200, { logs: await readLogs(env), generated: new Date().toISOString(), archive: (await listArchive(env)).slice(0, 60) }, { "Cache-Control": "no-store" });
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
}
