import json
import tempfile
import unittest
from pathlib import Path

from scraper.menicka import classify, main, parse_allergens, parse_list_page

# Výňatok zo skutočnej stránky menicka.cz/brno.html (4 reštaurácie).
FIXTURE = Path(__file__).parent / "fixtures" / "sample.html"


class ParseAllergensTest(unittest.TestCase):
    def test_variants(self):
        cases = {
            "Svíčková, knedlík 1,3,7": ("Svíčková, knedlík", ["1", "3", "7"]),
            "Kuře (7)": ("Kuře", ["7"]),
            "Vývar A: 1a, 3, 9": ("Vývar", ["1a", "3", "9"]),
            "Guláš 1,3 145 Kč": ("Guláš", ["1", "3"]),
            "Halušky s uzeným masem a zelím 1 3": ("Halušky s uzeným masem a zelím", ["1", "3"]),
            "Dhál z červené čočky, rýže A - 6, 9, 10, 12": ("Dhál z červené čočky, rýže", ["6", "9", "10", "12"]),
            "Řízek + pol. (A-1,3,7)": ("Řízek + pol.", ["1", "3", "7"]),
            "Casarecce s ragú +pol. (A-3,7,)": ("Casarecce s ragú +pol.", ["3", "7"]),
            "Losos, brambory A 3, 7": ("Losos, brambory", ["3", "7"]),
            "Hovězí maso s rýží /1a,": ("Hovězí maso s rýží", ["1a"]),
            "Ragú z králíka 1, 1, 3, 7": ("Ragú z králíka", ["1", "3", "7"]),
            "Řízek, salát (1, 3, 7) •": ("Řízek, salát", ["1", "3", "7"]),
            "Kapustová 1": ("Kapustová", ["1"]),
            "Rizoto se zeleninou": ("Rizoto se zeleninou", None),
            "Menu 2": ("Menu 2", None),
            "Pivo 0,5": ("Pivo 0,5", None),
            "Talíř 200g 199": ("Talíř 200g 199", None),
            "Steak House K1": ("Steak House K1", None),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_allergens(text), expected)


class ClassifyTest(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(classify("Bezlepkové těstoviny", ["1"]), "explicit_gf")
        self.assertEqual(classify("Uzené maso, kaše (BL)", None), "explicit_gf")
        self.assertEqual(classify("Vývar", ["1a", "9"]), "gluten")
        self.assertEqual(classify("Kuře", ["7", "10"]), "no_gluten")
        self.assertEqual(classify("Rizoto", None), "unknown")
        self.assertEqual(classify("Polévka blbá", None), "unknown")


class PageTest(unittest.TestCase):
    def setUp(self):
        rs = parse_list_page(FIXTURE.read_bytes(), "https://www.menicka.cz/brno.html")
        self.by_name = {r["name"]: r for r in rs}

    def test_restaurants(self):
        self.assertEqual(list(self.by_name),
                         ["Restaurace Sharingham", "re.SET Caffe", "Restaurace Stavba", "Sushi Noodles"])
        self.assertEqual(self.by_name["Restaurace Sharingham"]["url"],
                         "https://www.menicka.cz/5396-restaurace-sharingham.html")
        self.assertEqual(self.by_name["Sushi Noodles"]["dishes"], [])

    def test_text_allergens(self):
        soup, first = self.by_name["Restaurace Sharingham"]["dishes"][:2]
        self.assertEqual((soup["name"], soup["allergens"], soup["price"], soup["soup"]),
                         ("Hovězí vývar s celestýnskými nudlemi a masem", ["1", "3"], "35 Kč", True))
        self.assertEqual((first["allergens"], first["price"], first["soup"]),
                         (["1", "3", "7", "9", "10"], "179 Kč", False))

    def test_em_allergens(self):
        dishes = self.by_name["re.SET Caffe"]["dishes"]
        self.assertEqual(dishes[0]["allergens"], ["6", "9", "11"])
        self.assertEqual(dishes[0]["status"], "no_gluten")
        self.assertEqual(dishes[1]["status"], "gluten")
        self.assertNotIn("6", dishes[0]["name"])

    def test_explicit_gf(self):
        statuses = [d["status"] for d in self.by_name["Restaurace Stavba"]["dishes"]]
        self.assertEqual(statuses.count("explicit_gf"), 5)
        self.assertEqual(statuses.count("unknown"), 3)

    def test_main_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "data"
            rc = main(["--html", str(FIXTURE), "--out", str(out), "--debug-dir", str(Path(tmp) / "dbg")])
            self.assertEqual(rc, 0)
            today = json.loads((out / "today.json").read_text(encoding="utf-8"))
            self.assertEqual(len(today["restaurants"]), 4)
            stats = json.loads((out / "stats.json").read_text(encoding="utf-8"))
            s = stats["https://www.menicka.cz/4954-restaurace-stavba.html"]
            self.assertEqual((s["days_seen"], s["days_gf"], s["days_explicit"]), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
