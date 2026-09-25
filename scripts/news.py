"""Pull the latest headlines for every country from Google News RSS (free, no key).

Writes data/news.json:
  generated   ISO time of this run
  items       [{t, s, u, d}]                                every headline once (title, source, url, date)
  countries   {ISO3: {i: [item index], n, tension}}          newest headlines + a conflict-term share
  pairs       {"AAA|BBB": [item index]}                      headlines naming both countries
  alerts      [{c: [ISO3...], kw, t, s, u, d}]               "headlines to watch", kept for 7 days
Runs at 00:00, 07:00, 12:00 and 18:00 Berlin time (plus manual runs).
"""
from __future__ import annotations

import datetime as dt
import email.utils
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

from common import (NAMES, QUERIES, countries_in, http_get, in_slot, iso_z, load_json, now_utc,
                    ran_recently, save_json, scheduled)

KEEP_PER_COUNTRY = 6
KEEP_PER_PAIR = 2
MAX_PAIRS = 400
ALERT_DAYS = 7
MAX_ALERTS = 80

TENSION = re.compile(r"\b(war|wars|attack\w*|strike|strikes|airstrike\w*|missile\w*|drone\w*|killed|kills|dead|clash\w*|troops|"
                     r"invasion|invade\w*|sanction\w*|coup|bomb\w*|shelling|militant\w*|terror\w*|hostage\w*|ceasefire|"
                     r"truce|blockade|insurg\w*|rebel\w*|fighting|offensive|massacre|executed|crackdown)\b", re.I)
ALERT = re.compile(r"\b(declares? war|war with|invad\w*|invasion|airstrikes?|missile strikes?|strikes? on|attacks? on|"
                   r"coup|sanctions?|expels?\w* (?:the )?(?:\w+ )?(?:ambassador|diplomats?)|recalls? (?:its )?ambassador|"
                   r"(?:cuts?|severs?|restores?|resumes?) (?:diplomatic )?(?:ties|relations)|ceasefire|truce|peace (?:deal|agreement|talks)|"
                   r"(?:defen[cs]e|security|mutual) (?:pact|treaty|agreement)|alliance|annex\w*|blockade|border (?:clash\w*|dispute)|"
                   r"recogni[sz]\w* (?:the )?(?:state|independence)|troops? (?:to|into|deploy\w*))\b", re.I)
POLITICS = re.compile(r"\b(coup|wins? (?:the )?(?:presidential |general |parliamentary |snap )?election|election results?|"
                      r"sworn in as (?:prime minister|president)|(?:prime minister|president|chancellor|government) (?:resigns|ousted|falls|collapses)|"
                      r"resigns as (?:prime minister|president)|new (?:prime minister|president|government)|snap election|"
                      r"no-confidence|impeach\w*)\b", re.I)
# keep results about politics, security and the economy rather than sport or culture
TOPICS = ("(government OR minister OR president OR military OR army OR election OR sanctions OR war OR border "
          "OR talks OR economy OR protest OR diplomat OR attack OR treaty)")


def query_for(iso: str, name: str) -> str:
    return f"({QUERIES.get(iso) or chr(34) + name + chr(34)}) {TOPICS}"


def fetch(iso: str, name: str) -> list[dict]:
    q = urllib.parse.quote(f"{query_for(iso, name)} when:2d")
    root = ET.fromstring(http_get(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"))
    out = []
    for it in root.findall("./channel/item"):
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3].strip()
        try:
            when = iso_z(email.utils.parsedate_to_datetime(it.findtext("pubDate") or ""))
        except (TypeError, ValueError):
            continue
        link = (it.findtext("link") or "").strip()
        if title and link.startswith("https://"):
            out.append({"t": title[:220], "s": src[:60], "u": link, "d": when})
    out.sort(key=lambda x: x["d"], reverse=True)
    return out


def main() -> None:
    t0 = now_utc()
    prev = load_json("news.json", {})
    if scheduled() and (not in_slot(t0) or ran_recently(prev.get("generated"), t0)):
        print("Not a refresh slot (or already refreshed); skipping.")
        return

    # every headline is stored once in `items`; countries and pairs point at it by index
    pool: list = []
    index: dict = {}
    prev_pool = prev.get("items") or []

    def ref(x: dict) -> int:
        if x["u"] not in index:
            index[x["u"]] = len(pool)
            pool.append(x)
        return index[x["u"]]

    countries: dict = {}
    pair_cand: dict = {}
    alerts_new: list = []
    failed = 0
    for iso, name in NAMES.items():
        try:
            items = fetch(iso, name)
        except Exception as e:  # keep the previous headlines for this country
            failed += 1
            print(f"{iso}: failed ({e})")
            old = (prev.get("countries") or {}).get(iso)
            if old:
                countries[iso] = {**old, "i": [ref(prev_pool[j]) for j in old.get("i", []) if j < len(prev_pool)]}
            time.sleep(2)
            continue
        hits = sum(1 for x in items if TENSION.search(x["t"]))
        countries[iso] = {"i": [ref(x) for x in items[:KEEP_PER_COUNTRY]], "n": len(items),
                          "tension": round(100 * hits / len(items)) if items else None}
        for x in items:
            named = countries_in(x["t"])[:4]          # countries explicitly named in the headline
            for i, a in enumerate(named):
                for b in named[i + 1:]:
                    k = "|".join(sorted((a, b)))
                    cand = pair_cand.setdefault(k, {"n": 0, "x": []})
                    if all(y["u"] != x["u"] for y in cand["x"]):
                        cand["n"] += 1
                        if len(cand["x"]) < KEEP_PER_PAIR:
                            cand["x"].append(x)
            m = ALERT.search(x["t"]) if len(named) >= 2 else None
            p = POLITICS.search(x["t"]) if named else None
            if m or p:
                alerts_new.append({"c": named[:3] if m else named[:1], "kw": (m or p).group(0).lower(), **x})
        time.sleep(0.4)
    top = sorted(pair_cand.items(), key=lambda kv: -kv[1]["n"])[:MAX_PAIRS]
    pairs = {k: [ref(x) for x in v["x"]] for k, v in top}
    print(f"Fetched {len(NAMES) - failed}/{len(NAMES)} countries; {len(pair_cand)} pairs seen, {len(pairs)} kept; "
          f"{len(alerts_new)} new alerts.")
    if failed > len(NAMES) * 0.5:
        sys.exit("Google News failed for most countries; keeping the previous news.json.")

    cutoff = iso_z(t0 - dt.timedelta(days=ALERT_DAYS))
    seen, alerts = set(), []
    for a in sorted(alerts_new + (prev.get("alerts") or []), key=lambda a: a["d"], reverse=True):
        key = a["u"] if a["u"] not in seen else None
        title_key = re.sub(r"\W+", " ", a["t"].lower())[:80]
        if key is None or title_key in seen or a["d"] < cutoff:
            continue
        seen.update((a["u"], title_key))
        alerts.append(a)
    save_json("news.json", {"generated": iso_z(t0), "items": pool, "countries": countries, "pairs": pairs,
                            "alerts": alerts[:MAX_ALERTS]}, compact=True)
    print(f"Wrote news.json with {len(alerts[:MAX_ALERTS])} alerts.")


if __name__ == "__main__":
    main()
