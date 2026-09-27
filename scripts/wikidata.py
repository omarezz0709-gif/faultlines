"""Current heads of state/government, their current party and system of government, from Wikidata (free).

Writes data/politics.json: {generated, countries: {ISO3: {system, hos, hog, ruling, source}}}.
Runs once a day in the 00:00 Berlin slot (or on a manual run).
One light query to the Wikidata query service (IDs only), then names and parties
through the regular Wikidata API, which is far less rate-limited.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse

from common import NAMES, http_get, in_slot, iso_z, load_json, now_utc, ran_recently, save_json, scheduled

SPARQL = """SELECT ?c ?iso ?hos ?hog ?form WHERE {
  { ?c wdt:P298 ?iso . }
  UNION { VALUES (?c ?iso) { (wd:Q1246 "XKX") (wd:Q23681 "NCY") (wd:Q34754 "SOL") } }
  OPTIONAL { ?c wdt:P35 ?hos . }
  OPTIONAL { ?c wdt:P6 ?hog . }
  OPTIONAL { ?c wdt:P122 ?form . }
}"""
API = "https://www.wikidata.org/w/api.php?format=json&action=wbgetentities&"


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


class QueryServiceDown(Exception):
    pass


def query() -> list[dict]:
    url = "https://query.wikidata.org/sparql?query=" + urllib.parse.quote(SPARQL)
    for attempt in range(2):
        try:
            return json.loads(http_get(url, timeout=120, accept="application/sparql-results+json"))["results"]["bindings"]
        except Exception as e:  # the query service rate-limits and has outages; back off and retry once
            print(f"query attempt {attempt + 1} failed: {e}")
            if attempt == 0:
                time.sleep(70)
    raise QueryServiceDown()


def truthy(claims: list) -> list[str]:
    """The item ids of a property's 'truthy' statements, like wdt: in the query service: preferred rank if any, else normal."""
    live = [c for c in claims if c.get("rank") != "deprecated"]
    best = [c for c in live if c.get("rank") == "preferred"] or live
    out = []
    for c in best:
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
        if isinstance(v, dict) and v.get("id") and v["id"] not in out:
            out.append(v["id"])
    return out


def api_get(url: str, deadline: float) -> dict:
    """One regular-API request, gently: on "too many requests" it waits as long as Wikidata asks."""
    for attempt in range(4):
        if time.time() > deadline:
            raise TimeoutError("time budget used up")
        try:
            return json.loads(http_get(url, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(int(e.headers.get("Retry-After") or 30), 120)
                print(f"  Wikidata says slow down; waiting {wait} s")
                time.sleep(wait)
                continue
            raise
        except Exception:
            time.sleep(3)
    raise RuntimeError("gave up after retries")


def by_country_api(prev: dict) -> dict[str, dict]:
    """Fallback when the query service is down: the same heads of state/government through the regular Wikidata API,
    one small request per country and property, about one a second, for at most 10 minutes (the country ids come from
    the previous politics.json). Countries it doesn't reach keep their previous entry."""
    by: dict[str, dict] = {}
    deadline = time.time() + 600
    for iso, doc in (prev.get("countries") or {}).items():
        q = qid(doc.get("source", ""))
        if iso not in NAMES or not q.startswith("Q"):
            continue
        e = {"qid": q, "hos": [], "hog": [], "form": []}
        try:
            for prop, field in (("P35", "hos"), ("P6", "hog")):
                url = f"https://www.wikidata.org/w/api.php?format=json&action=wbgetclaims&entity={q}&property={prop}"
                e[field] = truthy(api_get(url, deadline).get("claims", {}).get(prop, []))
                time.sleep(1)
        except TimeoutError:
            print("fallback: 10 minutes used up; the remaining countries keep their previous entry")
            break
        except Exception as ex:
            print(f"{iso}: {ex}; keeps its previous entry")
            continue
        by[iso] = e
    print(f"fallback API: {len(by)} countries refreshed")
    return by


def entities(ids: list[str], props: str) -> dict:
    out: dict = {}
    for i in range(0, len(ids), 50):
        url = API + f"props={props}&languages=en|mul|fr|de|es&sitefilter=enwiki&ids=" + "|".join(ids[i:i + 50])
        for attempt in range(3):
            try:
                out.update(json.loads(http_get(url, timeout=60)).get("entities", {}))
                break
            except Exception as e:
                print(f"entity lookup failed ({e}); retrying")
                time.sleep(5)
    return out


def name_of(ent: dict) -> str | None:
    labels = ent.get("labels") or {}
    for lang in ("en", "mul"):
        if lang in labels:
            return labels[lang]["value"]
    site = (ent.get("sitelinks") or {}).get("enwiki")
    if site:
        return site["title"]
    for lab in labels.values():
        return lab["value"]
    return None


def current_parties(ent: dict) -> list[str]:
    """P102 (member of political party) claims without an end date; preferred rank first."""
    claims = (ent.get("claims") or {}).get("P102", [])
    live = [c for c in claims if c.get("rank") != "deprecated" and "P582" not in (c.get("qualifiers") or {})]
    live.sort(key=lambda c: c.get("rank") != "preferred")
    out = []
    for c in live:
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
        if v.get("id") and v["id"] not in out:
            out.append(v["id"])
    return out


def main() -> None:
    t0 = now_utc()
    prev = load_json("politics.json", {})
    if scheduled() and (not in_slot(t0, hours=(0,)) or ran_recently(prev.get("generated"), t0, hours=20)):
        print("Wikidata refresh runs once a day in the midnight slot; skipping.")
        return

    by: dict[str, dict] = {}
    try:
        for b in query():
            iso = b["iso"]["value"]
            if iso not in NAMES:
                continue
            e = by.setdefault(iso, {"qid": qid(b["c"]["value"]), "hos": [], "hog": [], "form": []})
            for f in ("hos", "hog", "form"):
                if f in b:
                    v = qid(b[f]["value"])
                    if v not in e[f]:
                        e[f].append(v)
    except QueryServiceDown:
        # the query service has outages (then it throttles everyone to 1 request a minute): use the regular API instead
        print("Wikidata query service unavailable; using the regular Wikidata API instead.")
        by = by_country_api(prev)
        fallback = True
    else:
        fallback = False

    hogs = sorted({q for e in by.values() for q in e["hog"]})
    hog_ents = entities(hogs, "labels|sitelinks|claims")
    parties = {q: current_parties(hog_ents.get(q, {})) for q in hogs}
    rest = sorted({q for e in by.values() for f in ("hos", "form") for q in e[f]} |
                  {p for ps in parties.values() for p in ps})
    names = {q: name_of(ent) for q, ent in {**entities(rest, "labels|sitelinks"), **hog_ents}.items()}
    label = lambda ids: [names[q] for q in ids if names.get(q)]

    out: dict[str, dict] = {}
    for iso, e in by.items():
        hos, hog, form = label(e["hos"]), label(e["hog"]), label(e["form"])
        party = label([p for h in e["hog"] for p in parties.get(h, [])])
        party = [p for p in party if p.lower() != "independent politician"] or (["Independent"] if party else [])
        doc = {}
        if form: doc["system"] = ", ".join(dict.fromkeys(form[:2]))
        elif (prev.get("countries") or {}).get(iso, {}).get("system"):   # fallback run: the system of government rarely changes
            doc["system"] = prev["countries"][iso]["system"]
        if hos: doc["hos"] = " / ".join(dict.fromkeys(hos[:2]))
        if hog: doc["hog"] = " / ".join(dict.fromkeys(hog[:2]))
        if party: doc["ruling"] = " / ".join(dict.fromkeys(party[:2])) + " (party of the head of government)"
        if doc:
            doc["source"] = f"https://www.wikidata.org/wiki/{e['qid']}"
            out[iso] = doc
    if fallback:   # countries the fallback didn't reach keep yesterday's entry
        for iso, doc in (prev.get("countries") or {}).items():
            out.setdefault(iso, doc)
    print(f"{len(out)} countries with politics data.")
    if len(out) < 120:
        sys.exit("Too few countries returned; keeping the previous politics.json.")
    save_json("politics.json", {"generated": iso_z(t0), "countries": out})


if __name__ == "__main__":
    main()
