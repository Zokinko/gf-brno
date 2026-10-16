"""Jednorazový prehľad podnikov v Brne: zoznam z OpenStreetMap + zmienky o bezlepkovom jedle na ich weboch.

Spustenie: python -m scraper.places --out site/data/places.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import sys
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .menicka import parse_allergens

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
# Brno podľa Wikidata (Q14960), aby sme nezáviseli od admin_level.
AMENITY_FILTER = '["amenity"~"^(restaurant|cafe|fast_food|pub|biergarten|ice_cream)$"]["name"]'
OVERPASS_QUERIES = [
    # Presne hranice mesta (Brno podľa Wikidata Q14960). Niektoré zrkadlá nemajú predpočítané oblasti
    # a vrátia prázdny výsledok, preto záložne obdĺžnik okolo Brna (zachytí aj kúsok okolia).
    ("hranice mesta", f'[out:json][timeout:180];area["wikidata"="Q14960"]->.brno;'
                      f'nwr{AMENITY_FILTER}(area.brno);out center tags;'),
    ("obdĺžnik okolo Brna", f'[out:json][timeout:180];nwr{AMENITY_FILTER}(49.109,16.428,49.295,16.728);'
                            f'out center tags;'),
]
# V Brne je podnikov rádovo viac ako tisíc; menej znamená chybnú alebo neúplnú odpoveď servera.
MIN_EXPECTED_PLACES = 200
USER_AGENT = "bezlepkove-brno/0.2 (osobny projekt, jednorazovy prechod webov)"
TIMEOUT_S = 12
MAX_BYTES = 4_000_000
MAX_PAGES_PER_SITE = 6
MAX_SNIPPETS = 6
SNIPPET_RADIUS = 110
PER_HOST_DELAY_S = 1.0

GF_RE = re.compile(
    r"bezlepk\w*|bez\s+lepku|bez\s+glutenu|gluten[\s-]?free|glutenfrei|senza\s+glutine|sans\s+gluten"
    r"|without\s+gluten|bezgluten\w*|celiak\w*|coeliac\w*|celiac\w*|(?-i:\(BL\))",
    re.I,
)
ALLERGEN_INFO_RE = re.compile(r"alerg[eé]n", re.I)
ALLERGEN_ONLY_RE = re.compile(r"^\(?\s*(?:A\s*[:\-–]?\s*)?\d{1,2}[a-h]?(?:[\s,;/]+\d{1,2}[a-h]?)*[\s,;)]*$", re.I)
MENU_LINK_RE = re.compile(r"menu|l[ií]stek|j[ií]deln|nab[ií]dk|karta|food|speise|allerg|alerg|bezlep|gluten|\.pdf",
                          re.I)
# Weby, ktoré sa bez prihlásenia/JS nedajú rozumne čítať, alebo nie sú web podniku.
SKIP_HOSTS_RE = re.compile(r"(facebook|instagram|fb)\.com|wolt\.com|foodora\.cz|bolt\.eu|tripadvisor|google\.", re.I)


# ---------- OpenStreetMap ----------

def fetch_osm(session: requests.Session, rounds: int = 3,
              min_places: int = MIN_EXPECTED_PLACES) -> list[dict]:
    """Verejné Overpass servery bývajú preťažené (429/504) alebo vrátia neúplné dáta,
    skúsi preto viac zrkadiel, oba dotazy a opakuje."""
    last_error: Exception | None = None
    for attempt in range(rounds):
        for url in OVERPASS_URLS:
            for label, query in OVERPASS_QUERIES:
                try:
                    resp = session.post(url, data={"data": query}, timeout=240)
                    resp.raise_for_status()
                    data = resp.json()
                    places = parse_osm(data)
                    if len(places) < min_places:
                        remark = data.get("remark", "")
                        raise RuntimeError(f"len {len(places)} podnikov {remark}".strip())
                    print(f"OSM dáta z {url} ({label})", flush=True)
                    return places
                except Exception as e:
                    last_error = e
                    print(f"Overpass {url} ({label}) zlyhal: {type(e).__name__}: {str(e)[:150]}", flush=True)
        if attempt < rounds - 1:
            wait = 30 * (attempt + 1)
            print(f"Všetky Overpass servery zlyhali, čakám {wait} s…", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Overpass nedostupný ani po {rounds} kolách") from last_error


def parse_osm(data: dict) -> list[dict]:
    places = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if lat is None or lon is None or not tags.get("name"):
            continue
        website = tags.get("website") or tags.get("contact:website") or tags.get("url")
        address = " ".join(filter(None, [tags.get("addr:street") or tags.get("addr:place"),
                                         tags.get("addr:housenumber")]))
        places.append({
            "id": f"{el['type']}/{el['id']}",
            "name": tags["name"],
            "type": tags.get("amenity"),
            "cuisine": tags.get("cuisine"),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "address": address or None,
            "website": normalize_url(website) if website else None,
            "osm_gf": tags.get("diet:gluten_free"),  # yes / only / limited / no
        })
    return places


def normalize_url(url: str) -> str | None:
    url = url.strip().split(";")[0].strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


# ---------- prechod webov ----------

class HostLimiter:
    """Najviac jeden request za PER_HOST_DELAY_S na jeden host."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str):
        with self._lock:
            now = time.monotonic()
            at = max(now, self._next.get(host, 0))
            self._next[host] = at + PER_HOST_DELAY_S
        if at > now:
            time.sleep(at - now)


def fetch_limited(session: requests.Session, url: str) -> tuple[bytes, str]:
    with session.get(url, timeout=TIMEOUT_S, stream=True, allow_redirects=True) as resp:
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "").lower()
        body = b""
        for chunk in resp.iter_content(64_000):
            body += chunk
            if len(body) > MAX_BYTES:
                break
        return body, ctype


def extract_text(body: bytes, ctype: str, url: str) -> tuple[str, list[str], BeautifulSoup | None]:
    """Vráti (text v jednom riadku na hľadanie zmienok, riadky na čítanie alergénov, soup alebo None pri PDF)."""
    if "pdf" in ctype or url.lower().endswith(".pdf") or body[:5] == b"%PDF-":
        from pypdf import PdfReader
        try:
            raw = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(body)).pages[:20])
        except Exception:
            return "", [], None
        soup = None
    else:
        soup = BeautifulSoup(body, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        raw = block_text(BeautifulSoup(str(soup), "html.parser"))
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    lines = [line for line in lines if line]
    return " ".join(lines), lines, soup


INLINE_TAGS = ["span", "b", "strong", "i", "em", "a", "small", "u", "sup", "sub", "font", "mark", "abbr"]


def block_text(soup: BeautifulSoup) -> str:
    """Text po blokoch (odsek, bunka, položka): jeden riadok = jeden blok bez ohľadu na zalomenia v zdrojáku."""
    for tag in soup.find_all(INLINE_TAGS):
        tag.insert_before(" ")
        tag.insert_after(" ")
        tag.unwrap()
    soup.smooth()
    return "\n".join(" ".join(t.split()) for t in soup.stripped_strings)


def scan_allergens(lines: list[str]) -> tuple[int, int]:
    """Spočíta položky lístka s číslami alergénov: (s alergénmi, z toho bez alergénu 1).

    Zvláda aj alergény na samostatnom riadku pod názvom jedla (časté v HTML aj PDF).
    """
    # Alergény po jednom na riadok ("1" / "3" / "7") spojiť k predchádzajúcemu riadku.
    merged: list[str] = []
    for line in lines:
        if merged and ALLERGEN_ONLY_RE.match(line) and len(line) < 40:
            merged[-1] += ", " + line
        else:
            merged.append(line)
    items = no_gluten = 0
    prev_name = None
    for line in merged:
        if len(line) > 250:
            prev_name = None
            continue
        name, allergens = parse_allergens(line)
        if allergens and not re.search(r"[^\W\d_]{3}", name) and prev_name:
            name = prev_name
        if allergens and re.search(r"[^\W\d_]{3}", name):
            items += 1
            no_gluten += not any(re.fullmatch(r"1[a-h]?", a) for a in allergens)
            prev_name = None
        else:
            prev_name = line if not allergens and re.search(r"[^\W\d_]{3}", line) else None
    return items, no_gluten


def find_snippets(text: str, url: str, limit: int = MAX_SNIPPETS) -> list[dict]:
    out = []
    last_end = -1
    for m in GF_RE.finditer(text):
        if m.start() < last_end:  # prekrývajúce sa úryvky spojiť
            continue
        a, b = max(0, m.start() - SNIPPET_RADIUS), min(len(text), m.end() + SNIPPET_RADIUS)
        out.append({"url": url, "text": " ".join(text[a:b].split()),
                    "key": re.sub(r"\W+", " ", text[m.start(): m.end() + 60].lower()).strip()})
        last_end = b
        if len(out) >= limit:
            break
    return out


def menu_links(soup: BeautifulSoup, base_url: str, limit: int) -> list[str]:
    base_host = urlparse(base_url).netloc.removeprefix("www.")
    seen, links = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"]).split("#")[0]
        if not href.startswith("http") or href in seen:
            continue
        host = urlparse(href).netloc.removeprefix("www.")
        is_pdf = href.lower().split("?")[0].endswith(".pdf")
        if host != base_host and not is_pdf:
            continue
        label = f"{href} {a.get_text(' ')}"
        if MENU_LINK_RE.search(label):
            seen.add(href)
            links.append(href)
    # PDF a odkazy s "bezlep"/"alerg" najprv
    links.sort(key=lambda u: (not re.search(r"bezlep|gluten|alerg|allerg", u, re.I), not u.lower().endswith(".pdf")))
    return links[:limit]


def allowed(session: requests.Session, url: str, cache: dict, lock: threading.Lock) -> bool:
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    with lock:
        if origin in cache:
            return cache[origin].can_fetch(USER_AGENT, url)
    rp = robotparser.RobotFileParser()
    try:
        r = session.get(origin + "/robots.txt", timeout=8)
        rp.parse(r.text.splitlines() if r.status_code == 200 else [])
    except Exception:
        rp.parse([])
    with lock:
        cache[origin] = rp
    return rp.can_fetch(USER_AGENT, url)


def crawl_site(session, place, limiter, robots, robots_lock) -> dict:
    url = place["website"]
    if not url:
        return {"status": "no_website"}
    if SKIP_HOSTS_RE.search(urlparse(url).netloc):
        return {"status": "skipped"}
    result = {"status": "ok", "pages": 0, "allergen_info": False, "snippets": [],
              "allergen_items": 0, "allergen_items_no1": 0}
    queue, done, seen_snippets = [url], set(), set()
    while queue and result["pages"] < MAX_PAGES_PER_SITE:
        page = queue.pop(0)
        if page in done:
            continue
        done.add(page)
        try:
            if not allowed(session, page, robots, robots_lock):
                if page == url:
                    return {"status": "robots_disallow"}
                continue
            limiter.wait(urlparse(page).netloc)
            body, ctype = fetch_limited(session, page)
            text, lines, soup = extract_text(body, ctype, page)
        except Exception as e:
            if page == url:  # nedostupná titulka = nedostupný web
                return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:120]}"}
            continue
        result["pages"] += 1
        result["allergen_info"] |= bool(ALLERGEN_INFO_RE.search(text))
        items, no1 = scan_allergens(lines)
        result["allergen_items"] += items
        result["allergen_items_no1"] += no1
        # Rovnaký text na viacerých podstránkach (pätička, banner) počítať len raz.
        for sn in find_snippets(text, page):
            key = sn.pop("key")
            if key not in seen_snippets and len(result["snippets"]) < MAX_SNIPPETS:
                seen_snippets.add(key)
                result["snippets"].append(sn)
        if soup is not None and page == url:
            queue += [u for u in menu_links(soup, page, MAX_PAGES_PER_SITE) if u not in done]
    return result


# ---------- menicka.cz ----------

NAME_STOP_WORDS = {"restaurace", "restaurant", "bistro", "cafe", "kavarna", "pub", "bar", "pizzeria", "hospoda",
                   "jidelna", "u", "na", "v", "ve", "the", "a"}


def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    words = re.sub(r"[^a-z0-9]+", " ", s).split()
    core = [w for w in words if w not in NAME_STOP_WORDS]
    return " ".join(core or words)


def attach_menicka(places: list[dict], stats: dict) -> int:
    """Spáruje štatistiky z denného menu (site/data/stats.json) s podnikmi z OSM podľa názvu."""
    index = defaultdict(list)
    for p in places:
        index[norm_name(p["name"])].append(p)
    matched = 0
    for key, s in stats.items():
        cands = index.get(norm_name(s["name"]), [])
        if len(cands) == 1:  # pri reťazcoch s viacerými pobočkami radšej nič
            cands[0]["menicka"] = {"url": key if key.startswith("http") else None, **s}
            matched += 1
    return matched


# ---------- hodnotenie ----------

def score(place: dict) -> tuple[int, str]:
    """Hrubé skóre 0–3 pred ručnou/LLM kontrolou úryvkov; dôvod spája všetky nájdené signály."""
    osm = place.get("osm_gf")
    web = place.get("web", {})
    snippets = web.get("snippets", [])
    mn = place.get("menicka") or {}
    signals: list[tuple[int, str]] = []
    if osm == "only":
        signals.append((3, "100 % bezlepkové (OSM)"))
    elif osm == "yes":
        signals.append((2, "bezlepkové jedlá (OSM)"))
    elif osm == "limited":
        signals.append((1, "obmedzená ponuka bez lepku (OSM)"))
    if len(snippets) >= 2:
        signals.append((2, f"zmienky o bezlepkovom na webe ({len(snippets)})"))
    elif snippets:
        signals.append((1, "zmienka o bezlepkovom na webe"))
    if mn.get("days_explicit"):
        signals.append((2, f"menicka.cz: výslovne bezlepkové jedlo v {mn['days_explicit']} z {mn['days_seen']} dní"))
    elif mn.get("days_gf"):
        signals.append((1, f"menicka.cz: jedlo bez alergénu 1 v {mn['days_gf']} z {mn['days_seen']} dní"))
    no1 = web.get("allergen_items_no1", 0)
    if no1 >= 3:
        signals.append((1, f"lístok na webe: {no1} z {web['allergen_items']} položiek bez alergénu 1"))
    if not signals:
        return 0, ""
    return max(s for s, _ in signals), "; ".join(r for _, r in signals)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("site/data/places.json"))
    ap.add_argument("--osm-json", type=Path, help="použiť uložený výstup z Overpass namiesto sťahovania")
    ap.add_argument("--limit", type=int, help="len prvých N podnikov (na skúšku)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--no-crawl", action="store_true", help="len zoznam z OSM, bez prechodu webov")
    ap.add_argument("--menicka-stats", type=Path, default=Path("site/data/stats.json"),
                    help="štatistiky z denného menu na spárovanie podľa názvu")
    args = ap.parse_args(argv)

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.mount("https://", requests.adapters.HTTPAdapter(pool_maxsize=args.workers * 2))

    if args.osm_json:
        places = parse_osm(json.loads(args.osm_json.read_text(encoding="utf-8")))
    else:
        places = fetch_osm(session)
    places.sort(key=lambda p: p["name"].lower())
    if args.limit:
        places = places[: args.limit]
    print(f"OSM: {len(places)} podnikov, s webom {sum(1 for p in places if p['website'])}", flush=True)

    if not args.no_crawl:
        limiter, robots, robots_lock = HostLimiter(), {}, threading.Lock()
        with ThreadPoolExecutor(args.workers) as pool:
            futures = {pool.submit(crawl_site, session, p, limiter, robots, robots_lock): p for p in places}
            for i, fut in enumerate(as_completed(futures), 1):
                futures[fut]["web"] = fut.result()
                if i % 100 == 0:
                    print(f"  web {i}/{len(places)}", flush=True)
    if args.menicka_stats.exists():
        stats = json.loads(args.menicka_stats.read_text(encoding="utf-8"))
        print(f"menicka.cz: spárovaných {attach_menicka(places, stats)} z {len(stats)} podnikov", flush=True)
    for p in places:
        p.setdefault("web", {"status": "not_crawled"})
        p["score"], p["reason"] = score(p)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated": dt.date.today().isoformat(), "source": "OpenStreetMap + weby podnikov",
               "places": places}
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    statuses = {}
    for p in places:
        statuses[p["web"]["status"]] = statuses.get(p["web"]["status"], 0) + 1
    by_score = {s: sum(p["score"] == s for p in places) for s in (3, 2, 1, 0)}
    print(f"weby: {statuses}")
    print(f"skóre: {by_score}  (3 = 100 % BL, 2 = silný signál, 1 = zmienka)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
