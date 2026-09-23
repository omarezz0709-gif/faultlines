/**
 * Faultlines AI proxy (Cloudflare Worker, free plan).
 *
 * Holds the Gemini key as a Worker secret (GEMINI_API_KEY) so the website can offer
 * in-depth explanations on every device without anyone typing a key, and without the
 * key ever appearing in the page or on GitHub.
 *
 * Only accepts requests from the site's own address, only Gemini Flash models, and
 * caps the answer length. Set ALLOWED_ORIGIN below to your site's origin.
 */
const ALLOWED_ORIGIN = "https://omarezz0709-gif.github.io";
const GOOGLE = "https://generativelanguage.googleapis.com/v1beta";
const MAX_OUTPUT_TOKENS = 8192;
const MAX_BODY_BYTES = 200_000;

function cors(extra = {}) {
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
    ...extra,
  };
}

const deny = (status, message) =>
  new Response(JSON.stringify({ error: { code: status, message } }), {
    status, headers: cors({ "Content-Type": "application/json" }),
  });

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors() });
    if (origin !== ALLOWED_ORIGIN) return deny(403, "This AI proxy only serves the Faultlines site.");
    if (!env.GEMINI_API_KEY) return deny(500, "The GEMINI_API_KEY secret is not set on this Worker.");

    const url = new URL(request.url);
    const path = url.pathname.replace(/^\/+/, "");

    // 1. list models (so the site can pick the newest Flash model)
    if (request.method === "GET" && path === "models") {
      const r = await fetch(`${GOOGLE}/models?pageSize=200`, { headers: { "x-goog-api-key": env.GEMINI_API_KEY } });
      return new Response(r.body, { status: r.status, headers: cors({ "Content-Type": "application/json" }) });
    }

    // 2. generate: models/<flash model>:streamGenerateContent or :generateContent
    const m = path.match(/^models\/(gemini-[a-z0-9.\-]*flash[a-z0-9.\-]*):(streamGenerateContent|generateContent)$/);
    if (request.method !== "POST" || !m) return deny(404, "Unsupported request.");
    const raw = await request.text();
    if (raw.length > MAX_BODY_BYTES) return deny(413, "Request too large.");
    let body;
    try { body = JSON.parse(raw); } catch { return deny(400, "Invalid JSON."); }
    body.generationConfig = { ...(body.generationConfig || {}) };
    body.generationConfig.maxOutputTokens = Math.min(body.generationConfig.maxOutputTokens || MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS);
    delete body.tools;            // no web tools through the proxy
    delete body.cachedContent;

    const target = `${GOOGLE}/models/${m[1]}:${m[2]}${m[2] === "streamGenerateContent" ? "?alt=sse" : ""}`;
    const r = await fetch(target, {
      method: "POST",
      headers: { "x-goog-api-key": env.GEMINI_API_KEY, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return new Response(r.body, {
      status: r.status,
      headers: cors({ "Content-Type": r.headers.get("Content-Type") || "application/json", "Cache-Control": "no-store" }),
    });
  },
};
