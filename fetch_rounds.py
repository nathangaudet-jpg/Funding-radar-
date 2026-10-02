"""Funding radar: weekly sweep of funding rounds from free news feeds.

Runs every Monday on GitHub Actions (see .github/workflows/monday.yml).
Uses only the Python standard library, so nothing needs installing.
Results are written to data/rounds.json and data/sweeps.json.
"""
import hashlib, html, json, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

FUND_VERBS = r"(raises|raised|secures|secured|closes|closed|lands|bags|nabs|snags|gets|picks up|scores|collects|attracts|announces|completes)"
VERB_RE = re.compile(r"\b" + FUND_VERBS + r"\b", re.I)
FUNDING_HINT = re.compile(r"\b(funding|round|series [a-h]|seed|investment|raises|raised|secures|valuation)\b", re.I)
ACQ_RE = re.compile(r"\b(acquires|acquired|to acquire|acquisition of|buys|invests in|takes stake|leads investment)\b", re.I)
AMOUNT_RE = re.compile(
    r"(?P<cur>US\$|\$|€|£|USD\s?|EUR\s?|GBP\s?)?\s?(?P<num>\d{1,4}(?:[.,]\d{1,3})?)\s?"
    r"(?P<unit>billion|bn|million|mln|mn|m|b)\b\.?\s?(?P<cur2>dollars?|USD|euros?|EUR|pounds?|GBP)?", re.I)
STAGE_RE = re.compile(r"\b(pre-seed|seed|series [a-h]\+?|growth round|growth|debt|IPO)\b", re.I)
WIRES = ("businesswire", "prnewswire", "globenewswire", "newswire", "presseportal", "accesswire")

PLACES = [  # (pattern, country, region)
    (r"\b(UK|U\.K\.|British|London|Manchester|Edinburgh|Cambridge, UK)\b", "UK", "Europe"),
    (r"\b(German|Germany|Berlin|Munich|Hamburg|Cologne|Frankfurt)\b", "Germany", "Europe"),
    (r"\b(French|France|Paris|Lyon)\b", "France", "Europe"),
    (r"\b(Dutch|Netherlands|Amsterdam|Rotterdam)\b", "Netherlands", "Europe"),
    (r"\b(Swedish|Sweden|Stockholm)\b", "Sweden", "Europe"),
    (r"\b(Danish|Denmark|Copenhagen)\b", "Denmark", "Europe"),
    (r"\b(Finnish|Finland|Helsinki)\b", "Finland", "Europe"),
    (r"\b(Norwegian|Norway|Oslo)\b", "Norway", "Europe"),
    (r"\b(Spanish|Spain|Madrid|Barcelona)\b", "Spain", "Europe"),
    (r"\b(Italian|Italy|Milan|Rome)\b", "Italy", "Europe"),
    (r"\b(Swiss|Switzerland|Zurich|Geneva|Lausanne)\b", "Switzerland", "Europe"),
    (r"\b(Irish|Ireland|Dublin)\b", "Ireland", "Europe"),
    (r"\b(Polish|Poland|Warsaw|Krakow)\b", "Poland", "Europe"),
    (r"\b(Estonian|Estonia|Tallinn)\b", "Estonia", "Europe"),
    (r"\b(Portuguese|Portugal|Lisbon|Porto)\b", "Portugal", "Europe"),
    (r"\b(Belgian|Belgium|Brussels|Antwerp)\b", "Belgium", "Europe"),
    (r"\b(Austrian|Austria|Vienna)\b", "Austria", "Europe"),
    (r"\b(European|Europe's|Europe-based)\b", "", "Europe"),
    (r"\b(Chinese|China|Beijing|Shanghai|Shenzhen|Hangzhou)\b", "China", "China"),
    (r"\b(US|U\.S\.|US-based|American|San Francisco|New York|NYC|Silicon Valley|Boston|Seattle|Austin|Palo Alto|Los Angeles)\b", "US", "US"),
]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (funding-radar)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def gnews_url(query, edition):
    hl, gl = {"US": ("en-US", "US"), "GB": ("en-GB", "GB")}[edition]
    q = urllib.parse.quote(query + " when:%dd" % CONFIG["window_days"])
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:en"


def parse_feed(raw, feed_name):
    items = []
    root = ET.fromstring(raw)
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        src_el = it.find("source")
        source = src_el.text.strip() if src_el is not None and src_el.text else feed_name
        source_url = src_el.get("url", "") if src_el is not None else ""
        # Google News appends " - Source" to titles
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        pub = it.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
        except Exception:
            published = None
        items.append({"title": title, "summary": re.sub(r"\s+", " ", desc).strip(),
                      "link": (it.findtext("link") or "").strip(), "source": source,
                      "source_url": source_url, "published": published})
    return items


def parse_amount(text):
    """Return (amount in $M, label) for the first money amount in text."""
    for m in AMOUNT_RE.finditer(text):
        cur = (m.group("cur") or m.group("cur2") or "").strip().lower()
        if not cur:
            continue  # bare numbers like "5 million users" are not money
        num = float(m.group("num").replace(",", "."))
        unit = m.group("unit").lower()
        musd = num * 1000 if unit in ("billion", "bn", "b") else num
        if cur in ("€", "eur") or cur.startswith("euro"):
            musd *= CONFIG["eur_to_usd"]
        elif cur in ("£", "gbp") or cur.startswith("pound"):
            musd *= CONFIG["gbp_to_usd"]
        return round(musd, 1), m.group(0).strip()
    return None, None


def parse_company(title):
    m = VERB_RE.search(title)
    if not m:
        return None, ""
    before = re.sub(r"^(exclusive|breaking|update|report)\s*[:\-–]\s*", "", title[: m.start()].strip(), flags=re.I)
    words = before.replace("’", "'").split()
    name = []
    for w in reversed(words):
        if w[:1].isupper() or w[:1].isdigit():
            name.insert(0, w)
        else:
            break
    company = " ".join(name).strip(" ,:;'\"")
    company = re.sub(r"'s$", "", company)
    descriptor = " ".join(words[: len(words) - len(name)]).strip(" ,")
    return (company or None), descriptor


def detect_place(text):
    for pat, country, region in PLACES:
        if re.search(pat, text):
            return country, region
    return "", ""


def match_fields(text):
    low = text.lower()
    return [f["name"] for f in CONFIG["fields"] if any(k.lower() in low for k in f["keywords"])]


def big_actor_in(text):
    for a in CONFIG["big_actors"]:
        if re.search(r"\b" + re.escape(a) + r"\b", text):
            return a
    return None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def is_first_party(item, company):
    dom = urllib.parse.urlparse(item.get("source_url") or item.get("link") or "").netloc.lower()
    return any(w in dom for w in WIRES) or (bool(company) and norm(company)[:6] in norm(item["source"] + dom))


def classify(item, field_hint):
    text = item["title"] + " " + item["summary"]
    fields = [field_hint] if field_hint else match_fields(text)
    if not fields:
        return None
    musd, label = parse_amount(item["title"])
    if musd is None:
        musd, label = parse_amount(item["summary"])
    actor = big_actor_in(item["title"])
    if actor and ACQ_RE.search(item["title"]):
        kind = "big"
    elif VERB_RE.search(item["title"]) and FUNDING_HINT.search(text) and musd:
        kind = "startup"
    else:
        return None
    company, descriptor = parse_company(item["title"])
    if kind == "big":
        company = actor
    if not company:
        return None
    country, region = detect_place(text)
    if not region and label and ("€" in label or "eur" in label.lower() or "£" in label or "gbp" in label.lower()):
        region = "Europe"
    region = region or "Unknown"
    if kind == "startup":
        if musd < CONFIG["thresholds_musd"].get(region, 10):
            return None
    stage = STAGE_RE.search(text)
    return {
        "company": company, "kind": kind, "field": fields[0],
        "headline": item["title"], "what": descriptor,
        "amount_usd_m": musd, "amount_label": label or "",
        "stage": stage.group(0).title() if stage else "",
        "country": country, "region": region,
        "date": item["published"].date().isoformat() if item["published"] else "",
        "source_name": item["source"], "source_url": item["link"],
        "first_party": is_first_party(item, company),
    }


def queries_for(field):
    kw = " OR ".join(f'"{k}"' for k in field["keywords"][:6])
    return [f"({kw}) (raises OR funding OR \"Series A\" OR \"Series B\" OR \"Series C\" OR acquires)"]


def main():
    DATA.mkdir(exist_ok=True)
    rounds_path, sweeps_path = DATA / "rounds.json", DATA / "sweeps.json"
    rounds = json.loads(rounds_path.read_text(encoding="utf-8")) if rounds_path.exists() else []
    sweeps = json.loads(sweeps_path.read_text(encoding="utf-8")) if sweeps_path.exists() else []

    today = date.today()
    week = (today - timedelta(days=today.weekday())).isoformat()
    since = datetime.now(timezone.utc) - timedelta(days=CONFIG["window_days"] + 1)

    jobs = []
    for f in CONFIG["fields"]:
        for q in queries_for(f):
            for ed in ("US", "GB"):
                jobs.append((f["name"], "Google News", gnews_url(q, ed)))
    for feed in CONFIG.get("extra_feeds", []):
        jobs.append((None, feed["name"], feed["url"]))

    found, errors = {}, []
    for field_hint, name, url in jobs:
        try:
            items = parse_feed(fetch(url), name)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        for it in items:
            if it["published"] and it["published"] < since:
                continue
            r = classify(it, field_hint)
            if not r:
                continue
            key = norm(r["company"]) + ("-" + r["kind"])
            prev = found.get(key)
            if prev is None or (r["first_party"] and not prev["first_party"]):
                found[key] = r
        time.sleep(1)  # be polite to the feeds

    # skip rounds already stored in the last 30 days (same company, same type)
    recent_cut = (today - timedelta(days=30)).isoformat()
    seen = {norm(r["company"]) + "-" + r["kind"] for r in rounds if r.get("week", "") >= recent_cut}
    new = []
    for key, r in found.items():
        if key in seen:
            continue
        r["week"] = week
        r["id"] = hashlib.sha1((key + week).encode()).hexdigest()[:12]
        new.append(r)

    rounds.extend(sorted(new, key=lambda r: -(r["amount_usd_m"] or 0)))
    sweeps = [s for s in sweeps if s["week"] != week] + [{
        "week": week, "ran_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "count": len(new) + sum(1 for r in rounds if r.get("week") == week and r not in new),
        "feeds_checked": len(jobs), "feed_errors": errors[:10]}]
    rounds_path.write_text(json.dumps(rounds, indent=1, ensure_ascii=False), encoding="utf-8")
    sweeps_path.write_text(json.dumps(sweeps, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Week {week}: {len(new)} new rounds, {len(errors)} feed errors")
    for e in errors:
        print("  ", e)


if __name__ == "__main__":
    sys.exit(main())
