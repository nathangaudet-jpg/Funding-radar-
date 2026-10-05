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
NOT_FUNDING = re.compile(r"\b(bonds?|issuers|placing|registered direct offering|at-the-market|securiti[sz]ation|debt|loan|credit facility|outlook|guidance|revenue|worries|concerns|price target|dividend|buyback|reverse split|stake sale|stock offering|public offering)\b", re.I)
NOT_FUNDING_CS = re.compile(r"\b(IPO|ATM|EBIT|EBITDA)\b")
ACQ_RE = re.compile(r"\b(acquires|acquired|to acquire|acquisition of|buys|invests in|takes stake|leads investment)\b", re.I)
AMOUNT_RE = re.compile(
    r"(?P<cur>US\$|C\$|CA\$|\$|€|£|USD\s?|EUR\s?|GBP\s?|JPY\s?|CNY\s?|RMB\s?)?\s?(?P<num>\d{1,4}(?:[.,]\d{1,3})?)\s?"
    r"(?P<unit>billion|bn|million|mln|mn|m|b)\b\.?\s?(?P<cur2>dollars?|USD|euros?|EUR|pounds?|GBP|yen|JPY|yuan|CNY|RMB)?", re.I)
STAGE_RE = re.compile(r"\b(pre-seed|seed|series [a-h]\+?|growth round|growth|debt|IPO)\b", re.I)
WIRES = ("businesswire", "prnewswire", "globenewswire", "newswire", "presseportal", "accesswire")

PLACES = [  # (pattern, country, region)
    (r"\b(UK|U\.K\.|British|London|Manchester|Edinburgh)\b", "UK", "Europe"),
    (r"\b(German|Germany|Berlin|Munich|Hamburg|Cologne|Frankfurt)\b", "Germany", "Europe"),
    (r"\b(French|France|Paris|Lyon)\b", "France", "Europe"),
    (r"\b(Dutch|Netherlands|Amsterdam|Rotterdam)\b", "Netherlands", "Europe"),
    (r"\b(Swedish|Sweden|Stockholm)\b", "Sweden", "Europe"),
    (r"\b(Danish|Denmark|Copenhagen)\b", "Denmark", "Europe"),
    (r"\b(Finnish|Finland|Helsinki)\b", "Finland", "Europe"),
    (r"\b(Norwegian|Norway|Oslo)\b", "Norway", "Europe"),
    (r"\b(Spanish|Spain|Madrid|Barcelona)\b", "Spain", "Europe"),
    (r"\b(Italian|Italy|Milan)\b", "Italy", "Europe"),
    (r"\b(Swiss|Switzerland|Zurich|Geneva|Lausanne)\b", "Switzerland", "Europe"),
    (r"\b(Irish|Ireland|Dublin)\b", "Ireland", "Europe"),
    (r"\b(Polish|Poland|Warsaw|Krakow)\b", "Poland", "Europe"),
    (r"\b(Estonian|Estonia|Tallinn)\b", "Estonia", "Europe"),
    (r"\b(Lithuanian|Lithuania|Vilnius|Latvian|Latvia|Riga)\b", "Baltics", "Europe"),
    (r"\b(Portuguese|Portugal|Lisbon|Porto)\b", "Portugal", "Europe"),
    (r"\b(Belgian|Belgium|Brussels|Antwerp)\b", "Belgium", "Europe"),
    (r"\b(Austrian|Austria|Vienna)\b", "Austria", "Europe"),
    (r"\b(Czech|Prague|Romanian|Romania|Bucharest|Greek|Greece|Athens|Hungarian|Budapest|Luxembourg)\b", "", "Europe"),
    (r"\b(European|Europe's|Europe-based)\b", "", "Europe"),
    (r"\b(US|U\.S\.|US-based|American|San Francisco|New York|NYC|Silicon Valley|Boston|Seattle|Austin|Palo Alto|Los Angeles|Miami|Chicago)\b", "US", "North America"),
    (r"\b(Canadian|Canada|Toronto|Montreal|Vancouver|Ottawa)\b", "Canada", "North America"),
    (r"\b(Japanese|Japan|Tokyo|Osaka|Kyoto)\b", "Japan", "Asia"),
    (r"\b(Chinese|China|Beijing|Shanghai|Shenzhen|Hangzhou|Hong Kong)\b", "China", "Asia"),
    # outside the scouting areas: rounds from these places are dropped
    (r"\b(Indian|India|Bengaluru|Bangalore|Mumbai|Delhi|Israeli|Israel|Tel Aviv|Singapore|Singaporean|Korean|Korea|Seoul|Taiwan|Taiwanese|Indonesian|Indonesia|Jakarta|Vietnam|Vietnamese|Australian|Australia|Sydney|Melbourne|New Zealand|Brazilian|Brazil|Sao Paulo|Mexican|Mexico|Argentina|Chile|Colombia|UAE|Dubai|Abu Dhabi|Saudi|Riyadh|Egypt|Egyptian|Colombian|Peruvian|Chilean|Nigeria|Nigerian|Kenya|Kenyan|South Africa|Turkish|Turkey|Istanbul|Pakistan)\b", "", "Other"),
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
        elif cur in ("yen", "jpy"):
            musd *= CONFIG["jpy_to_usd"]
        elif cur in ("yuan", "cny", "rmb"):
            musd *= CONFIG["cny_to_usd"]
        return round(musd, 1), m.group(0).strip()
    return None, None


DESCRIPTORS = {"startup", "start-up", "company", "firm", "platform", "maker", "chipmaker", "developer",
               "provider", "fintech", "insurtech", "proptech", "legaltech", "healthtech", "unicorn", "giant",
               "specialist", "agent", "database", "app", "ai", "robotics", "software", "saas", "tool", "chip",
               "team", "lead", "model", "law", "group", "operator", "brand", "scaleup", "scale-up"}


def parse_company(title):
    m = VERB_RE.search(title)
    if not m:
        return None, ""
    before = title[: m.start()].strip()
    before = re.split(r"[:}|]\s*", before)[-1]          # "Exclusive: X", "{Funding Alert} X"
    if ", " in before:
        before = before.split(", ")[0]                   # "X, founded by ..., raises"
    words = before.replace("’", "'").split()
    name = []
    for w in reversed(words):
        lw = w.lower().strip(",")
        if lw.startswith(("ex-", "former")) or (lw.endswith("'s") or "-based" in lw or "-led" in lw or "-backed" in lw
                or "-headquartered" in lw or re.search(r"-[a-z]", w)):
            break                                        # "London's", "Paris-based", "Nine-person"
        if name and (lw in DESCRIPTORS or any(re.fullmatch(pat, w) for pat, _, _ in PLACES)):
            break                                        # "Platform Supabase", "Miami Fintech Jeeves"
        if w[:1].isupper() or w[:1].isdigit():
            name.insert(0, w)
        else:
            break
    while name and name[0].lower() in DESCRIPTORS:        # "Startup Reco" -> "Reco"
        name.pop(0)
    if name and all(w.lower() in DESCRIPTORS for w in name):
        name = []
    company = " ".join(name).strip(" ,:;'\"")
    descriptor = " ".join(words[: len(words) - len(name)]).strip(" ,")
    return (company or None), descriptor


def detect_place(*texts):
    """Country and region of the earliest place mentioned (title first, then summary)."""
    for text in texts:
        best = None
        for pat, country, region in PLACES:
            m = re.search(pat, text)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), country, region)
        if best:
            return best[1], best[2]
    return "", ""


def area_threshold(country, region):
    """Threshold in $M if the place is inside a scouting area, else None (drop)."""
    if region == "Unknown":
        u = CONFIG["unknown_region"]
        return u["threshold_musd"] if u.get("keep", True) else None
    for a in CONFIG["scouting_areas"]:
        if a["region"] != region:
            continue
        if a["countries"] == "all" or country in a["countries"]:
            return a["threshold_musd"]
    return None


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


# ---------- Finding a company's location when the headline doesn't say ----------
WD_COUNTRIES = {  # Wikidata country ids -> (country, region)
    "Q30": ("US", "North America"), "Q16": ("Canada", "North America"),
    "Q17": ("Japan", "Asia"), "Q148": ("China", "Asia"), "Q8646": ("China", "Asia"),
    "Q145": ("UK", "Europe"), "Q183": ("Germany", "Europe"), "Q142": ("France", "Europe"),
    "Q55": ("Netherlands", "Europe"), "Q34": ("Sweden", "Europe"), "Q35": ("Denmark", "Europe"),
    "Q33": ("Finland", "Europe"), "Q20": ("Norway", "Europe"), "Q29": ("Spain", "Europe"),
    "Q38": ("Italy", "Europe"), "Q39": ("Switzerland", "Europe"), "Q27": ("Ireland", "Europe"),
    "Q36": ("Poland", "Europe"), "Q191": ("Estonia", "Europe"), "Q37": ("Lithuania", "Europe"),
    "Q211": ("Latvia", "Europe"), "Q45": ("Portugal", "Europe"), "Q31": ("Belgium", "Europe"),
    "Q40": ("Austria", "Europe"), "Q213": ("Czechia", "Europe"), "Q218": ("Romania", "Europe"),
    "Q41": ("Greece", "Europe"), "Q28": ("Hungary", "Europe"), "Q32": ("Luxembourg", "Europe"),
    "Q224": ("Croatia", "Europe"), "Q215": ("Slovenia", "Europe"), "Q214": ("Slovakia", "Europe"),
    "Q219": ("Bulgaria", "Europe"), "Q189": ("Iceland", "Europe"), "Q212": ("Ukraine", "Europe"),
    "Q229": ("Cyprus", "Europe"), "Q233": ("Malta", "Europe"), "Q403": ("Serbia", "Europe"),
    "Q668": ("India", "Other"), "Q801": ("Israel", "Other"), "Q334": ("Singapore", "Other"),
    "Q884": ("South Korea", "Other"), "Q408": ("Australia", "Other"), "Q155": ("Brazil", "Other"),
    "Q865": ("Taiwan", "Other"), "Q252": ("Indonesia", "Other"), "Q881": ("Vietnam", "Other"),
    "Q96": ("Mexico", "Other"), "Q878": ("UAE", "Other"), "Q851": ("Saudi Arabia", "Other"),
    "Q1033": ("Nigeria", "Other"), "Q114": ("Kenya", "Other"), "Q258": ("South Africa", "Other"),
    "Q43": ("Turkey", "Other"), "Q664": ("New Zealand", "Other"), "Q414": ("Argentina", "Other"),
}
SOURCE_HINTS = {  # outlets that mostly cover one region
    "eu-startups.com": ("", "Europe"), "sifted.eu": ("", "Europe"), "tech.eu": ("", "Europe"),
    "maddyness.com": ("France", "Europe"), "frenchweb.fr": ("France", "Europe"),
    "gruenderszene.de": ("Germany", "Europe"), "deutsche-startups.de": ("Germany", "Europe"),
    "uktn.co.uk": ("UK", "Europe"), "uktech.news": ("UK", "Europe"), "siliconcanals.com": ("", "Europe"),
    "betakit.com": ("Canada", "North America"), "technode.com": ("China", "Asia"),
    "36kr.com": ("China", "Asia"), "pandaily.com": ("China", "Asia"), "thebridge.jp": ("Japan", "Asia"),
    "inc42.com": ("India", "Other"), "yourstory.com": ("India", "Other"), "entrackr.com": ("India", "Other"),
    "calcalistech.com": ("Israel", "Other"), "techinasia.com": ("", "Other"), "e27.co": ("", "Other"),
}
TLD_HINTS = {".de": ("Germany", "Europe"), ".fr": ("France", "Europe"), ".it": ("Italy", "Europe"),
             ".es": ("Spain", "Europe"), ".nl": ("Netherlands", "Europe"), ".se": ("Sweden", "Europe"),
             ".dk": ("Denmark", "Europe"), ".fi": ("Finland", "Europe"), ".no": ("Norway", "Europe"),
             ".pl": ("Poland", "Europe"), ".at": ("Austria", "Europe"), ".be": ("Belgium", "Europe"),
             ".ch": ("Switzerland", "Europe"), ".ie": ("Ireland", "Europe"), ".pt": ("Portugal", "Europe"),
             ".uk": ("UK", "Europe"), ".eu": ("", "Europe"), ".jp": ("Japan", "Asia"),
             ".cn": ("China", "Asia"), ".ca": ("Canada", "North America"), ".in": ("India", "Other")}
COMPANY_WORDS = re.compile(r"compan|startup|business|firm|developer|manufacturer|platform|provider|maker|bank|fintech|enterprise|software|service|brand|retailer|lab", re.I)
MEMORY = {}      # company -> [country, region], saved in data/companies.json
STATS = {"wikidata_found": 0, "wikidata_errors": 0, "news_found": 0, "news_errors": 0, "first_error": ""}


def _err(kind, company, e):
    STATS[kind] += 1
    if not STATS["first_error"]:
        STATS["first_error"] = f"{kind[:-7]} lookup for {company}: {e}"[:300]
_WD_CACHE = {}
NOT_FOUND = {}   # company -> date last searched without success (retried after 14 days)
LOOKUPS = {"left": 80}   # max new company lookups per run


def country_region(name):
    """Map a country name written by the user (overrides) to (country, region)."""
    for pat, country, region in PLACES:
        if re.search(pat, name, re.I):
            return country or name, region
    for c, r in WD_COUNTRIES.values():
        if c.lower() == name.lower():
            return c, r
    return name, "Other"


def _wd(params):
    url = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(dict(params, format="json"))
    req = urllib.request.Request(url, headers={
        "User-Agent": "FundingRadar/1.1 (https://github.com/nathangaudet-jpg/Funding-radar-; weekly funding scouting) python-urllib",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _claim_id(entity, prop):
    try:
        return entity["claims"][prop][0]["mainsnak"]["datavalue"]["value"]["id"]
    except Exception:
        return None


def wikidata_location(company):
    """Look the company up on Wikidata (free): description first, then country / HQ claims."""
    if company in _WD_CACHE:
        return _WD_CACHE[company]
    result = ("", "")
    try:
        hits = _wd({"action": "wbsearchentities", "search": company, "language": "en", "type": "item", "limit": 5}).get("search", [])
        hit = next((h for h in hits if COMPANY_WORDS.search(h.get("description", ""))
                    and norm(h.get("label", "")) == norm(company)), None)
        if hit:
            c, r = detect_place(hit.get("description", ""))
            if r:
                result = (c, r)
            else:
                ent = _wd({"action": "wbgetentities", "ids": hit["id"], "props": "claims"})["entities"][hit["id"]]
                qid = _claim_id(ent, "P17")
                if not qid:
                    hq = _claim_id(ent, "P159")
                    if hq:
                        city = _wd({"action": "wbgetentities", "ids": hq, "props": "claims"})["entities"][hq]
                        qid = _claim_id(city, "P17")
                result = WD_COUNTRIES.get(qid, ("", ""))
    except Exception as e:
        _err("wikidata_errors", company, e)
    if result[1]:
        STATS["wikidata_found"] += 1
    _WD_CACHE[company] = result
    return result


def news_location(company):
    """Search Google News for other headlines about the company, e.g. 'Paris-based Inbolt'."""
    try:
        q = urllib.parse.quote(f'"{company}"')
        items = parse_feed(fetch(f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"), "lookup")
    except Exception as e:
        _err("news_errors", company, e)
        return "", ""
    votes = {}
    for it in items[:30]:
        for text in (it["title"], it["summary"]):
            i = text.lower().find(company.lower())
            if i < 0:
                continue
            c, r = detect_place(text[max(0, i - 45): i])   # place words just before the name
            if r:
                votes[(c, r)] = votes.get((c, r), 0) + 1
    time.sleep(1)
    if votes:
        best = max(votes, key=votes.get)
        regions = {r for _, r in votes}
        if len(regions) == 1 or votes[best] >= 2:
            STATS["news_found"] += 1
            return best
    return "", ""


DATELINE_RE = re.compile(r"\b([A-Z][A-Z .'-]{2,30}),\s+(?:[A-Z][a-z]{2,9}\.?\s+\d{1,2},\s+\d{4}|\d{1,2}\s+[A-Z][a-z]{2,9}\s+\d{4})")


def article_dateline(link):
    """Press releases start with a dateline like 'BERLIN, Oct. 1, 2026'. Only for direct links."""
    if not link or "news.google.com" in link:
        return "", ""
    try:
        raw = fetch(link)[:400000].decode("utf-8", "ignore")
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", " ", raw))
        m = DATELINE_RE.search(text)
        if m:
            return detect_place(m.group(1).title())
    except Exception:
        pass
    return "", ""


def source_hint(domain):
    domain = (domain or "").lower().replace("www.", "")
    for d, loc in SOURCE_HINTS.items():
        if domain.endswith(d):
            return loc
    for tld, loc in TLD_HINTS.items():
        if domain.endswith(tld) or domain.endswith(tld + "/"):
            return loc
    return "", ""


def resolve_location(company, link="", domain=""):
    """Try, in order: your manual list, companies seen before, Wikidata, press-release dateline, outlet."""
    key = norm(company)
    for name, place in CONFIG.get("company_locations", {}).items():
        if norm(name) == key:
            return country_region(place)
    if key in MEMORY:
        return tuple(MEMORY[key])
    checked = NOT_FOUND.get(key, "")
    if checked and checked > (date.today() - timedelta(days=14)).isoformat():
        return source_hint(domain)
    if LOOKUPS["left"] > 0:
        LOOKUPS["left"] -= 1
        for finder in (lambda: wikidata_location(company), lambda: news_location(company),
                       lambda: article_dateline(link)):
            c, r = finder()
            if r:
                MEMORY[key] = [c, r]
                NOT_FOUND.pop(key, None)
                return c, r
        NOT_FOUND[key] = date.today().isoformat()
    return source_hint(domain)  # weakest hint, not remembered


def classify(item, field_hint):
    text = item["title"] + " " + item["summary"]
    fields = [field_hint] if field_hint else match_fields(text)
    if not fields:
        return None
    musd, label = parse_amount(item["title"])
    if musd is None:
        musd, label = parse_amount(item["summary"])
    title = item["title"]
    if NOT_FUNDING.search(title) or NOT_FUNDING_CS.search(title):
        return None
    actor = big_actor_in(title)
    acq = ACQ_RE.search(title)
    verb = VERB_RE.search(title)
    if actor and acq and title.find(actor) < acq.start() and title.find(actor) < 25:
        kind = "big"
    elif verb and musd and FUNDING_HINT.search(text) and (
            parse_amount(title[verb.end(): verb.end() + 40])[0] or
            re.match(r"\W*(\w+\W+){0,3}?(funding|round|series|seed)", title[verb.end():], re.I)):
        kind = "startup"
    else:
        return None
    company, descriptor = parse_company(item["title"])
    if kind == "big":
        company = actor
    if not company:
        return None
    min_limit = min([a["threshold_musd"] for a in CONFIG["scouting_areas"]] + [CONFIG["unknown_region"]["threshold_musd"]])
    if kind == "startup" and musd < min_limit:
        return None
    domain = urllib.parse.urlparse(item.get("source_url") or item.get("link") or "").netloc
    country, region = detect_place(item["title"], item["summary"])
    if region and company and region != "Other":
        MEMORY[norm(company)] = [country, region]
    if not region:
        country, region = resolve_location(company, item.get("link", ""), domain)
    low = (label or "").lower()
    if not region and label and ("€" in label or "eur" in low or "£" in label or "gbp" in low):
        region = "Europe"
    region = region or "Unknown"
    limit = area_threshold(country, region)
    if limit is None:
        return None  # outside the scouting areas
    if kind == "startup" and musd < limit:
        return None
    stage = STAGE_RE.search(text)
    return {
        "company": company, "kind": kind, "field": fields[0],
        "headline": item["title"], "what": descriptor,
        "amount_usd_m": musd, "amount_label": label or "",
        "stage": stage.group(0).title() if stage else "",
        "country": country, "region": region,
        "date": item["published"].date().isoformat() if item["published"] else "",
        "source_name": item["source"], "source_url": item["link"], "source_domain": domain,
        "first_party": is_first_party(item, company),
    }


def queries_for(field):
    kw = " OR ".join(f'"{k}"' for k in field["keywords"][:6])
    return [f"({kw}) (raises OR funding OR \"Series A\" OR \"Series B\" OR \"Series C\" OR acquires)"]


def same_round(a, b):
    na, nb = norm(a["company"]), norm(b["company"])
    close = abs((a.get("amount_usd_m") or 0) - (b.get("amount_usd_m") or 0)) <= 0.08 * max(a.get("amount_usd_m") or 1, b.get("amount_usd_m") or 1)
    return a.get("kind") == b.get("kind") and (na == nb or (na[:4] == nb[:4] and close))


def weeks_apart(a, b):
    try:
        return abs((date.fromisoformat(a["week"]) - date.fromisoformat(b["week"])).days)
    except Exception:
        return 0


def dedupe(rows):
    """Merge the same round reported by several outlets ('Jeeves' / 'Jeevs' / 'Miami Fintech Jeeves')."""
    out = []
    for r in rows:
        twin = next((o for o in out if weeks_apart(o, r) <= 14 and same_round(o, r)), None)
        if twin is None:
            out.append(r)
        else:
            if (twin.get("region") == "Unknown" and r.get("region") != "Unknown") or (r.get("first_party") and not twin.get("first_party")):
                keep_id = twin.get("id")
                twin.update(r)
                twin["id"] = keep_id
            if len(r["company"]) < len(twin["company"]) and norm(twin["company"]).endswith(norm(r["company"])):
                twin["company"] = r["company"]
    return out


def main():
    DATA.mkdir(exist_ok=True)
    rounds_path, sweeps_path = DATA / "rounds.json", DATA / "sweeps.json"
    rounds = json.loads(rounds_path.read_text(encoding="utf-8")) if rounds_path.exists() else []
    sweeps = json.loads(sweeps_path.read_text(encoding="utf-8")) if sweeps_path.exists() else []
    mem_path = DATA / "companies.json"
    if mem_path.exists():
        MEMORY.update(json.loads(mem_path.read_text(encoding="utf-8")))

    NOT_FOUND.update(MEMORY.pop("_not_found", {}))

    # Re-check stored rounds with the current rules: clean names, drop non-funding news, locate
    kept, dropped = [], 0
    for r in rounds:
        if not r.get("headline"):
            kept.append(r)
            continue
        item = {"title": r["headline"], "summary": "", "link": r.get("source_url", ""),
                "source": r.get("source_name", ""), "published": None,
                "source_url": ("https://" + r["source_domain"]) if r.get("source_domain") else ""}
        fresh = classify(item, r.get("field"))
        if fresh is None:
            dropped += 1
            continue
        for k in ("id", "week", "date", "source_url", "source_domain"):
            fresh[k] = r.get(k, fresh.get(k))
        kept.append(fresh)
    rounds = dedupe(kept)
    print(f"Re-check: kept {len(rounds)} stored rounds, removed {dropped} (not funding news or outside your areas)")

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

    rounds = dedupe(rounds + sorted(new, key=lambda r: -(r["amount_usd_m"] or 0)))
    sweeps = [s for s in sweeps if s["week"] != week] + [{
        "week": week, "ran_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "count": sum(1 for r in rounds if r.get("week") == week),
        "feeds_checked": len(jobs), "feed_errors": errors[:10], "location_lookups": STATS}]
    rounds_path.write_text(json.dumps(rounds, indent=1, ensure_ascii=False), encoding="utf-8")
    MEMORY["_not_found"] = NOT_FOUND
    mem_path.write_text(json.dumps(MEMORY, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    sweeps_path.write_text(json.dumps(sweeps, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Week {week}: {len(new)} new rounds, {len(errors)} feed errors")
    for e in errors:
        print("  ", e)


if __name__ == "__main__":
    sys.exit(main())
