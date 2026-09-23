"""Refresh data/wb.json with the latest World Bank figures (free API, no key).

Runs once a day in the 00:00 Berlin slot (or on a manual run). Keeps the old
file if the API is down, so a failed fetch never blanks the page's numbers.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from refresh import BERLIN, SLOT_WINDOW_MIN  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "wb.json")

INDICATORS = {
    "gdp": "NY.GDP.MKTP.CD", "gdppc": "NY.GDP.PCAP.CD", "gnippp": "NY.GNP.PCAP.PP.CD",
    "infl": "FP.CPI.TOTL.ZG", "unemp": "SL.UEM.TOTL.ZS", "fert": "SP.DYN.TFRT.IN",
    "cbr": "SP.DYN.CBRT.IN", "life": "SP.DYN.LE00.IN", "gini": "SI.POV.GINI",
    "spcov": "per_allsp.cov_pop_tot", "milgdp": "MS.MIL.XPND.GD.ZS", "milusd": "MS.MIL.XPND.CD",
    "milpers": "MS.MIL.TOTL.P1", "forest": "AG.LND.FRST.ZS", "protect": "ER.PTD.TOTL.ZS",
    "co2pc": "EN.GHG.CO2.PC.CE.AR5", "renew": "EG.FEC.RNEW.ZS",
}


def due() -> bool:
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return True
    now = dt.datetime.now(BERLIN)
    return now.hour == 0 or (now.hour == 1 and now.minute < SLOT_WINDOW_MIN - 60)


def fetch(code: str) -> list:
    url = f"https://api.worldbank.org/v2/country/all/indicator/{code}?format=json&mrnev=1&per_page=500"
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    return data[1] if isinstance(data, list) and len(data) > 1 and data[1] else []


def main() -> None:
    if not due():
        print("World Bank refresh runs in the midnight slot only; skipping.")
        return
    out: dict = {}
    ok = 0
    for key, code in INDICATORS.items():
        try:
            rows = fetch(code)
        except Exception as e:  # network or API hiccup: keep going, judge at the end
            print(f"{key} ({code}): failed: {e}")
            continue
        n = 0
        for row in rows:
            iso, v = row.get("countryiso3code"), row.get("value")
            if not iso or v is None:
                continue
            v = float(v)
            out.setdefault(iso, {})[key] = [round(v) if abs(v) >= 1000 else round(v, 2), int(row["date"])]
            n += 1
        print(f"{key} ({code}): {n}")
        ok += n > 0
    if ok < len(INDICATORS) * 0.8:
        raise SystemExit(f"Only {ok}/{len(INDICATORS)} indicators fetched; keeping the previous wb.json.")
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, separators=(",", ":"), sort_keys=True)
    print(f"Wrote {len(out)} countries.")


if __name__ == "__main__":
    main()
