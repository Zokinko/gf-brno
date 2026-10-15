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

## Nastavenie na GitHube

1. Vytvor repo (napr. `bezlepkove-brno`) a nahraj doň tieto súbory.
2. **Settings → Pages → Build and deployment → Source: GitHub Actions**
3. **Settings → Actions → General → Workflow permissions: Read and write permissions**
4. **Actions → Denné menu → Run workflow**: prvé ručné spustenie.
5. Stránka bude na `https://<tvoj-ucet>.github.io/bezlepkove-brno/`.

Potom to beží samo po–pi ráno. GitHub Pages pre súkromné repo vyžaduje platený plán. Vo verejnom repe sú dáta
(len menu z verejného webu) viditeľné pre každého.

## Keď parser nefunguje

Štruktúra menicka.cz je v kóde **odhadnutá** (bloky `div.menicka_detail`, `.nazev`, `.menicka li`, `.polozka`, `.cena`),
pretože pri písaní nebola stránka dostupná. Ak beh skončí chybou alebo ukáže varovanie o >80 % jedál bez alergénov:

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
