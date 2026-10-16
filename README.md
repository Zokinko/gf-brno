# Bezlepkové Brno

Každý pracovný deň stiahne denné menu brnianskych reštaurácií z [menicka.cz](https://www.menicka.cz/brno.html),
podľa čísel alergénov nájde jedlá bez lepku (alergén 1) a zobrazí ich na jednoduchej stránke.

> Bez alergénu 1 znamená len to, že v surovinách nie je lepok. O kontaminácii v kuchyni to nič nehovorí.

## Ako sa jedlá označujú

| Stav | Význam |
|---|---|
| `explicit_gf` | V názve je „bezlepkové“, „bez lepku“, „gluten free“ alebo „BL“ |
| `no_gluten` | Alergény sú uvedené a 1 (ani 1a, 1b…) medzi nimi nie je |
| `gluten` | Obsahuje alergén 1 |
| `unknown` | Alergény sa nenašli |

`stats.json` počíta pre každú reštauráciu, koľko sledovaných dní mala aspoň jedno vhodné jedlo.

## Mapa všetkých podnikov (jednorazovo)

`site/mapa.html` zobrazí podniky v Brne z OpenStreetMap (reštaurácie, kaviarne, bistrá, krčmy…) a zvýrazní tie,
ktoré majú na webe alebo v OSM zmienku o bezlepkovom jedle. Dáta vytvorí workflow **Prehľad podnikov (jednorazovo)**
(Actions → Run workflow, trvá desiatky minút):

1. Stiahne z OSM všetky podniky v Brne (vrátane značky `diet:gluten_free`).
2. Prejde ich weby: titulku a najviac 5 podstránok alebo PDF s menu a alergénmi. Rešpektuje `robots.txt`
   a na jeden web pošle najviac 1 request za sekundu. Facebook, Instagram a donáškové služby preskočí.
3. Uloží úryvky so zmienkami („bezlepkové“, „bez lepku“, „gluten free“, „celiakie“) a hrubé skóre
   do `site/data/places.json`. Súbor je aj v artifacte `places`.

Skóre: 3 = 100 % bezlepkové (OSM `only`), 2 = OSM `yes` alebo viac zmienok, 1 = jedna zmienka, 0 = nič.
Ručné hodnotenia sú v samostatnom súbore `site/data/reviews.json`, ktorý workflow neprepisuje:
- `reviews`: podľa ID podniku z `places.json` (napr. `"node/123"`) → `{"review": {"score": 2, "summary": "…"}, "fmgf": {…}}`.
  Ručné skóre má prednosť pred automatickým.
- `extra_places`: podniky, ktoré v OSM chýbajú (pridané ručne, s polohou).

Mapa oba súbory spojí pri načítaní. Nový beh „Prehľadu podnikov“ preto ručné hodnotenia nezmaže.
Ak podnik z OSM zmizne, jeho hodnotenie sa ticho ignoruje. Na skúšku: v poli „limit“ zadaj napr. `50`.

Lokálne: `python -m scraper.places --limit 50 --out site/data/places.json`

## Obľúbené reštaurácie

Hviezdička ☆ pri reštaurácii ju uloží do obľúbených v prehliadači (`localStorage`, bez prihlasovania).
Obľúbené sú zoradené navrchu a filter „len obľúbené“ ich ukáže aj v dni, keď nemajú vhodné jedlo.
Do iného zariadenia ich prenesieš odkazom „Skopírovať odkaz na prenos…“ dole na stránke.

## Nastavenie na GitHube

1. Vytvor repo (napr. `bezlepkove-brno`) a nahraj doň tieto súbory.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions**
3. **Settings → Actions → General → Workflow permissions: Read and write permissions**
4. **Actions → Denné menu → Run workflow**: prvé ručné spustenie.
5. Stránka bude na `https://<tvoj-ucet>.github.io/bezlepkove-brno/`.

Potom to beží samo po–pi ráno. GitHub Pages pre súkromné repo vyžaduje platený plán. Vo verejnom repe sú dáta
(len menu z verejného webu) viditeľné pre každého.

## Keď parser nefunguje

Parser počíta so štruktúrou menicka.cz z októbra 2026: reštaurácia `div.menicka_detail` → `.nazev a`,
v `div.menicka` idú za sebou `div.nabidka_N` (jedlo, polievka je v `<i>`) a `div.cena`. Alergény sú buď
štruktúrované (`<em title="Obiloviny obsahující lepek">1</em>`), alebo vpísané do textu jedla (`(1, 3)`, `A - 6, 9`).
Ak menicka.cz stránku zmení, beh skončí chybou alebo ukáže varovanie o >80 % jedál bez alergénov:

1. V behu workflowu v sekcii **Artifacts** stiahni `debug-html`.
2. Otvor `page1.html` a uprav selektory v `scraper/menicka.py` (`parse_list_page`, `_parse_item`).
   Alebo súbor pošli Claudovi a on ich upraví.
3. Overenie lokálne bez sťahovania: `python -m scraper --html debug/page1.html --out /tmp/out`

## Lokálne spustenie

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m scraper                      # zapíše site/data/today.json, stats.json, history/
python -m http.server -d site 8000     # http://localhost:8000
python -m unittest discover -s tests -t .
```

## Ďalšie nápady

- Recenzie z Google Places API (zmienky o celiakii, „vlastná fritéza“) ako ďalší signál
- Mapa (Leaflet) s geokódovanými adresami
- Telegram notifikácia „dnes bez lepku“
