"""Scraper denných menu z menicka.cz (Brno) so zameraním na jedlá bez lepku.

Alergén 1 = obilniny obsahujúce lepok (1a pšenica, 1b raž, 1c jačmeň, ...).
Jedlo bez alergénu 1 NEZNAMENÁ, že je bezpečné pre celiatika (kontaminácia
v kuchyni), je to len signál.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://www.menicka.cz/"
LIST_URL = BASE + "brno.html"
USER_AGENT = "bezlepkove-brno/0.1 (osobny projekt, par requestov denne)"
TZ = ZoneInfo("Europe/Prague")
REQUEST_DELAY_S = 3

# Cena na konci textu: "145 Kč", "145,- Kč", "145Kč"
PRICE_RE = re.compile(r"\s*\d+\s*(?:,-|,–|\.-)?\s*Kč\s*$", re.I)
# Zoznam alergénov na konci textu: "(1,3,7)", "A: 1a, 3, 7", "1,3,7"
ALLERGEN_RE = re.compile(
    r"""(?P<prefix>\(|\b(?:A|Al|Alerg[eé]ny)\s*[:.]\s*)?
        (?P<list>\d{1,2}[a-h]?(?:\s*[,;/]\s*\d{1,2}[a-h]?)*)
        \s*\)?\s*$""",
    re.X | re.I,
)
TOKEN_SPLIT_RE = re.compile(r"\s*[,;/]\s*")
GF_TEXT_RE = re.compile(r"bezlepk|bez\s+lepku|gluten[\s-]?free", re.I)
GF_ABBR_RE = re.compile(r"\bBL\b")  # case-sensitive, "bl" by bolo príliš voľné
NO_MENU_RE = re.compile(r"nebylo zadáno|není k dispozici|zavřeno|dovolená", re.I)


class ParseError(RuntimeError):
    pass


def parse_allergens(text: str) -> tuple[str, list[str] | None]:
    """Vráti (názov jedla bez alergénov a ceny, zoznam alergénov alebo None)."""
    t = PRICE_RE.sub("", " ".join(text.split())).strip()
    m = ALLERGEN_RE.search(t)
    if not m:
        return t, None
    tokens = [x.lower() for x in TOKEN_SPLIT_RE.split(m.group("list")) if x]
    if not all(1 <= int(re.match(r"\d+", x).group()) <= 14 for x in tokens):
        return t, None
    # Osamotené číslo bez zátvorky/prefixu je nejednoznačné ("Menu 2").
    if len(tokens) == 1 and not m.group("prefix"):
        return t, None
    name = t[: m.start()].strip(" -–,:|")
    return name, tokens


def classify(name: str, allergens: list[str] | None) -> str:
    if GF_TEXT_RE.search(name) or GF_ABBR_RE.search(name):
        return "explicit_gf"
    if allergens is None:
        return "unknown"
    if any(re.fullmatch(r"1[a-h]?", a) for a in allergens):
        return "gluten"
    return "no_gluten"


def _text(el: Tag | None) -> str:
    return " ".join(el.get_text(" ").split()) if el else ""


def _parse_item(li: Tag) -> dict | None:
    dish_el = li.select_one(".polozka") or li
    price = _text(li.select_one(".cena"))
    allergen_el = li.select_one("[class*=alerg]")

    if allergen_el is not None:
        allergen_text = _text(allergen_el)
        allergen_el.extract()
        name = PRICE_RE.sub("", _text(dish_el)).strip()
        if price:
            name = name.replace(price, "").strip()
        tokens = [x.lower() for x in re.findall(r"\d{1,2}[a-h]?", allergen_text)]
        allergens = tokens or None
    else:
        raw = _text(dish_el)
        if price and dish_el is li:
            raw = raw.replace(price, "")
        name, allergens = parse_allergens(raw)

    if not name or NO_MENU_RE.search(name):
        return None
    classes = " ".join(li.get("class", []))
    return {
        "name": name,
        "price": price or None,
        "allergens": allergens,
        "soup": "polevka" in classes or name.lower().startswith("polévka"),
        "status": classify(name, allergens),
    }


def parse_list_page(html: bytes, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select("div.menicka_detail")
    if not blocks:
        raise ParseError(
            "Nenašiel som bloky reštaurácií (div.menicka_detail). "
            "Štruktúra menicka.cz je asi iná, pozri uložené HTML v debug adresári."
        )
    restaurants = []
    for block in blocks:
        name_el = block.select_one(".nazev a") or block.select_one(".nazev, h2, h3")
        if name_el is None:
            continue
        link = name_el if name_el.name == "a" else name_el.find("a")
        url = urljoin(page_url, link["href"]) if link and link.get("href") else None
        items = block.select(".menicka li") or block.select("li")
        dishes = [d for li in items if (d := _parse_item(li))]
        restaurants.append({
            "name": _text(name_el),
            "url": url,
            "address": _text(block.select_one(".adresa, .address")) or None,
            "dishes": dishes,
        })
    return restaurants


def find_next_page(html: bytes, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("a", rel="next")
    if link is None:
        link = soup.find("a", string=re.compile(r"^\s*(další|dalsi|»)", re.I))
    return urljoin(page_url, link["href"]) if link and link.get("href") else None


def fetch(session: requests.Session, url: str) -> bytes:
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content  # bytes: BeautifulSoup si kódovanie zistí z <meta charset>


def allowed_by_robots(url: str) -> bool:
    rp = robotparser.RobotFileParser(urljoin(url, "/robots.txt"))
    try:
        rp.read()
    except Exception:
        return True
    return rp.can_fetch(USER_AGENT, url)


def build_stats(history_dir: Path) -> dict:
    stats: dict[str, dict] = {}
    for f in sorted(history_dir.glob("*.json")):
        day = json.loads(f.read_text(encoding="utf-8"))
        for r in day["restaurants"]:
            key = r["url"] or r["name"]
            s = stats.setdefault(key, {"name": r["name"], "days_seen": 0,
                                       "days_gf": 0, "days_explicit": 0})
            statuses = {d["status"] for d in r["dishes"]}
            s["days_seen"] += 1
            s["days_gf"] += bool(statuses & {"no_gluten", "explicit_gf"})
            s["days_explicit"] += "explicit_gf" in statuses
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("site/data"))
    ap.add_argument("--debug-dir", type=Path, default=Path("debug"))
    ap.add_argument("--html", type=Path, help="parsovať lokálny HTML súbor namiesto sťahovania")
    ap.add_argument("--max-pages", type=int, default=15)
    args = ap.parse_args(argv)

    now = dt.datetime.now(TZ)
    pages: list[tuple[str, bytes]] = []
    if args.html:
        pages.append((LIST_URL, args.html.read_bytes()))
    else:
        if not allowed_by_robots(LIST_URL):
            print("robots.txt nepovoľuje sťahovanie, končím.", file=sys.stderr)
            return 1
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        url: str | None = LIST_URL
        seen = set()
        while url and url not in seen and len(pages) < args.max_pages:
            seen.add(url)
            html = fetch(session, url)
            pages.append((url, html))
            url = find_next_page(html, url)
            if url:
                time.sleep(REQUEST_DELAY_S)

    args.debug_dir.mkdir(parents=True, exist_ok=True)
    for i, (_, html) in enumerate(pages):
        (args.debug_dir / f"page{i + 1}.html").write_bytes(html)

    restaurants = []
    for url, html in pages:
        restaurants.extend(parse_list_page(html, url))

    payload = {
        "date": now.date().isoformat(),
        "fetched_at": now.isoformat(timespec="seconds"),
        "source": LIST_URL,
        "restaurants": restaurants,
    }
    history_dir = args.out / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    dump = json.dumps(payload, ensure_ascii=False, indent=1)
    (args.out / "today.json").write_text(dump, encoding="utf-8")
    (history_dir / f"{payload['date']}.json").write_text(dump, encoding="utf-8")
    (args.out / "stats.json").write_text(
        json.dumps(build_stats(history_dir), ensure_ascii=False, indent=1), encoding="utf-8")

    dishes = [d for r in restaurants for d in r["dishes"]]
    counts = {s: sum(d["status"] == s for d in dishes)
              for s in ("explicit_gf", "no_gluten", "gluten", "unknown")}
    print(f"{len(pages)} strán, {len(restaurants)} reštaurácií, {len(dishes)} jedál: {counts}")
    if dishes and counts["unknown"] / len(dishes) > 0.8:
        print("Varovanie: pri >80 % jedál sa nenašli alergény, parser treba asi upraviť.",
              file=sys.stderr)
    return 0
