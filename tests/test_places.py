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
