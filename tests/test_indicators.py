import unittest
import pandas as pd
import numpy as np
from data.collector import _rsi, _macd, _atr

class TestIndicators(unittest.TestCase):
    def setUp(self):
        # Create dummy price data
        self.prices = pd.Series([100, 101, 102, 101, 100, 99, 98, 99, 100, 101] * 5)

    def test_rsi_calculation(self):
        rsi = _rsi(self.prices)
        self.assertEqual(len(rsi), len(self.prices))
        self.assertTrue(all(0 <= v <= 100 for v in rsi.dropna()))

    def test_macd_calculation(self):
        macd, signal, hist = _macd(self.prices)
        self.assertEqual(len(macd), len(self.prices))
        self.assertEqual(len(signal), len(self.prices))
        self.assertEqual(len(hist), len(self.prices))

    def test_atr_calculation(self):
        high = self.prices + 1
        low = self.prices - 1
        close = self.prices
        atr = _atr(high, low, close)
        self.assertEqual(len(atr), len(self.prices))
        self.assertTrue(all(v >= 0 for v in atr.dropna()))

if __name__ == '__main__':
    unittest.main()
