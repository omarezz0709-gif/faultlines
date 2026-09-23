"""Refresh the globe's live data from the news, using Claude with web search.

Runs in GitHub Actions three times a day (00:00, 12:00, 18:00 Berlin time) and
writes data/live.json, which index.html loads on top of its built-in baseline.

Env:
  ANTHROPIC_API_KEY   required (GitHub secret)
  FAULTLINES_MODEL    optional, default claude-opus-5
  GITHUB_EVENT_NAME   set by Actions; scheduled runs only proceed in a Berlin time slot
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_PATH = os.path.join(ROOT, "data", "live.json")
INDEX_PATH = os.path.join(ROOT, "index.html")

MODEL = os.environ.get("FAULTLINES_MODEL") or "claude-opus-5"
BERLIN = ZoneInfo("Europe/Berlin")
SLOTS = (0, 12, 18)            # Berlin hours to refresh at
SLOT_WINDOW_MIN = 90           # GitHub's scheduler can start runs late
MIN_GAP_HOURS = 3              # skip if a run already happened in this slot

STATUSES = {"A", "F", "N", "T", "H", "W", "none"}
THREAD_TYPES = {"security", "trafficking", "migration", "proxy", "resource", "economy"}
POI_TYPES = {"strait", "land", "flash", "base", "resource"}
POLITICS_FIELDS = ("system", "hos", "hog", "ruling", "lastelec", "nextelec")
ISO = re.compile(r"^[A-Z]{3}$")


# ---------------------------------------------------------------- scheduling
def in_slot(now_berlin: dt.datetime) -> bool:
    for h in SLOTS:
        start = now_berlin.replace(hour=h, minute=0, second=0, microsecond=0)
        if start <= now_berlin < start + dt.timedelta(minutes=SLOT_WINDOW_MIN):
            return True
    return False


def should_run(live: dict, now_utc: dt.datetime, event: str | None) -> tuple[bool, str]:
    if event != "schedule":
        return True, "manual run"
    if not in_slot(now_utc.astimezone(BERLIN)):
        return False, "outside the 00:00 / 12:00 / 18:00 Berlin slots (daylight-saving twin trigger)"
    last = (live.get("meta") or {}).get("lastRun")
    if last:
        try:
            last_dt = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
            if now_utc - last_dt < dt.timedelta(hours=MIN_GAP_HOURS):
                return False, f"already refreshed at {last}"
        except ValueError:
            pass
    return True, "scheduled slot"


# ---------------------------------------------------------------- baseline from index.html
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


# ---------------------------------------------------------------- prompt
SYSTEM = """You maintain "Faultlines", a public interactive globe of international relations. It has a built-in baseline (relations between countries, "threads" = indirect transnational links such as militant networks, smuggling and migration routes, proxy wars, water/food/energy and money flows, strategic locations, and per-country politics) plus live overrides that you produce.

Your job on each run: use web search to find SIGNIFICANT, well-sourced world news published since the previous run, and return the changes as JSON.

Rules:
- Be conservative. Change a relationship status only when an event clearly moves it (ties cut or restored, war starts or ends, formal alliance signed, coup). Routine rhetoric is not a change, but you may refresh a note to mention a notable new development. A typical run has 0-3 changes plus politics backfill.
- Prefer reputable outlets (Reuters, AP, BBC, AFP, FT, Al Jazeera, official sites) and confirm important claims with at least one of them. Every change needs an https source URL. Never invent facts.
- Status codes: A=Ally (formal/de facto alliance), F=Partner, N=Normal/mixed, T=Tense, H=Hostile, W=Armed conflict; "none" removes a baseline pair.
- Countries use ISO3 codes (special: XKX Kosovo, NCY Northern Cyprus, SOL Somaliland, PSE Palestine, TWN Taiwan, ESH Western Sahara).
- Thread types: security, trafficking, migration, proxy, resource, economy. Thread descriptions explain the mechanism as a chain: what happens where, how it moves, who it hits and who is watching. When updating a baseline thread, give its full current countries list and description.
- Location types: strait, land, flash, base, resource. To update a baseline location use its exact name and only the fields that change.
- Politics: update any country that held a national election, changed government or leader, or had a coup since the last run; also backfill up to 5 countries that have no politics entry yet (G20 members first, then countries in the news). Verify each field.
- Spell out abbreviations in parentheses on first use in every note or description, e.g. "JNIM (Jama'at Nusrat al-Islam wal-Muslimin)".
- Notes: one sentence, max 140 characters, present tense.

Finish with ONLY a JSON object in a ```json code block, shaped like this (omit empty lists):
{
  "summary": "one sentence about this check",
  "relations": [{"a": "ISO", "b": "ISO", "s": "T", "note": "...", "source": "https://..."}],
  "threads": [{"id": "kebab-id", "t": "security", "n": "Name", "c": ["ISO", "ISO"], "d": "...", "source": "https://..."}],
  "pois": [{"n": "Name", "t": "strait", "lat": 0.0, "lng": 0.0, "p": ["ISO"], "s": "key stat", "d": "...", "source": "https://..."}],
  "politics": [{"iso": "DEU", "system": "...", "hos": "name and title", "hog": "name and title", "ruling": "party (ABBR)", "lastelec": "type, month year, winner", "nextelec": "type and date", "source": "https://..."}],
  "log": [{"label": "Country A–Country B, thread, location or country", "change": "e.g. Tense → Hostile: short reason", "source": "https://..."}]
}
Only log politics backfills when something actually changed."""


def build_prompt(live: dict, baseline: dict, now_utc: dt.datetime) -> str:
    meta = live.get("meta") or {}
    since = meta.get("lastRun") or (now_utc - dt.timedelta(hours=24)).isoformat()
    overrides = {k: live.get(k, {}) for k in ("relations", "threads", "pois")}
    have_politics = sorted((live.get("politics") or {}).keys())
    return f"""Now: {now_utc.strftime('%Y-%m-%d %H:%M')} UTC. Previous run: {since}. Look for news published since then.

Current live overrides (already applied on top of the baseline):
{json.dumps(overrides, ensure_ascii=False)}

Countries that already have a verified politics entry: {', '.join(have_politics) or 'none'}

Baseline relations (A B STATUS note):
{baseline['relations']}

Baseline threads (id [type] name: countries):
{chr(10).join(baseline['threads'])}

Baseline locations (name [type] (lat,lng): countries):
{chr(10).join(baseline['pois'])}"""


# ---------------------------------------------------------------- Claude call
def call_claude(prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    haiku = MODEL.startswith("claude-haiku")
    tools = [{"type": "web_search_20250305" if haiku else "web_search_20260209", "name": "web_search", "max_uses": 25}]
    params: dict = dict(model=MODEL, max_tokens=64000, system=SYSTEM, tools=tools)
    if not haiku:
        params["output_config"] = {"effort": "high"}
    if MODEL == "claude-opus-5":
        # re-run on Anthropic's recommended fallback model if a safety classifier declines
        params["betas"] = ["server-side-fallback-2026-07-01"]
        params["fallbacks"] = "default"

    messages: list = [{"role": "user", "content": prompt}]
    for _ in range(8):  # web search can pause long turns; resume a few times
        with client.beta.messages.stream(messages=messages, **params) as stream:
            msg = stream.get_final_message()
        print(f"stop_reason={msg.stop_reason} model={msg.model} "
              f"in={msg.usage.input_tokens} out={msg.usage.output_tokens}", flush=True)
        if msg.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": msg.content})
            continue
        if msg.stop_reason == "refusal":
            raise SystemExit("Claude declined this run (refusal); nothing written.")
        return "".join(b.text for b in msg.content if b.type == "text")
    raise SystemExit("Run kept pausing; nothing written.")


def parse_json(text: str) -> dict:
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    body = fences[-1] if fences else text[text.find("{"): text.rfind("}") + 1]
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


# ---------------------------------------------------------------- merge (pure, tested locally)
def _https(u) -> str | None:
    return u if isinstance(u, str) and u.startswith("https://") else None


def _txt(v, n=400) -> str:
    return str(v).strip()[:n]


def _isos(v) -> list[str]:
    return [c for c in (str(x).upper() for x in (v or [])) if ISO.match(c)]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:80]


def merge(live: dict, upd: dict, now_utc: dt.datetime) -> int:
    today = now_utc.date().isoformat()
    for k in ("relations", "threads", "pois", "politics"):
        live.setdefault(k, {})
    changes = 0

    for r in upd.get("relations") or []:
        a, b, s = str(r.get("a", "")).upper(), str(r.get("b", "")).upper(), r.get("s")
        if not (ISO.match(a) and ISO.match(b)) or a == b or s not in STATUSES:
            continue
        a, b = sorted((a, b))
        live["relations"][f"{a}-{b}"] = {"a": a, "b": b, "s": s, "note": _txt(r.get("note", ""), 300),
                                         "updated": today, "source": _https(r.get("source"))}
        changes += 1

    for t in upd.get("threads") or []:
        tid = slug(str(t.get("id") or t.get("n") or ""))
        if not tid:
            continue
        if t.get("remove"):
            live["threads"][tid] = {"remove": True, "updated": today}
        else:
            doc = dict(live["threads"].get(tid, {}))
            doc.pop("remove", None)
            if t.get("t") in THREAD_TYPES: doc["t"] = t["t"]
            if t.get("n"): doc["n"] = _txt(t["n"], 120)
            if t.get("d"): doc["d"] = _txt(t["d"], 700)
            cs = _isos(t.get("c"))
            if len(cs) >= 2: doc["c"] = cs
            doc.update(updated=today, source=_https(t.get("source")))
            live["threads"][tid] = doc
        changes += 1

    for p in upd.get("pois") or []:
        name = _txt(p.get("n", ""), 120)
        if not name:
            continue
        key = slug(name)
        if p.get("remove"):
            live["pois"][key] = {"n": name, "remove": True, "updated": today}
        else:
            doc = dict(live["pois"].get(key, {}))
            doc.pop("remove", None)
            doc["n"] = name
            if p.get("t") in POI_TYPES: doc["t"] = p["t"]
            for f, n in (("s", 200), ("d", 500)):
                if p.get(f): doc[f] = _txt(p[f], n)
            try:
                lat, lng = float(p["lat"]), float(p["lng"])
                if -90 <= lat <= 90 and -180 <= lng <= 180: doc.update(lat=lat, lng=lng)
            except (KeyError, TypeError, ValueError):
                pass
            if p.get("p"): doc["p"] = _isos(p["p"])
            doc.update(updated=today, source=_https(p.get("source")))
            live["pois"][key] = doc
        changes += 1

    for c in upd.get("politics") or []:
        iso = str(c.get("iso", "")).upper()
        if not ISO.match(iso):
            continue
        fields = {f: _txt(c[f], 200) for f in POLITICS_FIELDS if c.get(f)}
        if not fields:
            continue
        doc = {**live["politics"].get(iso, {}), **fields, "updated": today, "source": _https(c.get("source"))}
        live["politics"][iso] = doc
        changes += 1

    meta = live.setdefault("meta", {})
    meta["lastRun"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["summary"] = _txt(upd.get("summary") or "Checked the news; no significant changes.", 300)
    items = [{"label": _txt(i.get("label", ""), 120), "change": _txt(i.get("change", ""), 240),
              **({"source": _https(i.get("source"))} if _https(i.get("source")) else {})}
             for i in (upd.get("log") or []) if isinstance(i, dict) and i.get("label")]
    log = meta.setdefault("log", [])
    if log and log[0].get("date") == today:
        log[0].setdefault("items", []).extend(items)
    else:
        log.insert(0, {"date": today, "items": items})
    meta["log"] = log[:30]
    return changes


# ---------------------------------------------------------------- main
def main() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    with open(LIVE_PATH, encoding="utf-8") as f:
        live = json.load(f)
    ok, why = should_run(live, now, os.environ.get("GITHUB_EVENT_NAME"))
    print(f"{'Running' if ok else 'Skipping'}: {why}", flush=True)
    if not ok:
        return
    with open(INDEX_PATH, encoding="utf-8") as f:
        baseline = read_baseline(f.read())
    text = call_claude(build_prompt(live, baseline, now))
    try:
        upd = parse_json(text)
    except (ValueError, json.JSONDecodeError) as e:
        print(text[-3000:])
        raise SystemExit(f"Could not parse Claude's JSON ({e}); nothing written.")
    n = merge(live, upd, now)
    with open(LIVE_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(live, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"Wrote {n} change(s). Summary: {live['meta']['summary']}")


if __name__ == "__main__":
    main()
