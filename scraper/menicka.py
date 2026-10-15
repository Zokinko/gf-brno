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
# Alergény vpísané do textu na konci: "(1, 3)", "1 3 7", "A - 6, 9", "(A-1,3,7)", "/1a,"
ALLERGEN_RE = re.compile(
    r"""(?P<prefix>\(\s*(?:(?:A|Alerg[eé]ny)\s*[:.\-–]?\s*)?
                  |\b(?:A|Al|Alerg[eé]ny)\s*[:.\-–]?\s*
                  |/\s*)?
        (?P<list>\d{1,2}[a-h]?(?:(?:\s*[,;/]\s*|\s+)\d{1,2}[a-h]?)*)
        \s*[,;]?\s*\)?\s*$""",
    re.X | re.I,
)
TOKEN_SPLIT_RE = re.compile(r"\s*[,;/]\s*|\s+")
GF_TEXT_RE = re.compile(r"bezlepk|bez\s+lepku|gluten[\s-]?free", re.I)
GF_ABBR_RE = re.compile(r"\bBL\b")  # case-sensitive, "bl" by bolo príliš voľné
NO_MENU_RE = re.compile(r"nebylo zadáno|není k dispozici|zavřeno|dovolená", re.I)


class ParseError(RuntimeError):
    pass


def parse_allergens(text: str) -> tuple[str, list[str] | None]:
    """Vráti (názov jedla bez alergénov a ceny, zoznam alergénov alebo None)."""
    t = PRICE_RE.sub("", " ".join(text.split())).strip().rstrip(" •·*")
    m = ALLERGEN_RE.search(t)
    if not m:
        return t, None
    tokens = [x.lower() for x in TOKEN_SPLIT_RE.split(m.group("list")) if x]
    if not all(1 <= int(re.match(r"\d+", x).group()) <= 14 for x in tokens):
        return t, None
    # Osamotené číslo bez zátvorky/prefixu je nejednoznačné ("Menu 2"). Akceptujeme len "1",
    # pri ktorej omyl znamená nanajvýš skryté jedlo, nie falošne bezlepkové.
    if len(tokens) == 1 and not m.group("prefix") and tokens[0] not in ("1", "1a"):
        return t, None
    name = t[: m.start()].strip(" -–,:|/")
    return name, list(dict.fromkeys(tokens))


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


def _parse_dish(el: Tag) -> dict | None:
    """Jedno jedlo z menicka.cz: <div class="nabidka_1">Názov <em title="...">1</em>...</div>."""
    em_allergens = [e.get_text(strip=True).lower() for e in el.find_all("em")]
    is_soup = el.find("i") is not None
    for junk in el.find_all(["em", "a"]):
        junk.extract()
    name, text_allergens = parse_allergens(_text(el))
    allergens = list(dict.fromkeys(em_allergens)) or text_allergens
    if not name or NO_MENU_RE.search(name):
        return None
    return {
        "name": name,
        "price": None,
        "allergens": allergens,
        "soup": is_soup or name.lower().startswith("polévka"),
        "status": classify(name, allergens),
    }


def parse_list_page(html: bytes, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select("div.menicka_detail")
    if not blocks:
        raise ParseError(
            "Nenašiel som bloky reštaurácií (div.menicka_detail). "
            "Štruktúra menicka.cz sa asi zmenila, pozri uložené HTML v debug adresári."
        )
    restaurants = []
    for block in blocks:
        name_el = block.select_one(".nazev a") or block.select_one(".nazev")
        if name_el is None:
            continue
        link = name_el if name_el.name == "a" else name_el.find("a")
        url = urljoin(page_url, link["href"]) if link and link.get("href") else None
        dishes: list[dict] = []
        menu = block.select_one("div.menicka")
        # Jedlá a ceny sú súrodenci v poradí: nabidka_N, cena, newrows, poradi_N, nabidka_N, ...
        for child in menu.find_all("div", recursive=False) if menu else []:
            classes = child.get("class", [])
            if any(c.startswith("nabidka") for c in classes):
                dish = _parse_dish(child)
                if dish:
                    dishes.append(dish)
            elif "cena" in classes and dishes and dishes[-1]["price"] is None:
                dishes[-1]["price"] = _text(child) or None
        restaurants.append({"name": _text(name_el), "url": url, "address": None, "dishes": dishes})
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
