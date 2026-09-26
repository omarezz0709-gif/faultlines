"""Daily diagnostics (GitHub Actions, 12:30 and 18:30 Berlin time): checks the live site, its data, the AI Worker,
every news source and a real browser run, writes data/health.json (shown in the admin panel), repairs what it can
(a stale refresh or a broken Worker deploy is re-run) and fails the job on real problems, so GitHub sends an email.

Statuses: ok, warn (worth a look, the site still works), fail (something visitors notice).
Run by hand:  python scripts/diagnose.py            (add --no-browser to skip the browser test)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

from common import BERLIN, ROOT, in_slot, iso_z, load_json, now_utc, ran_recently, save_json, scheduled

SITE = "https://faultlines-1pw.pages.dev"
MIRROR = "https://omarezz0709-gif.github.io/faultlines/"
WORKER = "https://faultlines-ai.omarezz0709.workers.dev"
ORIGIN = SITE
LANGS = ("en", "de", "fr", "es", "ar")
UA = "Mozilla/5.0 (compatible; Faultlines diagnostics)"

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
            return r.status, r.read(), dict(r.headers), int((time.time() - t0) * 1000)
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
    add("site", "GitHub Pages copy", "ok" if s == 200 else "warn", f"HTTP {s}", ms)
    data = {}
    for name in ("news", "live", "politics", "wb", "geo", "cities"):
        s, b, _, ms = fetch(f"{SITE}/data/{name}.json?diag={int(time.time())}")
        try:
            data[name] = json.loads(b) if s == 200 else None
        except ValueError:
            data[name] = None
        add("site", f"data/{name}.json", "ok" if data[name] is not None else "fail",
            f"HTTP {s}, {len(b) // 1024} KB" if data[name] is not None else f"HTTP {s}, not valid JSON", ms)
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
        if st != "ok" and label.startswith(("Headlines", "AI")):
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
    s, b, _, ms = fetch(f"{WORKER}/admin/logs", headers=h)
    if s == 423:
        add("security", "Admin login", "warn", "LOCKED after 5 wrong codes: open ⚙ and enter the backup code", ms)
    else:
        add("security", "Admin login", "ok" if s == 401 else "fail", f"HTTP {s} without a code (should be 401)", ms)
    # one tiny AI request (a new prompt each time, so it isn't answered from the cache)
    stamp = now_utc().strftime("%Y-%m-%d %H:%M")
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": f"Diagnostics check {stamp}. Reply with exactly the word OK."}]}],
                       "generationConfig": {"temperature": 0, "maxOutputTokens": 1024}}).encode()   # room for the model's thinking
    s, b, hd, ms = fetch(f"{WORKER}/models/auto:streamGenerateContent", method="POST", body=body,
                         headers={**h, "Content-Type": "application/json", "X-Faultlines-Kind": "diag"}, timeout=60)
    text = "".join(re.findall(r'"text":\s*"((?:[^"\\]|\\.)*)"', b.decode("utf-8", "replace")))
    provider = hd.get("X-Faultlines-Provider") or hd.get("x-faultlines-provider") or "?"
    if s == 200 and "OK" in text.upper():
        add("ai", "AI answers", "ok", f"answered by {provider}", ms)
    elif s == 429:
        add("ai", "AI answers", "warn", "the free AI quota is used up right now (it refills during the day)", ms)
    else:
        add("ai", "AI answers", "fail", f"HTTP {s}: {text[:80] or b[:120].decode('utf-8', 'replace')}", ms)
        broken += 1
    return broken >= 5


# ---------------------------------------------------------------- 3. news sources and video channels
def check_sources() -> None:
    src = open(os.path.join(ROOT, "worker", "gemini-proxy.js"), encoding="utf-8").read()
    outlets = src[src.index("const OUTLETS"):src.index("};", src.index("const OUTLETS"))]
    feeds = re.findall(r'\["([^"]+)", "(https://[^"]+)"\]', outlets)
    channels = src[src.index("const CHANNELS"):src.index("};", src.index("const CHANNELS"))]
    yts = sorted(set(re.findall(r'\["([^"]+)", "(UC[\w-]{22})"\]', channels)))
    bad = []
    for name, url in feeds:
        s, b, _, _ = fetch(url, timeout=12)
        if s != 200 or not re.search(rb"<(item|entry)[\s>]", b):
            bad.append(f"{name} ({s})")
    share = len(bad) / max(1, len(feeds))
    add("sources", "News outlet feeds", "ok" if not bad else "warn" if share < 0.3 else "fail",
        f"{len(feeds) - len(bad)}/{len(feeds)} working" + (f"; not working: {', '.join(bad[:8])}" if bad else ""))
    bad = []
    for name, cid in yts:
        s, b, _, _ = fetch(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}", timeout=12)
        if s != 200 or b"<entry>" not in b:
            bad.append(f"{name} ({s})")
    add("sources", "YouTube news channels", "ok" if not bad else "warn" if len(bad) < len(yts) * 0.3 else "fail",
        f"{len(yts) - len(bad)}/{len(yts)} working" + (f"; not working: {', '.join(bad[:8])}" if bad else ""))


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
  for (const l of ['en', 'de', 'fr', 'es', 'ar']) {
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
            add("browser", "Every country, place and panel in 5 languages",
                "ok" if not r["nErrors"] and not r["nBad"] else "fail",
                f"{r['countries']} countries, {r['places']} places: {r['nErrors']} errors, {r['nBad']} panels with broken text"
                + (f"; e.g. {'; '.join(r['errors'][:3] or r['bad'][:3])}" if r["nErrors"] or r["nBad"] else ""))
        except Exception as e:
            add("browser", "Every country, place and panel in 5 languages", "fail", str(e)[:200])
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


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # ✓ ✗ on any console
    except AttributeError:
        pass
    t0 = now_utc()
    prev = load_json("health.json", {})
    skip = scheduled() and (not in_slot(t0, hours=(12, 18)) or ran_recently(prev.get("generated"), t0, hours=2))
    if "--slot" in sys.argv:   # the workflow asks first, so it doesn't install a browser for nothing
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
            f.write(f"run={'false' if skip else 'true'}\n")
        print("run" if not skip else "skip")
        return
    if skip:
        print("Not a diagnostics slot (or already checked); skipping.")
        return
    data = check_site()
    stale = check_fresh(data)
    worker_broken = check_worker()
    check_sources()
    if "--no-browser" not in sys.argv:
        try:
            check_browser()
        except Exception as e:
            add("browser", "Browser test", "warn", f"could not run: {str(e)[:160]}")
    # repairs
    if "refresh" in stale:
        trigger("refresh.yml")
    if worker_broken:
        trigger("worker.yml")
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    status = "fail" if fails else "warn" if warns else "ok"
    summary = ("All checks passed." if status == "ok" else
               f"{len(fails)} problem(s), {len(warns)} warning(s): " + "; ".join(f"{c['name']}: {c['detail']}" for c in (fails + warns)[:4]))
    save_json("health.json", {"generated": iso_z(t0), "berlin": t0.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M"),
                              "status": status, "summary": summary[:600], "repairs": heal, "checks": checks,
                              "ms": int((now_utc() - t0).total_seconds() * 1000)})
    print(f"\n{status.upper()}: {summary}")
    if fails:
        sys.exit(1)   # the workflow fails, so GitHub emails the owner


if __name__ == "__main__":
    main()
