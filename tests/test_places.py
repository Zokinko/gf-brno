import unittest

from bs4 import BeautifulSoup

from scraper import places as P

OSM = {"elements": [
    {"type": "node", "id": 1, "lat": 49.19, "lon": 16.6,
     "tags": {"name": "Bistro Bez", "amenity": "cafe", "website": "bistro-bez.cz",
              "diet:gluten_free": "only", "addr:street": "Česká", "addr:housenumber": "1"}},
    {"type": "way", "id": 2, "center": {"lat": 49.2, "lon": 16.61},
     "tags": {"name": "Hospoda", "amenity": "pub", "contact:website": "https://hospoda.cz/; https://fb.com/x"}},
    {"type": "node", "id": 3, "lat": 49.0, "lon": 16.0, "tags": {"amenity": "restaurant"}},  # bez mena
]}


class FakeResp:
    def __init__(self, body, ctype="text/html", status=200):
        self.body, self.headers, self.status_code = body, {"content-type": ctype}, status
        self.text = body.decode("utf-8", "replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, n):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kw):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return FakeResp(b"User-agent: *\nDisallow: /tajne\n")
        if url not in self.pages:
            return FakeResp(b"", status=404)
        return FakeResp(*self.pages[url]) if isinstance(self.pages[url], tuple) else FakeResp(self.pages[url])


class PlacesTest(unittest.TestCase):
    def test_parse_osm(self):
        ps = P.parse_osm(OSM)
        self.assertEqual([p["id"] for p in ps], ["node/1", "way/2"])
        self.assertEqual(ps[0]["website"], "https://bistro-bez.cz")
        self.assertEqual(ps[0]["address"], "Česká 1")
        self.assertEqual(ps[0]["osm_gf"], "only")
        self.assertEqual(ps[1]["website"], "https://hospoda.cz/")
        self.assertEqual((ps[1]["lat"], ps[1]["lon"]), (49.2, 16.61))

    def test_snippets(self):
        text = "Vítejte. " + "x " * 100 + "Nabízíme bezlepkové pečivo a pizzu bez lepku. " + "y " * 100 + "Celiakie: ptejte se."
        sn = P.find_snippets(text, "u")
        self.assertEqual(len(sn), 2)  # "bezlepkové" a "bez lepku" sú blízko -> jeden úryvok
        self.assertIn("bezlepkové pečivo", sn[0]["text"])
        self.assertIn("Celiakie", sn[1]["text"])
        self.assertEqual(P.find_snippets("Svíčková na smetaně", "u"), [])

    def test_more_keywords(self):
        for t in ["pizza bez glutenu", "Glutenfrei", "pasta senza glutine", "Pizza (BL) 189 Kč", "gluten-free buns"]:
            with self.subTest(t=t):
                self.assertTrue(P.find_snippets(t, "u"))
        self.assertEqual(P.find_snippets("BLT sendvič, bl", "u"), [])

    def test_block_text_and_allergens(self):
        html = b"""<table>
          <tr><td>Ku\xc5\x99e na paprice,\n  t\xc4\x9bstoviny <span>1, 7</span></td><td>159 K\xc4\x8d</td></tr>
          <tr><td>Rizoto se\n zeleninou</td><td><em>7</em> <em>9</em></td></tr>
          </table>
          <p>Losos, brambory</p><p>4</p><p>7</p>
          <p>Steak, hranolky (3, 10)</p><p>Praha 1</p><p>Otev\xc5\x99eno 10 - 22</p>"""
        text, lines, soup = P.extract_text(html, "text/html", "u")
        self.assertIn("Kuře na paprice, těstoviny 1, 7", lines)
        self.assertEqual(len(soup.find_all("span")), 1)  # pôvodný soup ostane nedotknutý (odkazy na menu)
        # Kuře (1,7) / Rizoto (7,9) / Losos (4,7) / Steak (3,10) / "Praha 1" (počíta sa ako s lepkom)
        self.assertEqual(P.scan_allergens(lines), (5, 3))

    def test_dedupe_snippets(self):
        footer = b"<p>Menu</p><footer>Nab\xc3\xadz\xc3\xadme i bezlepkov\xc3\xa9 pe\xc4\x8divo.</footer>"
        site = "https://podnik.cz/"
        session = FakeSession({site: b'<a href="/menu">Menu</a>' + footer, "https://podnik.cz/menu": footer})
        r = P.crawl_site(session, {"website": site}, P.HostLimiter(), {}, P.threading.Lock())
        self.assertEqual(r["pages"], 2)
        self.assertEqual(len(r["snippets"]), 1)

    def test_attach_menicka(self):
        places = [{"name": "Restaurace U Tomana"}, {"name": "Bageterie Boulevard"}, {"name": "Bageterie Boulevard"},
                  {"name": "Café Momo"}]
        stats = {
            "https://www.menicka.cz/1-u-tomana.html": {"name": "U Tomana", "days_seen": 3, "days_gf": 2, "days_explicit": 1},
            "https://www.menicka.cz/2-bb.html": {"name": "Bageterie Boulevard", "days_seen": 1, "days_gf": 1, "days_explicit": 0},
            "https://www.menicka.cz/3-momo.html": {"name": "Kavárna Momo", "days_seen": 1, "days_gf": 0, "days_explicit": 0},
        }
        self.assertEqual(P.attach_menicka(places, stats), 2)
        self.assertEqual(places[0]["menicka"]["url"], "https://www.menicka.cz/1-u-tomana.html")
        self.assertNotIn("menicka", places[1])  # dve pobočky -> nejednoznačné
        self.assertIn("menicka", places[3])     # "Café Momo" == "Kavárna Momo"
        sc, reason = P.score(places[0] | {"web": {}})
        self.assertEqual(sc, 2)
        self.assertIn("1 z 3 dní", reason)

    def test_score_combines_reasons(self):
        sc, reason = P.score({"osm_gf": "limited", "web": {"snippets": [1], "allergen_items": 20,
                                                            "allergen_items_no1": 6}})
        self.assertEqual(sc, 1)
        self.assertEqual(reason.count(";"), 2)

    def test_menu_links(self):
        soup = BeautifulSoup("""
          <a href="/o-nas">O nás</a><a href="/menu">Jídelní lístek</a>
          <a href="https://cdn.other.cz/listek.pdf">PDF</a><a href="https://other.cz/menu">cizí</a>
          <a href="/alergeny">Alergeny</a>""", "html.parser")
        links = P.menu_links(soup, "https://www.podnik.cz/", 5)
        self.assertEqual(links, ["https://www.podnik.cz/alergeny", "https://cdn.other.cz/listek.pdf",
                                 "https://www.podnik.cz/menu"])

    def test_score(self):
        self.assertEqual(P.score({"osm_gf": "only", "web": {}})[0], 3)
        self.assertEqual(P.score({"web": {"snippets": [1, 2]}})[0], 2)
        self.assertEqual(P.score({"web": {"snippets": [1]}})[0], 1)
        self.assertEqual(P.score({"web": {"status": "error"}})[0], 0)

    def test_crawl_site(self):
        site = "https://podnik.cz/"
        session = FakeSession({
            site: b'<a href="/menu">Menu</a><a href="/tajne/menu">tajne</a><p>Vitejte</p>',
            "https://podnik.cz/menu": b"<p>Alergeny: 1,3,7. Na prani pripravime bezlepkove testoviny.</p>",
        })
        r = P.crawl_site(session, {"website": site}, P.HostLimiter(), {}, P.threading.Lock())
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["pages"], 2)
        self.assertTrue(r["allergen_info"])
        self.assertEqual(len(r["snippets"]), 1)
        self.assertNotIn("https://podnik.cz/tajne/menu", session.calls)  # robots.txt

    def test_fetch_osm_falls_back_to_mirror(self):
        class S:
            calls = []

            def post(self, url, **kw):
                self.calls.append(url)
                if url == P.OVERPASS_URLS[0]:
                    return FakeResp(b"Gateway Timeout", status=504)
                r = FakeResp(b"")
                r.json = lambda: OSM
                return r

        s = S()
        self.assertEqual(len(P.fetch_osm(s, rounds=1)), 2)
        self.assertEqual(s.calls, P.OVERPASS_URLS[:2])

    def test_crawl_errors_and_skips(self):
        lock = P.threading.Lock()
        self.assertEqual(P.crawl_site(FakeSession({}), {"website": None}, P.HostLimiter(), {}, lock)["status"],
                         "no_website")
        self.assertEqual(P.crawl_site(FakeSession({}), {"website": "https://www.facebook.com/x"},
                                      P.HostLimiter(), {}, lock)["status"], "skipped")
        self.assertEqual(P.crawl_site(FakeSession({}), {"website": "https://nic.cz/"},
                                      P.HostLimiter(), {}, lock)["status"], "error")


if __name__ == "__main__":
    unittest.main()
