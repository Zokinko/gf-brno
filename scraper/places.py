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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Brno podľa Wikidata (Q14960), aby sme nezáviseli od admin_level.
OVERPASS_QUERY = """
[out:json][timeout:180];
area["wikidata"="Q14960"]->.brno;
nwr["amenity"~"^(restaurant|cafe|fast_food|pub|biergarten|ice_cream)$"]["name"](area.brno);
out center tags;
"""
USER_AGENT = "bezlepkove-brno/0.2 (osobny projekt, jednorazovy prechod webov)"
TIMEOUT_S = 12
MAX_BYTES = 4_000_000
MAX_PAGES_PER_SITE = 6
MAX_SNIPPETS = 6
SNIPPET_RADIUS = 110
PER_HOST_DELAY_S = 1.0

GF_RE = re.compile(r"bezlepk\w*|bez\s+lepku|gluten[\s-]?free|glutenfree|bezgluten\w*|celiak\w*|coeliac|celiac",
                   re.I)
ALLERGEN_INFO_RE = re.compile(r"alerg[eé]n", re.I)
MENU_LINK_RE = re.compile(r"menu|l[ií]stek|j[ií]deln|nab[ií]dk|karta|food|speise|allerg|alerg|bezlep|gluten|\.pdf",
                          re.I)
# Weby, ktoré sa bez prihlásenia/JS nedajú rozumne čítať, alebo nie sú web podniku.
SKIP_HOSTS_RE = re.compile(r"(facebook|instagram|fb)\.com|wolt\.com|foodora\.cz|bolt\.eu|tripadvisor|google\.", re.I)


# ---------- OpenStreetMap ----------

def fetch_osm(session: requests.Session) -> list[dict]:
    resp = session.post(OVERPASS_URL, data={"data": OVERPASS_QUERY}, timeout=240)
    resp.raise_for_status()
    return parse_osm(resp.json())


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


def extract_text(body: bytes, ctype: str, url: str) -> tuple[str, BeautifulSoup | None]:
    if "pdf" in ctype or url.lower().endswith(".pdf") or body[:5] == b"%PDF-":
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(body))
            return "\n".join((p.extract_text() or "") for p in reader.pages[:20]), None
        except Exception:
            return "", None
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split()), soup


def find_snippets(text: str, url: str, limit: int = MAX_SNIPPETS) -> list[dict]:
    out = []
    last_end = -1
    for m in GF_RE.finditer(text):
        if m.start() < last_end:  # prekrývajúce sa úryvky spojiť
            continue
        a, b = max(0, m.start() - SNIPPET_RADIUS), min(len(text), m.end() + SNIPPET_RADIUS)
        out.append({"url": url, "text": " ".join(text[a:b].split())})
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
    result = {"status": "ok", "pages": 0, "allergen_info": False, "snippets": []}
    queue, done = [url], set()
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
            text, soup = extract_text(body, ctype, page)
        except Exception as e:
            if page == url:  # nedostupná titulka = nedostupný web
                return {"status": "error", "error": f"{type(e).__name__}: {str(e)[:120]}"}
            continue
        result["pages"] += 1
        result["allergen_info"] |= bool(ALLERGEN_INFO_RE.search(text))
        room = MAX_SNIPPETS - len(result["snippets"])
        if room > 0:
            result["snippets"] += find_snippets(text, page, room)
        if soup is not None and page == url:
            queue += [u for u in menu_links(soup, page, MAX_PAGES_PER_SITE) if u not in done]
    return result


# ---------- hodnotenie ----------

def score(place: dict) -> tuple[int, str]:
    """Hrubé skóre 0–3 pred ručnou/LLM kontrolou úryvkov."""
    osm = place.get("osm_gf")
    web = place.get("web", {})
    snippets = web.get("snippets", [])
    if osm == "only":
        return 3, "100 % bezlepkové (OSM)"
    if osm == "yes":
        return 2, "bezlepkové jedlá (OSM)"
    if len(snippets) >= 2:
        return 2, "viac zmienok na webe"
    if snippets:
        return 1, "zmienka na webe"
    if osm == "limited":
        return 1, "obmedzene (OSM)"
    return 0, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("site/data/places.json"))
    ap.add_argument("--osm-json", type=Path, help="použiť uložený výstup z Overpass namiesto sťahovania")
    ap.add_argument("--limit", type=int, help="len prvých N podnikov (na skúšku)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--no-crawl", action="store_true", help="len zoznam z OSM, bez prechodu webov")
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
