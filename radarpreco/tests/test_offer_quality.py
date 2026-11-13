"""Prevent accessory-only listings from contaminating smartphone/tablet prices."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from offer_quality import device_offer_ok, kind_of, minimum_trusted_at
import server

class OfferQualityTests(unittest.TestCase):
    def test_complete_phone_vs_accessories(self):
        wanted = 'Samsung Galaxy S24 FE 256GB'
        valid = [
            'Smartphone Samsung Galaxy S24 FE 256GB novo',
            'Samsung Galaxy S24 FE 256GB 8GB RAM',
            'Smartphone Samsung Galaxy S24 FE 256GB + Capa de brinde',
        ]
        invalid = [
            'Capa Samsung Galaxy S24 FE 256GB',
            'Capinha de silicone Samsung Galaxy S24 FE 256GB',
            'Kit 3 capinhas para Samsung Galaxy S24 FE 256GB',
            'Película de vidro Samsung Galaxy S24 FE 256GB',
            'Tela Display Samsung Galaxy S24 FE 256GB',
            'Carregador para Smartphone Samsung Galaxy S24 FE 256GB',
            'Samsung Galaxy S24 FE 256GB case anti impacto',
            'Samsung Galaxy S24 FE 256GB caixa vazia',
        ]
        for x in valid:
            with self.subTest(valid=x): self.assertGreaterEqual(server.match_score(wanted, x), .72)
        for x in invalid:
            with self.subTest(invalid=x): self.assertEqual(server.match_score(wanted, x), 0)

    def test_apple_motorola_xiaomi_and_tablets(self):
        for name in ('iPhone 15 128GB','Motorola Razr 50','Google Pixel 9',
                     'Xiaomi 14T','realme C67','Samsung Galaxy Tab S9',
                     'iPad Air M3','Amazon Kindle Paperwhite'):
            with self.subTest(name=name):
                self.assertFalse(device_offer_ok(name,'Capinha para '+name))
                self.assertTrue(minimum_trusted_at(name))

    def test_accessory_search_still_allowed(self):
        self.assertTrue(device_offer_ok('Capa Samsung Galaxy S24 FE','Capa para Samsung Galaxy S24 FE'))
        self.assertIsNone(kind_of('Película iPhone 15'))

    def test_serpapi_rows(self):
        wanted='Samsung Galaxy S24 FE 256GB'
        items={'shopping_results':[
            {'title':'Capinha Samsung Galaxy S24 FE 256GB','extracted_price':29.90,
             'link':'https://shop.example/capinha','source':'A'},
            {'title':'Smartphone Samsung Galaxy S24 FE 256GB novo','extracted_price':2899.00,
             'link':'https://shop.example/aparelho','source':'B'}
        ]}
        got=server.extract_offers(items,wanted)
        self.assertEqual(len(got),1)
        self.assertIn('Smartphone',got[0]['title'])
        self.assertEqual(got[0]['price'],2899.00)

    def test_other_category_unchanged(self):
        self.assertGreaterEqual(server.match_score('Logitech G305','Mouse Logitech G305 LIGHTSPEED'),.72)

if __name__=='__main__':
    unittest.main()
