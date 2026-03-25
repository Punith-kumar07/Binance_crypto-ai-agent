import unittest
from unittest.mock import MagicMock, patch
from risk.manager import RiskManager, TradeOrder
import config

class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.risk_manager = RiskManager()
        self.snapshot = {
            "pair": "BTCUSDT",
            "current_price": 50000.0,
            "usdt_balance": 100.0,
            "indicators_1h": {"atr_pct": 1.0}
        }
        self.reasoning = {
            "signal_alignment": "strong",
            "direction": "BUY"
        }

    @patch('db.client.get_daily_pnl_pct')
    @patch('db.client.get_open_trades')
    def test_evaluate_buy_approved(self, mock_get_open_trades, mock_get_daily_pnl):
        mock_get_daily_pnl.return_value = 0.0
        mock_get_open_trades.return_value = []
        
        order = self.risk_manager.evaluate("BUY", 80.0, self.snapshot, self.reasoning)
        
        self.assertIsNotNone(order)
        self.assertEqual(order.side, "BUY")
        self.assertEqual(order.pair, "BTCUSDT")
        self.assertTrue(order.usdt_amount > 5.5)

    @patch('db.client.get_daily_pnl_pct')
    def test_evaluate_daily_loss_limit(self, mock_get_daily_pnl):
        mock_get_daily_pnl.return_value = -7.0 # config.MAX_DAILY_LOSS_PCT is -6.0
        
        order = self.risk_manager.evaluate("BUY", 80.0, self.snapshot, self.reasoning)
        
        self.assertIsNone(order)

    @patch('db.client.get_daily_pnl_pct')
    @patch('db.client.get_open_trades')
    def test_evaluate_sell_closes_position(self, mock_get_open_trades, mock_get_daily_pnl):
        mock_get_daily_pnl.return_value = 0.0
        mock_get_open_trades.return_value = [{"id": "123", "quantity": 0.001, "usdt_value": 50.0}]
        
        order = self.risk_manager.evaluate("SELL", 80.0, self.snapshot, self.reasoning)
        
        self.assertIsNotNone(order)
        self.assertEqual(order.side, "SELL")
        self.assertEqual(order.close_position_qty, 0.001)

if __name__ == '__main__':
    unittest.main()
