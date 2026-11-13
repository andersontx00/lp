"""Unit tests for officially labelled product identifiers."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from product_codes import valid_gtin, identifiers, compare
from product_identity import exact_model_ok

class ProductCodeTests(unittest.TestCase):
    def test_gs1_check_digit(self):
        self.assertTrue(valid_gtin('7891234567895'))
        self.assertTrue(valid_gtin('96385074'))
        self.assertFalse(valid_gtin('7891234567894'))
        self.assertFalse(valid_gtin('123'))

    def test_unlabelled_numbers_not_codes(self):
        self.assertEqual(identifiers('Logitech G502 X 220V RTX 4060')['gtin'], set())

    def test_explicit_gtin(self):
        self.assertEqual(compare('Logitech G502 GTIN: 7891234567895',
                                 'Logitech G502 EAN: 7891234567895')[0], True)
        self.assertEqual(compare('Logitech G502 GTIN: 7891234567895',
                                 'Logitech G502 EAN: 7891234567894'),
                         (False, 'gtin_not_verified'))
        self.assertEqual(compare('Logitech G502 GTIN: 7891234567895',
                                 'Logitech G502 sem codigo'),
                         (False, 'gtin_not_verified'))

    def test_explicit_mpn(self):
        self.assertEqual(compare('MPN: 920-009388', 'MPN 920009388')[0], True)
        self.assertEqual(compare('MPN: 920-009388', 'MPN: 920-009389'),
                         (False, 'mpn_conflict'))

    def test_integration(self):
        self.assertTrue(exact_model_ok('Logitech G502 GTIN: 7891234567895',
                                       'Mouse Logitech G502 EAN: 7891234567895'))
        self.assertFalse(exact_model_ok('Logitech G502 GTIN: 7891234567895',
                                        'Mouse Logitech G502 EAN: 7891234567894'))

if __name__ == '__main__':
    unittest.main()
