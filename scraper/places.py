name: Prehľad podnikov (jednorazovo)

on:
  workflow_dispatch:
    inputs:
      limit:
        description: "Len prvých N podnikov (prázdne = všetky)"
        required: false
        default: ""

permissions:
  contents: write
  pages: write
  id-token: write

concurrency:
  group: scrape
  cancel-in-progress: false

jobs:
  places:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - run: pip install -r requirements.txt

      - name: Stiahnuť podniky z OSM a prejsť ich weby
        run: python -m scraper.places --out site/data/places.json ${LIMIT:+--limit $LIMIT}
        env:
          LIMIT: ${{ inputs.limit }}

      - name: Uložiť výsledok ako artifact
        uses: actions/upload-artifact@v4
        with:
          name: places
          path: site/data/places.json
          retention-days: 30

      - name: Commit dát
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898283+github-actions[bot]@users.noreply.github.com"
          git add site/data/places.json
          git diff --cached --quiet || git commit -m "Prehľad podnikov $(date +%F)"
          git pull --rebase
          git push

      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: site

  deploy:
    needs: places
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
