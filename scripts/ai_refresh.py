"""AI news update with Google Gemini (free tier): reads the headlines collected by news.py
and updates relationship statuses, indirect links, locations and election info in data/live.json.

Every change must cite one of the collected headlines (by id), so sources are always real links.
Runs right after news.py at 00:00, 12:00 and 18:00 Berlin time. Needs the GitHub secret
GEMINI_API_KEY; without it the step is skipped and the rest of the site keeps working.
Optional: GEMINI_MODEL to pin a model (otherwise the newest available Flash model is used).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from common import ROOT, iso_z, load_json, now_utc, ran_recently, save_json, scheduled

API = "https://generativelanguage.googleapis.com/v1beta"
STATUSES = {"A", "F", "N", "T", "H", "W", "none"}
THREAD_TYPES = {"security", "trafficking", "migration", "proxy", "resource", "economy"}
POI_TYPES = {"strait", "land", "flash", "base", "resource"}
ISO = re.compile(r"^[A-Z]{3}$")
HID = re.compile(r"^h(\d+)$")


# ---------------------------------------------------------------- Gemini
def _req(method: str, url: str, key: str, body: dict | None = None, timeout: int = 300) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"x-goog-api-key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _version(name: str) -> tuple:
    nums = re.findall(r"\d+(?:\.\d+)?", name.split("/")[-1])
    return tuple(float(n) for n in nums[:1]) or (0.0,)


FALLBACK_MODELS = ["gemini-3.6-flash"]   # Google's recommended model first
LAST_RESORT = ["gemini-3.8-flash", "gemma-4-31b-it", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
BAD = ("image", "tts", "audio", "live", "thinking-exp", "8b", "omni", "embed")


def pick_models(models: list[dict]) -> list[str]:
    """The free tier allows ~20 requests a day per model, so return a spread: best Flash models (newest stable first),
    then the largest Gemma model, then the Lite models, then other Gemma models."""
    flash, lite, gemma = [], [], []
    for m in models:
        name = m.get("name", "").split("/")[-1]
        low = name.lower()
        if "generateContent" not in (m.get("supportedGenerationMethods") or []) or any(x in low for x in BAD):
            continue
        key = (_version(name), "preview" not in low and "exp" not in low, name)
        if low.startswith("gemma-"):
            gemma.append(key)
        elif low.startswith("gemini-") and "flash-lite" in low and (key[0] >= (3.0,) or "latest" in low):
            lite.append(key)
        elif low.startswith("gemini-") and "flash" in low and "lite" not in low and key[0] >= (3.0,):
            flash.append(key)
    for lst in (flash, lite):
        lst.sort(key=lambda t: (t[1], t[0]), reverse=True)   # stable first, then newest
    gemma.sort(key=lambda t: (t[0], "31b" in t[2]), reverse=True)
    order = FALLBACK_MODELS + [t[2] for t in flash] + [t[2] for t in gemma[:1]] + [t[2] for t in lite] + [t[2] for t in gemma[1:]]
    return list(dict.fromkeys(order)) if len(order) > 1 else FALLBACK_MODELS + LAST_RESORT


def pick_model(models: list[dict]) -> str:
    return pick_models(models)[0]


def call_gemini(key: str, system: str, prompt: str) -> str:
    pinned = os.environ.get("GEMINI_MODEL")
    if pinned:
        candidates = [pinned] + FALLBACK_MODELS
    else:
        try:
            candidates = pick_models(_req("GET", f"{API}/models?pageSize=200", key).get("models", []))
        except Exception as e:
            print(f"model list failed ({e}); using fallbacks")
            candidates = FALLBACK_MODELS + LAST_RESORT

    def body_for(model: str) -> dict:
        if model.startswith("gemma"):   # Gemma: no system instruction or JSON mode; the prompt asks for JSON
            return {"contents": [{"role": "user", "parts": [{"text": system + "\n\n" + prompt}]}],
                    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 16384}}
        return {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json", "maxOutputTokens": 16384}}

    # try models in order: a busy (503), retired (404), rate-limited (429) or unsupported (400 on Gemma) model falls through
    tries = [m for m in dict.fromkeys(candidates)][:10]
    for attempt, model in enumerate(tries):
        print(f"Using {model}", flush=True)
        try:
            res = _req("POST", f"{API}/models/{model}:generateContent", key, body_for(model))
            cands = res.get("candidates") or []
            if not cands:
                raise SystemExit(f"Gemini returned no answer: {json.dumps(res.get('promptFeedback', {}))[:300]}")
            parts = (cands[0].get("content") or {}).get("parts") or []
            u = res.get("usageMetadata", {})
            print(f"finish={cands[0].get('finishReason')} in={u.get('promptTokenCount')} out={u.get('candidatesTokenCount')}")
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))   # skip Gemma's private notes
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if (e.code in (404, 429, 500, 503) or (e.code == 400 and model.startswith("gemma"))) and attempt < len(tries) - 1:
                print(f"{model}: HTTP {e.code}, trying the next model: {msg}")
                time.sleep(10)
                continue
            if e.code in (400, 401, 403):
                raise SystemExit(f"Gemini rejected the request (HTTP {e.code}). Check the GEMINI_API_KEY secret. {msg}")
            raise SystemExit(f"Gemini error HTTP {e.code}: {msg}")
    raise SystemExit("Gemini kept failing; nothing written.")


# ---------------------------------------------------------------- inputs
def read_baseline(html: str) -> dict:
    rel = re.search(r"const REL_RAW = `\n(.*?)\n`;", html, re.S)
    threads = re.findall(r"\{id:'([^']+)',t:'(\w+)',n:'((?:[^'\\]|\\.)*)',c:\[([^\]]*)\]", html)
    pois = re.findall(r"\{n:'((?:[^'\\]|\\.)*)',t:'(\w+)',lat:(-?[\d.]+),lng:(-?[\d.]+),p:\[([^\]]*)\]", html)
    unq = lambda s: s.replace("\\'", "'")
    return {
        "relations": rel.group(1).strip() if rel else "",
        "threads": [f"{i} [{t}] {unq(n)}: {c.replace(chr(39), '')}" for i, t, n, c in threads],
        "pois": [f"{unq(n)} [{t}] ({lat},{lng}): {p.replace(chr(39), '')}" for n, t, lat, lng, p in pois],
    }


def headline_table(news: dict) -> tuple[str, dict]:
    """Numbered headline list for the prompt, and id -> headline lookup."""
    items = list(news.get("items") or [])
    urls = {x["u"] for x in items}
    for a in news.get("alerts") or []:
        if a.get("u") not in urls:
            items.append({k: a[k] for k in ("t", "s", "u", "d") if k in a})
            urls.add(a.get("u"))
    lookup, lines = {}, []
    for i, x in enumerate(sorted(items, key=lambda x: x.get("d", ""), reverse=True)):
        hid = f"h{i}"
        lookup[hid] = x
        lines.append(f"{hid} | {x.get('d', '')[:10]} | {x.get('s', '')} | {x.get('t', '')}")
    return "\n".join(lines), lookup


SYSTEM = """You maintain "Faultlines", a public interactive globe of international relations. It has a built-in baseline (relations between countries; "threads" = indirect transnational links such as militant networks, smuggling and migration routes, proxy wars, water/food/energy and money flows; strategic locations) plus live overrides that you produce.

On each run you receive the latest news headlines (each with an id like h12) and return only the changes they justify, as JSON.

Rules:
- Be conservative. Change a relationship status only when a headline clearly shows an event that moves it (diplomatic ties cut or restored, war starts or ends, formal alliance or defence pact signed, coup, major sanctions). Routine rhetoric, talks and visits are not status changes, but you may refresh a relationship note to mention a notable new development. A typical run has 0-3 changes.
- Every change MUST cite the id of a headline from the list in "src" (e.g. "h12"). Never invent facts or ids. If headlines are ambiguous, make no change.
- Status codes: A=Ally (formal/de facto alliance), F=Partner, N=Normal/mixed, T=Tense, H=Hostile, W=Armed conflict; "none" removes a baseline pair.
- Countries use ISO3 codes (special: XKX Kosovo, NCY Northern Cyprus, SOL Somaliland, PSE Palestine, TWN Taiwan, ESH Western Sahara).
- Thread types: security, trafficking, migration, proxy, resource, economy. A thread description explains the mechanism as a chain: what happens where, how it moves, who it hits and who is watching. When updating a baseline thread, give its full countries list and description.
- Location types: strait, land, flash, base, resource. To update a baseline location use its exact name and only the fields that change.
- Elections: when headlines report a national election result or a newly scheduled election, add a politics entry with "lastelec" and/or "nextelec" (leaders themselves come from another source).
- Spell out abbreviations in parentheses on first use in every note or description, e.g. "JNIM (Jama'at Nusrat al-Islam wal-Muslimin)".
- Notes: one sentence, max 140 characters, present tense, neutral.

Reply with ONLY this JSON object (use empty lists when nothing changed):
{
  "summary": "one sentence about this check",
  "relations": [{"a": "ISO", "b": "ISO", "s": "T", "note": "...", "src": "h12"}],
  "threads": [{"id": "kebab-id", "t": "security", "n": "Name", "c": ["ISO", "ISO"], "d": "...", "src": "h3"}],
  "pois": [{"n": "Name", "t": "strait", "lat": 0.0, "lng": 0.0, "p": ["ISO"], "s": "key stat", "d": "...", "src": "h7"}],
  "politics": [{"iso": "DEU", "lastelec": "type, month year, winner", "nextelec": "type and date", "src": "h9"}],
  "log": [{"label": "full country names, e.g. Belgium–Rwanda, or the thread, location or country name", "change": "e.g. Tense → Hostile: short reason", "src": "h12"}]
}"""


def build_prompt(live: dict, baseline: dict, table: str, t_utc: dt.datetime) -> str:
    overrides = {k: live.get(k, {}) for k in ("relations", "threads", "pois")}
    return f"""Now: {t_utc.strftime('%Y-%m-%d %H:%M')} UTC. Previous AI update: {(live.get('meta') or {}).get('lastRun') or 'none'}.

Latest headlines (id | date | source | title):
{table}

Current live overrides (already applied on top of the baseline):
{json.dumps(overrides, ensure_ascii=False)}

Baseline relations (A B STATUS note):
{baseline['relations']}

Baseline threads (id [type] name: countries):
{chr(10).join(baseline['threads'])}

Baseline locations (name [type] (lat,lng): countries):
{chr(10).join(baseline['pois'])}"""


def parse_json(text: str) -> dict:
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    body = fences[-1] if fences else text[text.find("{"): text.rfind("}") + 1]
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


# ---------------------------------------------------------------- merge (pure, tested locally)
def _txt(v, n=400) -> str:
    return str(v).strip()[:n]


def _isos(v) -> list[str]:
    return [c for c in (str(x).upper() for x in (v or [])) if ISO.match(c)]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80]


def merge(live: dict, upd: dict, lookup: dict, t_utc: dt.datetime) -> int:
    today = t_utc.date().isoformat()
    for k in ("relations", "threads", "pois", "politics"):
        live.setdefault(k, {})

    def src(x) -> str | None:
        h = lookup.get(str(x.get("src", "")).strip())
        return h["u"] if h and str(h.get("u", "")).startswith("https://") else None

    changes = 0
    for r in upd.get("relations") or []:
        a, b, s, u = str(r.get("a", "")).upper(), str(r.get("b", "")).upper(), r.get("s"), src(r)
        if not (ISO.match(a) and ISO.match(b)) or a == b or s not in STATUSES or not u:
            continue
        a, b = sorted((a, b))
        live["relations"][f"{a}-{b}"] = {"a": a, "b": b, "s": s, "note": _txt(r.get("note", ""), 300),
                                         "updated": today, "source": u}
        changes += 1

    for t in upd.get("threads") or []:
        tid, u = slug(str(t.get("id") or t.get("n") or "")), src(t)
        if not tid or not u:
            continue
        doc = dict(live["threads"].get(tid, {}))
        if t.get("t") in THREAD_TYPES: doc["t"] = t["t"]
        if t.get("n"): doc["n"] = _txt(t["n"], 120)
        if t.get("d"): doc["d"] = _txt(t["d"], 700)
        cs = _isos(t.get("c"))
        if len(cs) >= 2: doc["c"] = cs
        doc.update(updated=today, source=u)
        live["threads"][tid] = doc
        changes += 1

    for p in upd.get("pois") or []:
        name, u = _txt(p.get("n", ""), 120), src(p)
        if not name or not u:
            continue
        key = slug(name)
        doc = dict(live["pois"].get(key, {}))
        doc["n"] = name
        if p.get("t") in POI_TYPES: doc["t"] = p["t"]
        for f, n in (("s", 200), ("d", 500)):
            if p.get(f): doc[f] = _txt(p[f], n)
        try:
            lat, lng = float(p["lat"]), float(p["lng"])
            if -90 <= lat <= 90 and -180 <= lng <= 180 and (lat, lng) != (0.0, 0.0): doc.update(lat=lat, lng=lng)
        except (KeyError, TypeError, ValueError):
            pass
        if p.get("p"): doc["p"] = _isos(p["p"])
        doc.update(updated=today, source=u)
        live["pois"][key] = doc
        changes += 1

    for c in upd.get("politics") or []:
        iso, u = str(c.get("iso", "")).upper(), src(c)
        fields = {f: _txt(c[f], 200) for f in ("lastelec", "nextelec") if c.get(f)}
        if not ISO.match(iso) or not fields or not u:
            continue
        live["politics"][iso] = {**live["politics"].get(iso, {}), **fields, "updated": today, "source": u}
        changes += 1

    meta = live.setdefault("meta", {})
    meta["lastRun"] = iso_z(t_utc)
    meta["summary"] = _txt(upd.get("summary") or "Checked the headlines; no significant changes.", 300)
    items = []
    for i in upd.get("log") or []:
        if isinstance(i, dict) and i.get("label"):
            item = {"label": _txt(i["label"], 120), "change": _txt(i.get("change", ""), 240)}
            if src(i): item["source"] = src(i)
            items.append(item)
    log = meta.setdefault("log", [])
    if log and log[0].get("date") == today:
        log[0].setdefault("items", []).extend(items)
    else:
        log.insert(0, {"date": today, "items": items})
    meta["log"] = log[:30]
    return changes


# ---------------------------------------------------------------- main
def main() -> None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("No GEMINI_API_KEY secret set; skipping the AI update.")
        return
    t0 = now_utc()
    live = load_json("live.json", {})
    news = load_json("news.json", {})
    if scheduled():
        if not ran_recently(news.get("generated"), t0, hours=1):
            print("Headlines weren't refreshed in this run; skipping the AI update.")
            return
        if ran_recently((live.get("meta") or {}).get("lastRun"), t0, hours=3):
            print("AI update already ran in this slot; skipping.")
            return
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        baseline = read_baseline(f.read())
    table, lookup = headline_table(news)
    if not lookup:
        sys.exit("No headlines available; run news.py first.")
    text = call_gemini(key, SYSTEM, build_prompt(live, baseline, table, t0))
    try:
        upd = parse_json(text)
    except (ValueError, json.JSONDecodeError) as e:
        print(text[-2000:])
        sys.exit(f"Could not parse Gemini's JSON ({e}); nothing written.")
    n = merge(live, upd, lookup, t0)
    save_json("live.json", live)
    print(f"Wrote {n} change(s). Summary: {live['meta']['summary']}")


if __name__ == "__main__":
    main()
