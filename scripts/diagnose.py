"""Daily diagnostics (GitHub Actions, 12:30 and 18:30 Berlin time): checks the live site, its data, the AI Worker,
every news source and a real browser run, repairs what it can (a stale refresh or a broken Worker deploy is re-run, a
hanging refresh run is cancelled), looks again once the repairs are done, and writes data/health.json (shown in the
admin panel). The owner only hears about what is still broken after that: see write_email.

Statuses: ok, warn (worth a look, the site still works), fail (something visitors notice).
Run by hand:  python scripts/diagnose.py            (add --no-browser to skip the browser test)
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

from common import BERLIN, ROOT, iso_z, load_json, now_utc, save_json, scheduled

SITE = "https://faultlines-1pw.pages.dev"
MIRROR = "https://omarezz0709-gif.github.io/faultlines/"
WORKER = "https://faultlines-ai.omarezz0709.workers.dev"
ORIGIN = SITE
LANGS = ("en", "de", "fr", "es", "it", "ar")
UA = "Mozilla/5.0 (compatible; Faultlines diagnostics)"
LASTING_H = 20     # a warning this old (three checks in a row, about a day) needs the owner

checks: list[dict] = []
heal: list[str] = []


def add(group: str, name: str, status: str, detail: str = "", ms: int | None = None) -> None:
    checks.append({"g": group, "name": name, "status": status, "detail": detail[:300], **({"ms": ms} if ms is not None else {})})
    mark = {"ok": "✓", "warn": "!", "fail": "✗"}[status]
    print(f"{mark} [{group}] {name}: {detail}" + (f" ({ms} ms)" if ms is not None else ""), flush=True)


def fetch(url: str, *, headers: dict | None = None, method: str = "GET", body: bytes | None = None, timeout: int = 20):
    """(status, bytes, headers, ms); status 0 = no connection."""
    t0 = time.time()
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
            if b[:2] == b"\x1f\x8b":   # some servers (UN News) send gzip without saying so
                b = gzip.decompress(b)
            return r.status, b, dict(r.headers), int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {}), int((time.time() - t0) * 1000)
    except Exception as e:  # timeout, DNS, TLS ...
        return 0, str(e).encode(), {}, int((time.time() - t0) * 1000)


def age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return (now_utc() - dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600
    except ValueError:
        return None


# ---------------------------------------------------------------- 1. site and data
def check_site() -> dict:
    s, b, _, ms = fetch(SITE + "/")
    add("site", "Website", "ok" if s == 200 and b"Faultlines" in b else "fail", f"HTTP {s}, {len(b) // 1024} KB", ms)
    s, _, _, ms = fetch(MIRROR)
    mirror_ok = s == 200
    add("site", "GitHub Pages copy", "ok" if mirror_ok else "warn", f"HTTP {s}", ms)
    data = {}
    for name in ("news", "live", "politics", "wb", "geo", "cities"):
        s, b, _, ms = fetch(f"{SITE}/data/{name}.json?diag={int(time.time())}")
        try:
            data[name] = json.loads(b) if s == 200 else None
        except ValueError:
            data[name] = None
        add("site", f"data/{name}.json", "ok" if data[name] is not None else "fail",
            f"HTTP {s}, {len(b) // 1024} KB" if data[name] is not None else f"HTTP {s}, not valid JSON", ms)
    # the copy is published by the refresh run's last step: if that step hangs, the copy falls behind
    if mirror_ok and data.get("news"):
        s, b, _, _ = fetch(f"{MIRROR}data/news.json?diag={int(time.time())}")
        try:
            lag = age_hours(json.loads(b).get("generated")) - age_hours(data["news"].get("generated"))
            add("site", "GitHub Pages copy is up to date", "ok" if lag < 8 else "warn",
                f"{lag:.1f} h behind the main site" if lag >= 0.1 else "same data as the main site")
        except (ValueError, TypeError, AttributeError):
            add("site", "GitHub Pages copy is up to date", "warn", f"HTTP {s}, could not read its data")
    return data


def check_fresh(data: dict) -> list[str]:
    """Returns what is stale enough to re-run ('refresh')."""
    stale = []
    news, live, pol, wb = (data.get(k) or {} for k in ("news", "live", "politics", "wb"))
    # refresh slots are 00, 07, 12, 18 Berlin: the longest gap is 7 h, plus GitHub's delays
    for label, iso, warn_h, fail_h in (("Headlines (news.json)", news.get("generated"), 8, 14),
                                       ("AI relationship update (live.json)", (live.get("meta") or {}).get("lastRun"), 8, 14),
                                       ("Leaders (politics.json)", pol.get("generated"), 30, 54)):
        h = age_hours(iso)
        if h is None:
            add("fresh", label, "warn", "no timestamp")
            continue
        st = "ok" if h <= warn_h else "warn" if h <= fail_h else "fail"
        add("fresh", label, st, f"last update {h:.1f} h ago")
        if st != "ok":   # stale headlines, AI update or leaders: a manual refresh run catches up on all of them
            stale.append("refresh")
    countries = sum(1 for c in (news.get("countries") or {}).values() if c.get("i"))
    add("fresh", "Countries with confirmed headlines", "ok" if countries >= 100 else "warn", f"{countries}")
    add("fresh", "Headlines to watch", "ok" if len(news.get("alerts") or []) >= 5 else "warn", f"{len(news.get('alerts') or [])}")
    add("fresh", "Leaders known", "ok" if len(pol.get("countries") or {}) >= 150 else "warn", f"{len(pol.get('countries') or {})} countries")
    # World Bank figures are yearly data without a timestamp: check they cover the world
    add("fresh", "World Bank figures", "ok" if len(wb) >= 150 else "warn", f"{len(wb)} countries and regions")
    return stale


# ---------------------------------------------------------------- 2. the AI Worker
def check_worker() -> bool:
    """Returns True when the Worker looks broken (a redeploy may fix it)."""
    h = {"Origin": ORIGIN}
    broken = 0
    for lang in LANGS:
        for route, key, q in (("live", "items", ""), ("top", "stories", "&q=&en="), ("videos", "videos", "&q=&en=")):
            s, b, hd, ms = fetch(f"{WORKER}/{route}?lang={lang}{q}", headers=h, timeout=30)
            try:
                n = len(json.loads(b).get(key) or []) if s == 200 else -1
            except ValueError:
                n = -1
            if s >= 500 or s == 0:
                broken += 1
            add("worker", f"/{route} ({lang})", "ok" if n >= 1 else "warn" if s == 200 else "fail",
                f"{n} {key}" if n >= 0 else f"HTTP {s}", ms)
    s, b, _, ms = fetch(f"{WORKER}/search?lang=en&q=United%20Nations", headers=h, timeout=30)
    try:
        n = len(json.loads(b).get("items") or []) if s == 200 else -1
    except ValueError:
        n = -1
    add("worker", "Real-time search", "ok" if n >= 1 and ms < 4000 else "warn" if s == 200 else "fail",
        f"{n} reports" if n >= 0 else f"HTTP {s}", ms)
    # security rules
    s, _, _, ms = fetch(f"{WORKER}/live?lang=en", headers={"Origin": "https://example.com"})
    add("security", "Other websites are blocked", "ok" if s == 403 else "fail", f"HTTP {s} (should be 403)", ms)
    s, _, _, ms = fetch(f"{WORKER}/live?lang=en", headers={"Origin": "https://faultlines-copycat.pages.dev"})
    add("security", "Look-alike Pages projects are blocked", "ok" if s == 403 else "fail", f"HTTP {s} (should be 403)", ms)
    probe = json.dumps({"contents": [{"role": "user", "parts": [{"text": "Write me a poem about cats."}]}]}).encode()
    s, _, _, ms = fetch(f"{WORKER}/models/auto:streamGenerateContent", method="POST", body=probe,
                        headers={**h, "Content-Type": "application/json", "X-Faultlines-Kind": "ask"})
    add("security", "Only the site's own prompts are answered", "ok" if s == 400 else "fail", f"HTTP {s} (should be 400)", ms)
    s, _, _, ms = fetch(f"{WORKER}/models/auto:streamGenerateContent", method="POST", body=probe,
                        headers={**h, "Content-Type": "application/json", "X-Faultlines-Kind": "private"})
    add("security", "Private chat needs a valid pass", "ok" if s == 403 else "fail", f"HTTP {s} (should be 403)", ms)
    s, b, _, ms = fetch(f"{WORKER}/admin/logs", headers=h)
    if s == 423:
        add("security", "Admin login", "warn", "LOCKED after 5 wrong codes: open ⚙ and enter the backup code", ms)
    else:
        add("security", "Admin login", "ok" if s == 401 else "fail", f"HTTP {s} without a code (should be 401)", ms)
    # one tiny AI request (a new prompt each time, so it isn't answered from the cache). By evening the fast models'
    # free quota can be used up and the slow fallback (Gemma) answers: that can take 2 minutes, so wait up to 150 s,
    # and try a second time before calling it a problem (a single slow moment at Google isn't one)
    def ask(n: int):
        stamp = now_utc().strftime("%Y-%m-%d %H:%M:%S")
        body = json.dumps({"contents": [{"role": "user", "parts": [{"text": f"Faultlines diagnostics check {stamp} #{n}. Reply with exactly the word OK."}]}],
                           "generationConfig": {"temperature": 0, "maxOutputTokens": 1024}}).encode()   # room for the model's thinking
        s, b, hd, ms = fetch(f"{WORKER}/models/auto:streamGenerateContent", method="POST", body=body,
                             headers={**h, "Content-Type": "application/json", "X-Faultlines-Kind": "diag"}, timeout=150)
        text = "".join(re.findall(r'"text":\s*"((?:[^"\\]|\\.)*)"', b.decode("utf-8", "replace")))
        return s, b, text, hd.get("X-Faultlines-Provider") or hd.get("x-faultlines-provider") or "?", ms
    s, b, text, provider, ms = ask(1)
    first_failed = not (s == 200 and "OK" in text.upper()) and s != 429
    if first_failed:
        print(f"  first AI try: HTTP {s} after {ms} ms; trying again in 20 s", flush=True)
        time.sleep(20)
        s, b, text, provider, ms = ask(2)
    if s == 200 and "OK" in text.upper():
        if first_failed:
            add("ai", "AI answers", "warn", f"the first try failed, the second worked ({provider})", ms)
        elif ms > 45_000:
            add("ai", "AI answers", "warn", f"answered by {provider}, but slowly ({ms // 1000} s): the fast models' free quota is probably used up for today", ms)
        else:
            add("ai", "AI answers", "ok", f"answered by {provider}", ms)
    elif s == 429:
        add("ai", "AI answers", "warn", "the free AI quota is used up right now (it refills during the day)", ms)
    else:
        add("ai", "AI answers", "fail", f"no answer twice in a row: HTTP {s}: {text[:80] or b[:120].decode('utf-8', 'replace')}", ms)
        broken += 1
    return broken >= 5


# ---------------------------------------------------------------- 3. news sources and video channels
def check_sources() -> None:
    src = open(os.path.join(ROOT, "worker", "gemini-proxy.js"), encoding="utf-8").read()
    outlets = src[src.index("const OUTLETS"):src.index("};", src.index("const OUTLETS"))]
    # each feed with its language ("France 24 FR"), since some outlets have a feed in several languages
    langs = [(m.start(), m.group(1)) for m in re.finditer(r"\b(en|fr|es|ar|de|it): \[\[", outlets)]
    feeds = [(f"{m.group(1)} {next((l for p, l in reversed(langs) if p < m.start()), '').upper()}".strip(), m.group(2))
             for m in re.finditer(r'\["([^"]+)", "(https://[^"]+)"\]', outlets)]
    channels = src[src.index("const CHANNELS"):src.index("};", src.index("const CHANNELS"))]
    yts = sorted(set(re.findall(r'\["([^"]+)", "(UC[\w-]{22})"\]', channels)))

    def working(url: str, ok) -> tuple[bool, int]:
        """A feed counts as not working only if it fails twice: one-off hiccups are common and fix themselves."""
        for attempt in (1, 2):
            s, b, _, _ = fetch(url, timeout=12)
            if s == 200 and ok(b):
                return True, s
            if attempt == 1:
                time.sleep(3)
        return False, s

    bad = []
    for name, url in feeds:
        good, s = working(url, lambda b: re.search(rb"<(item|entry)[\s>]", b))
        if not good:
            bad.append((name, s))
    share = len(bad) / max(1, len(feeds))
    add("sources", "News outlet feeds", "ok" if not bad else "warn" if share < 0.3 else "fail",
        f"{len(feeds) - len(bad)}/{len(feeds)} working" + (f"; not working: {', '.join(f'{n} ({s})' for n, s in bad[:8])}" if bad else ""))
    checks[-1]["items"] = [n for n, _ in bad]   # which ones, so the email can tell a lasting problem from a new one
    bad = []
    for name, cid in yts:
        good, s = working(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", lambda b: b"<entry>" in b)
        if not good:
            bad.append(f"{name} ({s})")
    add("sources", "YouTube news channels", "ok" if not bad else "warn" if len(bad) < len(yts) * 0.3 else "fail",
        f"{len(yts) - len(bad)}/{len(yts)} working" + (f"; not working: {', '.join(bad[:8])}" if bad else ""))
    checks[-1]["items"] = [re.sub(r" \(\d+\)$", "", x) for x in bad]


# ---------------------------------------------------------------- 4. a real browser
BROWSER_TEST = r"""
async () => {
  const E = [], bad = [];
  addEventListener('error', e => E.push(e.message));
  addEventListener('unhandledrejection', e => E.push(String(e.reason && e.reason.message || e.reason)));
  const BAD = /\bundefined\b|\bNaN\b|\[object|\$\{/;
  const chk = n => { const m = document.body.innerText.match(BAD); if (m) bad.push(n); };
  const step = async (n, f) => { try { await f(); } catch(e){ E.push(n + ': ' + e.message); } await new Promise(r => setTimeout(r, 5)); };
  const ids = Object.keys(ENT).filter(k => ENT[k] && ENT[k].name);
  for (const l of ['en', 'de', 'fr', 'es', 'it', 'ar']) {
    await step('lang ' + l, () => setLang(l));
    for (const id of ids) { await step(l + ' ' + id, () => selectCountry(id)); chk(l + ' ' + id); }
    for (const p of POIS) { await step(l + ' ' + p.n, () => selectPoi(p)); chk(l + ' ' + p.n); }
    await step(l + ' compare', () => startCompare(['USA', 'CHN', 'RUS', 'IRN'], ['Strait of Hormuz'])); chk(l + ' compare');
    await step(l + ' feed', () => renderFeed()); chk(l + ' feed');
    await step(l + ' updates', () => renderChanges()); chk(l + ' updates');
    await step(l + ' legend', () => { toggleLegend(true); chk(l + ' legend'); toggleLegend(false); });
    await step(l + ' tour', () => { startTour(); for (let k = 0; k < 14 && tour; k++) tourGo(tour.i + 1); });
    for (const t of ['light', 'dark']) await step(l + ' ' + t, () => applyTheme(t));
    await step(l + ' intro', () => renderIntro(true)); chk(l + ' intro');
  }
  setLang('en');
  return {countries: ids.length, places: POIS.length, errors: [...new Set(E)].slice(0, 10), nErrors: E.length, bad: bad.slice(0, 10), nBad: bad.length};
}
"""


def check_browser() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        add("browser", "Browser test", "warn", "Playwright not installed; skipped")
        return
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
        # desktop: load, then click through everything in every language
        page = b.new_page(viewport={"width": 1280, "height": 800})
        js_errors, console = [], []
        page.on("pageerror", lambda e: js_errors.append(str(e)[:200]))
        page.on("console", lambda m: console.append(m.text[:200]) if m.type == "error" else None)
        t0 = time.time()
        try:
            page.goto(SITE + "/?notour", wait_until="load", timeout=60000)
            page.wait_for_function("() => typeof globe !== 'undefined' && typeof ENT !== 'undefined' && document.querySelector('#globe canvas')", timeout=30000)
            add("browser", "Page loads and the globe starts", "ok", "", int((time.time() - t0) * 1000))
        except Exception as e:
            add("browser", "Page loads and the globe starts", "fail", str(e)[:200])
            b.close()
            return
        page.wait_for_timeout(3000)
        try:
            r = page.evaluate(BROWSER_TEST)
            add("browser", "Every country, place and panel in 6 languages",
                "ok" if not r["nErrors"] and not r["nBad"] else "fail",
                f"{r['countries']} countries, {r['places']} places: {r['nErrors']} errors, {r['nBad']} panels with broken text"
                + (f"; e.g. {'; '.join(r['errors'][:3] or r['bad'][:3])}" if r["nErrors"] or r["nBad"] else ""))
        except Exception as e:
            add("browser", "Every country, place and panel in 6 languages", "fail", str(e)[:200])
        add("browser", "Script errors while loading", "ok" if not js_errors else "fail",
            "none" if not js_errors else "; ".join(js_errors[:3]))
        net = [c for c in console if "Failed to load resource" in c]
        other = [c for c in console if c not in net]
        add("browser", "Console errors", "ok" if not other else "warn",
            f"{len(net)} missing images/files" + (f"; {'; '.join(other[:2])}" if other else ""))
        page.close()
        # phone and tablet: nothing wider than the screen
        for label, w, hgt in (("Phone layout (375 px)", 375, 812), ("Small phone (320 px)", 320, 640), ("Tablet (820 px)", 820, 1180)):
            pg = b.new_page(viewport={"width": w, "height": hgt}, is_mobile=w < 900, has_touch=w < 900)
            try:
                pg.goto(SITE + "/?notour", wait_until="load", timeout=60000)
                pg.wait_for_function("() => typeof globe !== 'undefined'", timeout=30000)
                pg.wait_for_timeout(2500)
                sw = pg.evaluate("() => document.documentElement.scrollWidth")
                add("browser", label, "ok" if sw <= w + 1 else "fail", f"page width {sw} px on a {w} px screen")
            except Exception as e:
                add("browser", label, "fail", str(e)[:200])
            pg.close()
        b.close()


# ---------------------------------------------------------------- repairs, report
def trigger(workflow: str) -> None:
    """Re-run another workflow (needs GH_TOKEN; available in GitHub Actions)."""
    if not os.environ.get("GH_TOKEN"):
        return
    r = subprocess.run(["gh", "workflow", "run", workflow], capture_output=True, text=True)
    heal.append(f"re-ran {workflow}" + ("" if r.returncode == 0 else f" (failed: {r.stderr.strip()[:120]})"))
    print(f"repair: gh workflow run {workflow} -> {r.returncode}", flush=True)


def wait_for(workflow: str, minutes: int = 12) -> None:
    """Waits until a repair run has finished (a data refresh takes a few minutes)."""
    if not os.environ.get("GH_TOKEN"):
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "omarezz0709-gif/faultlines")
    time.sleep(20)   # a new run takes a moment to show up
    end = time.time() + minutes * 60
    while time.time() < end:
        r = subprocess.run(["gh", "run", "list", "--repo", repo, "--workflow", workflow, "--limit", "5", "--json", "status"],
                           capture_output=True, text=True)
        try:
            if r.returncode == 0 and all(x.get("status") == "completed" for x in json.loads(r.stdout or "[]")):
                return
        except ValueError:
            pass
        time.sleep(20)
    print(f"repair: {workflow} still running after {minutes} minutes", flush=True)


def unstick_refresh() -> None:
    """Only one refresh runs at a time, so a run that hangs holds up every later one (on 7 Oct GitHub left one
    'waiting' for 30 hours and every refresh after it was cancelled). Cancel any that has been going for over an hour;
    a normal run takes a few minutes."""
    if not os.environ.get("GH_TOKEN"):
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "omarezz0709-gif/faultlines")
    r = subprocess.run(["gh", "run", "list", "--repo", repo, "--workflow", "refresh.yml", "--limit", "30",
                        "--json", "databaseId,status,createdAt"], capture_output=True, text=True)
    if r.returncode:
        add("runs", "Refresh runs", "warn", f"could not list them: {r.stderr.strip()[:150]}")
        return
    stuck = [x for x in json.loads(r.stdout or "[]")
             if x.get("status") != "completed" and (age_hours(x.get("createdAt")) or 0) > 1]
    refused = 0
    for x in stuck:
        run = str(x["databaseId"])
        c = subprocess.run(["gh", "run", "cancel", run, "--repo", repo], capture_output=True, text=True)
        if c.returncode:   # a run stuck before its job even started may refuse a normal cancel
            c = subprocess.run(["gh", "api", "-X", "POST", f"repos/{repo}/actions/runs/{run}/force-cancel"],
                               capture_output=True, text=True)
        heal.append(f"cancelled refresh run {run} ({x.get('status')} for {age_hours(x.get('createdAt')):.0f} h)"
                    + ("" if c.returncode == 0 else f" (failed: {c.stderr.strip()[:120]})"))
        print(f"repair: cancel stuck refresh run {run} -> {c.returncode}", flush=True)
        refused += 1 if c.returncode else 0
    add("runs", "Refresh runs", "warn" if refused else "ok",   # repaired = fine; only one that can't be cancelled is left to report
        "none hanging" if not stuck else f"{len(stuck)} hanging run(s) found, {len(stuck) - refused} cancelled")
    if stuck:
        time.sleep(5)   # let GitHub release the queue before a new run is started


def daily_check(t: dt.datetime) -> tuple | None:
    """Which of the day's two checks a moment belongs to: the one from 12:00 or the one from 18:00 Berlin time."""
    b = t.astimezone(BERLIN)
    return (b.date(), 18 if b.hour >= 18 else 12) if b.hour >= 12 else None


def is_due(t0: dt.datetime, prev: dict) -> bool:
    """GitHub often starts scheduled runs hours late (since late October up to 5 hours), so any run after 12:00 or
    18:00 counts for that check, as long as that check hasn't been done yet. After a whole missed day, any run does."""
    h = age_hours(prev.get("generated"))
    if h is None or h >= 20:
        return True
    return daily_check(t0) is not None and daily_check(t0 - dt.timedelta(hours=h)) != daily_check(t0)


PARTS = {"site": ("site", "fresh"), "worker": ("worker", "security", "ai"), "sources": ("sources",), "browser": ("browser",)}


def run_part(part: str) -> str | None:
    """Runs one part of the checks. Returns the workflow that may repair what it found, if any."""
    if part == "site":
        return "refresh.yml" if "refresh" in check_fresh(check_site()) else None
    if part == "worker":
        return "worker.yml" if check_worker() else None
    if part == "sources":
        check_sources()
    elif "--no-browser" not in sys.argv:
        try:
            check_browser()
        except Exception as e:
            add("browser", "Browser test", "warn", f"could not run: {str(e)[:160]}")
    return None


def second_look(repairs: set) -> None:
    """Checks again whatever was repaired or looked broken, after the repairs have finished, so the report and the
    email only show what is really still wrong. A warning without a repair isn't looked at again: warnings only
    matter if they last (see write_email)."""
    again = [p for p, groups in PARTS.items()
             if any(c["g"] in groups and c["status"] == "fail" for c in checks)
             or (p == "site" and "refresh.yml" in repairs) or (p == "worker" and "worker.yml" in repairs)]
    if not again:
        return
    for w in repairs:
        wait_for(w)
    print(f"\nSecond look at: {', '.join(again)}", flush=True)
    before = {c["name"] for c in checks if c["status"] != "ok"}
    time.sleep(120)   # a repair's new data takes a minute or two to reach the site
    checks[:] = [c for c in checks if not any(c["g"] in PARTS[p] for p in again)]
    for p in again:
        run_part(p)
    back = before - {c["name"] for c in checks if c["status"] != "ok"}
    if back:
        heal.append("back to normal: " + ", ".join(sorted(back)))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # ✓ ✗ on any console
    except AttributeError:
        pass
    t0 = now_utc()
    prev = load_json("health.json", {})
    skip = scheduled() and not is_due(t0, prev)
    if "--slot" in sys.argv:   # the workflow asks first, so it doesn't install a browser for nothing
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
            f.write(f"run={'false' if skip else 'true'}\n")
        print("run" if not skip else "skip")
        return
    if skip:
        print("Not a diagnostics slot (or already checked); skipping.")
        return
    repairs = {w for w in map(run_part, PARTS) if w}
    unstick_refresh()
    for w in repairs:
        trigger(w)
    second_look(repairs)
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    status = "fail" if fails else "warn" if warns else "ok"
    summary = ("All checks passed." if status == "ok" else
               f"{len(fails)} problem(s), {len(warns)} warning(s): " + "; ".join(f"{c['name']}: {c['detail']}" for c in (fails + warns)[:4]))
    print(f"\n{status.upper()}: {summary}")
    emailed, since = write_email(t0, status, fails, warns, prev)
    save_json("health.json", {"generated": iso_z(t0), "berlin": t0.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M"),
                              "status": status, "summary": summary[:600], "repairs": heal, "checks": checks,
                              "emailed": emailed, "warn_since": since, "ms": int((now_utc() - t0).total_seconds() * 1000)})
    if fails:
        sys.exit(1)   # the workflow fails (shown in red on GitHub)


def warn_keys(c: dict) -> list[str]:
    """What a warning is about: each feed or channel that isn't working, or else the check itself."""
    return [f"{c['name']}: {i}" for i in c.get("items") or []] or [c["name"]]


def write_email(t0, status: str, fails: list, warns: list, prev: dict) -> tuple[list[str], dict]:
    """The report email (to the address in the REPORT_EMAIL secret), only when the owner has to step in:
    - a problem (fail) still there after the repairs and a second look: every time;
    - a locked admin login (only the owner can unlock it): every time;
    - a warning that has lasted about a day (LASTING_H) with no repair working: once. If it goes away and comes back
      later, it counts from the start.
    Anything repaired, or that fixes itself, sends nothing; it's listed in the admin panel.
    Returns the warnings reported so far and when each was first seen (saved in health.json for the next check)."""
    now_keys = {k for c in warns for k in warn_keys(c)}
    seen = prev.get("warn_since") or {}
    since = {k: seen.get(k) or iso_z(t0) for k in sorted(now_keys)}
    reported = set(prev.get("emailed") or []) & now_keys          # still going, already in an earlier email
    lasting = {k for k, iso in since.items() if (age_hours(iso) or 0) >= LASTING_H} - reported
    locked = any(c["name"] == "Admin login" for c in warns)
    send = bool(fails or locked or lasting)
    when = t0.astimezone(BERLIN).strftime("%d %b %Y, %H:%M")
    subject = (f"Faultlines needs you: {len(fails)} problem(s) the automatic repair couldn't fix ({when})" if fails else
               f"Faultlines needs you: admin login locked ({when})" if locked else
               f"Faultlines needs you: a warning has lasted a day ({when})" if lasting else f"Faultlines: all good ({when})")
    icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    lines = [f"Faultlines system check, {when} Berlin time", "",
             "Everything works." if status == "ok" else f"{len(fails)} problem(s), {len(warns)} warning(s):", ""]
    for c in fails + warns:
        keys = set(warn_keys(c)) if c["status"] == "warn" else set()
        name = lambda ks: ", ".join(sorted(k.split(": ", 1)[-1] for k in ks)) if c.get("items") else ""
        note = (f"   (for about a day now{': ' + name(keys & lasting) if name(keys & lasting) else ''})" if keys & lasting
                else "   (already reported)" if keys and keys <= reported
                else "   (only reported if it lasts a day)" if keys else "")
        lines.append(f"  {icon[c['status']]}  {c['name']}: {c['detail']}" + note)
    if heal:
        lines += ["", "Already tried automatically: " + "; ".join(heal)]
    lines += ["", f"{sum(c['status'] == 'ok' for c in checks)} of {len(checks)} checks passed.",
              "", f"Website: {SITE}", "Full report: open the site, ⚙ Usage & logs, System check",
              f"Run a check now: https://github.com/omarezz0709-gif/faultlines/actions/workflows/diagnose.yml",
              "", "(Sent by the daily diagnostics only when you need to step in: a problem the automatic repair couldn't",
              "fix, a locked admin login, or a warning that has lasted a day. Anything repaired sends nothing.)"]
    with open(os.path.join(ROOT, "report-email.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"email={'true' if send else 'false'}\nsubject={subject}\n")
    # Outlook (or any address) without Gmail: through Resend (free; secrets RESEND_API_KEY and REPORT_EMAIL,
    # REPORT_EMAIL being the address the Resend account was opened with)
    key, to = os.environ.get("RESEND_API_KEY", "").strip(), os.environ.get("REPORT_EMAIL", "").strip()
    delivered = send   # (the Gmail way sends after this script, so it can't be checked here)
    if key and to and (send or os.environ.get("TEST_EMAIL") == "true"):
        s, b, _, _ = fetch("https://api.resend.com/emails", method="POST",
                           headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                           body=json.dumps({"from": "Faultlines diagnostics <onboarding@resend.dev>", "to": [to],
                                            "subject": subject, "text": "\n".join(lines)}).encode())
        print(f"report email via Resend: HTTP {s}" + ("" if s in (200, 201) else f" {b[:200].decode('utf-8', 'replace')}"))
        delivered = send and s in (200, 201)
    return sorted(reported | (lasting if delivered else set())), since   # not delivered: tried again at the next check


if __name__ == "__main__":
    main()
