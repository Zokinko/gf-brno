import json
import tempfile
import unittest
from pathlib import Path

from scraper.menicka import classify, main, parse_allergens, parse_list_page

FIXTURE = Path(__file__).parent / "fixtures" / "sample.html"


class ParseAllergensTest(unittest.TestCase):
    def test_variants(self):
        cases = {
            "Svíčková, knedlík 1,3,7": ("Svíčková, knedlík", ["1", "3", "7"]),
            "Kuře (7)": ("Kuře", ["7"]),
            "Vývar A: 1a, 3, 9": ("Vývar", ["1a", "3", "9"]),
            "Guláš 1,3 145 Kč": ("Guláš", ["1", "3"]),
            "Guláš 1,3 145,- Kč": ("Guláš", ["1", "3"]),
            "Rizoto se zeleninou": ("Rizoto se zeleninou", None),
            "Menu 2": ("Menu 2", None),
            "Pivo 0,5": ("Pivo 0,5", None),
            "Steak 1,3,25": ("Steak 1,3,25", None),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_allergens(text), expected)


class ClassifyTest(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(classify("Bezlepkové těstoviny", ["1"]), "explicit_gf")
        self.assertEqual(classify("Pizza BL", None), "explicit_gf")
        self.assertEqual(classify("Vývar", ["1a", "9"]), "gluten")
        self.assertEqual(classify("Kuře", ["7", "10"]), "no_gluten")
        self.assertEqual(classify("Rizoto", None), "unknown")
        self.assertEqual(classify("Polévka blbá", None), "unknown")


class PageTest(unittest.TestCase):
    def test_fixture(self):
        rs = parse_list_page(FIXTURE.read_bytes(), "https://www.menicka.cz/brno.html")
        self.assertEqual([r["name"] for r in rs], ["Restaurace U Testu", "Bistro Druhé"])
        self.assertEqual(rs[0]["url"], "https://www.menicka.cz/1234-u-testu.html")
        self.assertEqual([d["status"] for d in rs[0]["dishes"]],
                         ["gluten", "no_gluten", "gluten", "explicit_gf", "unknown"])
        self.assertTrue(rs[0]["dishes"][0]["soup"])
        self.assertEqual(rs[0]["dishes"][1]["price"], "159 Kč")
        self.assertEqual(rs[1]["dishes"], [])

    def test_main_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data"
            rc = main(["--html", str(FIXTURE), "--out", str(out), "--debug-dir", str(Path(tmp) / "dbg")])
            self.assertEqual(rc, 0)
            today = json.loads((out / "today.json").read_text(encoding="utf-8"))
            self.assertEqual(len(today["restaurants"]), 2)
            stats = json.loads((out / "stats.json").read_text(encoding="utf-8"))
            s = stats["https://www.menicka.cz/1234-u-testu.html"]
            self.assertEqual((s["days_seen"], s["days_gf"], s["days_explicit"]), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
