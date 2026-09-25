"""Shared helpers for the free data updaters (no API keys needed)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.request
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
BERLIN = ZoneInfo("Europe/Berlin")
SLOTS = (0, 7, 12, 18)          # Berlin hours
SLOT_WINDOW_MIN = 90         # GitHub's scheduler can start runs late
UA = "faultlines-globe/1.0 (free news globe; GitHub Actions data refresh)"

# ISO3 -> display name (matches the page)
NAMES = {"AFG":"Afghanistan","AGO":"Angola","ALB":"Albania","ARE":"United Arab Emirates","ARG":"Argentina","ARM":"Armenia","AUS":"Australia","AUT":"Austria","AZE":"Azerbaijan","BDI":"Burundi","BEL":"Belgium","BEN":"Benin","BFA":"Burkina Faso","BGD":"Bangladesh","BGR":"Bulgaria","BHS":"Bahamas","BIH":"Bosnia and Herzegovina","BLR":"Belarus","BLZ":"Belize","BOL":"Bolivia","BRA":"Brazil","BRN":"Brunei","BTN":"Bhutan","BWA":"Botswana","CAF":"Central African Republic","CAN":"Canada","CHE":"Switzerland","CHL":"Chile","CHN":"China","CIV":"Côte d'Ivoire","CMR":"Cameroon","COD":"DR Congo","COG":"Republic of Congo","COL":"Colombia","CRI":"Costa Rica","CUB":"Cuba","NCY":"Northern Cyprus","CYP":"Cyprus","CZE":"Czechia","DEU":"Germany","DJI":"Djibouti","DNK":"Denmark","DOM":"Dominican Republic","DZA":"Algeria","ECU":"Ecuador","EGY":"Egypt","ERI":"Eritrea","ESP":"Spain","EST":"Estonia","ETH":"Ethiopia","FIN":"Finland","FJI":"Fiji","FRA":"France","GAB":"Gabon","GBR":"United Kingdom","GEO":"Georgia","GHA":"Ghana","GIN":"Guinea","GMB":"Gambia","GNB":"Guinea-Bissau","GNQ":"Equatorial Guinea","GRC":"Greece","GTM":"Guatemala","GUY":"Guyana","HND":"Honduras","HRV":"Croatia","HTI":"Haiti","HUN":"Hungary","IDN":"Indonesia","IND":"India","IRL":"Ireland","IRN":"Iran","IRQ":"Iraq","ISL":"Iceland","ISR":"Israel","ITA":"Italy","JAM":"Jamaica","JOR":"Jordan","JPN":"Japan","KAZ":"Kazakhstan","KEN":"Kenya","KGZ":"Kyrgyzstan","KHM":"Cambodia","KOR":"South Korea","XKX":"Kosovo","KWT":"Kuwait","LAO":"Laos","LBN":"Lebanon","LBR":"Liberia","LBY":"Libya","LKA":"Sri Lanka","LSO":"Lesotho","LTU":"Lithuania","LUX":"Luxembourg","LVA":"Latvia","MAR":"Morocco","MDA":"Moldova","MDG":"Madagascar","MEX":"Mexico","MKD":"North Macedonia","MLI":"Mali","MMR":"Myanmar","MNE":"Montenegro","MNG":"Mongolia","MOZ":"Mozambique","MRT":"Mauritania","MWI":"Malawi","MYS":"Malaysia","NAM":"Namibia","NER":"Niger","NGA":"Nigeria","NIC":"Nicaragua","NLD":"Netherlands","NOR":"Norway","NPL":"Nepal","NZL":"New Zealand","OMN":"Oman","PAK":"Pakistan","PAN":"Panama","PER":"Peru","PHL":"Philippines","PNG":"Papua New Guinea","POL":"Poland","PRK":"North Korea","PRT":"Portugal","PRY":"Paraguay","QAT":"Qatar","ROU":"Romania","RUS":"Russia","RWA":"Rwanda","ESH":"Western Sahara","SAU":"Saudi Arabia","SDN":"Sudan","SSD":"South Sudan","SEN":"Senegal","SLB":"Solomon Islands","SLE":"Sierra Leone","SLV":"El Salvador","SOL":"Somaliland","SOM":"Somalia","SRB":"Serbia","SUR":"Suriname","SVK":"Slovakia","SVN":"Slovenia","SWE":"Sweden","SWZ":"Eswatini","SYR":"Syria","TCD":"Chad","TGO":"Togo","THA":"Thailand","TJK":"Tajikistan","TKM":"Turkmenistan","TLS":"Timor-Leste","TTO":"Trinidad and Tobago","TUN":"Tunisia","TUR":"Türkiye","TWN":"Taiwan","TZA":"Tanzania","UGA":"Uganda","UKR":"Ukraine","URY":"Uruguay","USA":"United States","UZB":"Uzbekistan","VEN":"Venezuela","VNM":"Vietnam","VUT":"Vanuatu","PSE":"Palestine","YEM":"Yemen","ZAF":"South Africa","ZMB":"Zambia","ZWE":"Zimbabwe","SGP":"Singapore","BHR":"Bahrain","MLT":"Malta","MDV":"Maldives","MUS":"Mauritius","SYC":"Seychelles","CPV":"Cabo Verde","COM":"Comoros","STP":"São Tomé and Príncipe","BRB":"Barbados","ATG":"Antigua and Barbuda","DMA":"Dominica","GRD":"Grenada","KNA":"Saint Kitts and Nevis","LCA":"Saint Lucia","VCT":"Saint Vincent and the Grenadines","AND":"Andorra","MCO":"Monaco","LIE":"Liechtenstein","SMR":"San Marino","VAT":"Vatican City","WSM":"Samoa","TON":"Tonga","KIR":"Kiribati","FSM":"Micronesia","PLW":"Palau","MHL":"Marshall Islands","NRU":"Nauru","TUV":"Tuvalu"}

# Extra words that identify a country in a headline (demonyms, capitals, key actors).
# Plain "Georgia" is left out on purpose (usually the US state).
ALIASES = {
  "USA": ["United States", "U.S.", "US", "America", "American", "Americans", "Washington", "Pentagon", "White House"],
  "GBR": ["Britain", "British", "UK", "U.K.", "United Kingdom", "Downing Street"],
  "RUS": ["Russia", "Russian", "Russians", "Moscow", "Kremlin"],
  "CHN": ["China", "Chinese", "Beijing"], "TWN": ["Taiwan", "Taiwanese", "Taipei"],
  "UKR": ["Ukraine", "Ukrainian", "Ukrainians", "Kyiv"], "BLR": ["Belarus", "Belarusian", "Minsk"],
  "ISR": ["Israel", "Israeli", "Israelis", "IDF"], "PSE": ["Palestine", "Palestinian", "Palestinians", "Gaza", "West Bank", "Hamas"],
  "IRN": ["Iran", "Iranian", "Tehran"], "IRQ": ["Iraq", "Iraqi", "Baghdad"], "SYR": ["Syria", "Syrian", "Damascus"],
  "LBN": ["Lebanon", "Lebanese", "Beirut", "Hezbollah"], "YEM": ["Yemen", "Yemeni", "Houthi", "Houthis", "Sanaa"],
  "SAU": ["Saudi Arabia", "Saudi", "Riyadh"], "ARE": ["UAE", "United Arab Emirates", "Emirati", "Abu Dhabi", "Dubai"],
  "QAT": ["Qatar", "Qatari", "Doha"], "TUR": ["Türkiye", "Turkey", "Turkish", "Ankara", "Erdogan"], "EGY": ["Egypt", "Egyptian", "Cairo"],
  "JOR": ["Jordan", "Jordanian", "Amman"], "AFG": ["Afghanistan", "Afghan", "Kabul", "Taliban"],
  "PAK": ["Pakistan", "Pakistani", "Islamabad"], "IND": ["India", "Indian", "New Delhi"],
  "JPN": ["Japan", "Japanese", "Tokyo"], "KOR": ["South Korea", "South Korean", "Seoul"], "PRK": ["North Korea", "North Korean", "Pyongyang"],
  "DEU": ["Germany", "German", "Berlin"], "FRA": ["France", "French", "Paris"], "ITA": ["Italy", "Italian", "Rome"],
  "ESP": ["Spain", "Spanish", "Madrid"], "POL": ["Poland", "Polish", "Warsaw"], "GRC": ["Greece", "Greek", "Athens"],
  "SDN": ["Sudan", "Sudanese", "Khartoum", "RSF"], "SSD": ["South Sudan", "South Sudanese", "Juba"],
  "COD": ["DR Congo", "DRC", "Democratic Republic of Congo", "Democratic Republic of the Congo", "Kinshasa", "Goma", "M23"],
  "COG": ["Republic of Congo", "Congo-Brazzaville", "Brazzaville"], "RWA": ["Rwanda", "Rwandan", "Kigali"],
  "MLI": ["Mali", "Malian", "Bamako"], "BFA": ["Burkina Faso", "Burkinabe", "Ouagadougou"], "NER": ["Niger", "Nigerien", "Niamey"],
  "NGA": ["Nigeria", "Nigerian", "Abuja"], "ETH": ["Ethiopia", "Ethiopian", "Addis Ababa"], "ERI": ["Eritrea", "Eritrean", "Asmara"],
  "SOM": ["Somalia", "Somali", "Mogadishu", "al-Shabaab"], "LBY": ["Libya", "Libyan", "Tripoli", "Benghazi"],
  "DZA": ["Algeria", "Algerian", "Algiers"], "MAR": ["Morocco", "Moroccan", "Rabat"], "TCD": ["Chad", "Chadian", "N'Djamena"],
  "VEN": ["Venezuela", "Venezuelan", "Caracas"], "CUB": ["Cuba", "Cuban", "Havana"], "MEX": ["Mexico", "Mexican"],
  "CAN": ["Canada", "Canadian", "Ottawa"], "BRA": ["Brazil", "Brazilian", "Brasilia"], "ARG": ["Argentina", "Argentine", "Buenos Aires"],
  "COL": ["Colombia", "Colombian", "Bogota"], "AUS": ["Australia", "Australian", "Canberra"], "ARM": ["Armenia", "Armenian", "Yerevan"],
  "AZE": ["Azerbaijan", "Azerbaijani", "Baku"], "GEO": ["Georgian", "Tbilisi"], "SRB": ["Serbia", "Serbian", "Belgrade"],
  "XKX": ["Kosovo", "Kosovar", "Pristina"], "HUN": ["Hungary", "Hungarian", "Budapest", "Orban"], "MDA": ["Moldova", "Moldovan", "Chisinau", "Transnistria"],
  "PHL": ["Philippines", "Philippine", "Filipino", "Manila"], "VNM": ["Vietnam", "Vietnamese", "Hanoi"], "MMR": ["Myanmar", "Burmese"],
  "THA": ["Thailand", "Thai", "Bangkok"], "KHM": ["Cambodia", "Cambodian", "Phnom Penh"], "IDN": ["Indonesia", "Indonesian", "Jakarta"],
  "BGD": ["Bangladesh", "Bangladeshi", "Dhaka"], "LKA": ["Sri Lanka", "Sri Lankan", "Colombo"], "NLD": ["Netherlands", "Dutch"],
  "SWE": ["Sweden", "Swedish", "Stockholm"], "FIN": ["Finland", "Finnish", "Helsinki"], "NOR": ["Norway", "Norwegian", "Oslo"],
  "DNK": ["Denmark", "Danish", "Copenhagen", "Greenland"], "CYP": ["Cyprus", "Cypriot", "Nicosia"], "NCY": ["Northern Cyprus", "Turkish Cypriot"],
  "ESH": ["Western Sahara", "Polisario", "Sahrawi"], "SOL": ["Somaliland"], "KAZ": ["Kazakhstan", "Kazakh", "Astana"],
  "VAT": ["Vatican City", "Vatican", "Holy See", "Pope"],
}

# Google News search terms where the plain name would be ambiguous
QUERIES = {
  "GEO": '"Georgia" (Tbilisi OR Georgian)', "JOR": 'Jordanian OR "Jordan\'s" OR Amman', "TCD": 'Chadian OR "Chad\'s" OR "N\'Djamena"', "NER": '"Niger" -Nigeria', "GIN": '"Guinea" Conakry',
  "COG": '"Republic of Congo" OR Brazzaville', "COD": '"DR Congo" OR "Democratic Republic of Congo"', "USA": '"United States"',
  "PSE": 'Palestinian OR Gaza OR "West Bank"', "NCY": '"Northern Cyprus" OR "Turkish Cypriot"', "ESH": '"Western Sahara" OR Polisario',
  "VAT": 'Vatican', "DMA": '"Dominica" -"Dominican Republic"', "GBR": '"United Kingdom" OR Britain', "TUR": 'Turkey OR Türkiye',
  "CAF": '"Central African Republic"', "MHL": '"Marshall Islands"', "FSM": 'Micronesia',
}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_z(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def in_slot(t_utc: dt.datetime, hours=SLOTS) -> bool:
    b = t_utc.astimezone(BERLIN)
    for h in hours:
        start = b.replace(hour=h, minute=0, second=0, microsecond=0)
        if start <= b < start + dt.timedelta(minutes=SLOT_WINDOW_MIN):
            return True
    return False


def scheduled() -> bool:
    return os.environ.get("GITHUB_EVENT_NAME") == "schedule"


def ran_recently(generated: str | None, t_utc: dt.datetime, hours: float = 3) -> bool:
    if not generated:
        return False
    try:
        g = dt.datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError:
        return False
    return t_utc - g < dt.timedelta(hours=hours)


def http_get(url: str, timeout: int = 40, accept: str | None = None) -> bytes:
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def load_json(name: str, default):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(name: str, obj, compact: bool = False) -> None:
    with open(os.path.join(DATA, name), "w", encoding="utf-8", newline="\n") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def alias_table() -> list[tuple[str, str]]:
    """(alias, ISO3) pairs, longest first, so 'South Sudan' wins over 'Sudan'."""
    pairs = []
    for iso, name in NAMES.items():
        for w in ALIASES.get(iso, [name]):
            pairs.append((w, iso))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


_TABLE = None


def countries_in(text: str) -> list[str]:
    """ISO3 codes mentioned in a headline, in order of appearance."""
    global _TABLE
    if _TABLE is None:
        _TABLE = [(re.compile(r"(?<![\w.])" + re.escape(a) + r"(?![\w])"), iso) for a, iso in alias_table()]
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for rx, iso in _TABLE:
        for m in rx.finditer(text):
            s, e = m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            taken.append((s, e))
            found.append((s, iso))
    out: list[str] = []
    for _, iso in sorted(found):
        if iso not in out:
            out.append(iso)
    return out
