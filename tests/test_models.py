"""
test_models.py — Unit tests for data models.
"""

import unittest
from empower_personal_dashboard.models import (
    AccountBalance,
    DashboardBalances,
    InvestmentHolding,
    DashboardHoldings,
    Transaction,
    DashboardTransactions,
)


class TestModels(unittest.TestCase):
    def test_account_balance_model(self):
        acct = AccountBalance(
            account_id="ACC-001",
            account_name="Primary Checking",
            firm_name="Apex Bank",
            account_type="BANK",
            balance=15000.50,
        )
        d = acct.to_dict()
        self.assertEqual(d["account_id"], "ACC-001")
        self.assertEqual(d["balance"], 15000.50)
        self.assertTrue(d["is_asset"])

    def test_dashboard_balances_model(self):
        bals = DashboardBalances(
            as_of_date="2026-09-30",
            net_worth=500000.0,
            total_cash=50000.0,
            total_investment=460000.0,
            total_card_liabilities=10000.0,
            total_loan=0.0,
            total_mortgage=0.0,
            accounts=[{"account_name": "Checking", "balance": 50000.0}],
        )
        d = bals.to_dict()
        self.assertEqual(d["net_worth"], 500000.0)
        self.assertEqual(d["accounts_count"], 1)

    def test_holdings_model(self):
        holding = InvestmentHolding(
            user_account_id=101,
            account_name="Retirement 401(k)",
            ticker="VTI",
            cusip="922908769",
            description="Total Stock Market ETF",
            holding_type="ETF",
            quantity=100.0,
            price=275.0,
            value=27500.0,
        )
        d = holding.to_dict()
        self.assertEqual(d["ticker"], "VTI")
        self.assertEqual(d["value"], 27500.0)

        dashboard_holdings = DashboardHoldings(
            as_of_date="2026-09-30",
            total_value=27500.0,
            holdings=[d],
        )
        hd = dashboard_holdings.to_dict()
        self.assertEqual(hd["total_value"], 27500.0)
        self.assertEqual(hd["holdings_count"], 1)

    def test_transaction_model(self):
        tx = Transaction(
            user_transaction_id="TXN-101",
            account_id="ACC-001",
            user_account_id=101,
            account_name="Primary Checking",
            transaction_date="2026-09-15",
            description="Coffee Shop",
            original_description="COFFEE SHOP 123",
            amount=4.75,
            is_credit=False,
            is_cash_in=False,
            is_cash_out=True,
            is_income=False,
            is_spending=True,
            transaction_type="Purchase",
        )
        d = tx.to_dict()
        self.assertEqual(d["amount"], 4.75)
        self.assertEqual(d["description"], "Coffee Shop")

        dt = DashboardTransactions(
            start_date="2026-09-01",
            end_date="2026-09-30",
            total_transactions=1,
            money_in=0.0,
            money_out=4.75,
            net_cashflow=-4.75,
            transactions=[d],
        )
        dtd = dt.to_dict()
        self.assertEqual(dtd["net_cashflow"], -4.75)

    def test_histories_model(self):
        from empower_personal_dashboard.models import DailyHistoryPoint, DashboardHistories

        pt = DailyHistoryPoint(
            date="2024-01-15",
            net_worth=125000.50,
            total_assets=135000.50,
            total_liabilities=10000.00,
            balances={"ACC-INV-001": 100000.0, "ACC-CHK-002": 35000.50, "ACC-CRD-003": -10000.0},
        )
        d = pt.to_dict()
        self.assertEqual(d["date"], "2024-01-15")
        self.assertEqual(d["net_worth"], 125000.50)
        self.assertEqual(d["total_assets"], 135000.50)
        self.assertEqual(d["total_liabilities"], 10000.00)
        self.assertEqual(d["balances"]["ACC-INV-001"], 100000.0)

        histories = DashboardHistories(
            start_date="2024-01-01",
            end_date="2024-01-31",
            histories=[d],
            total_points=1,
            mode="live",
        )
        hd = histories.to_dict()
        self.assertEqual(hd["start_date"], "2024-01-01")
        self.assertEqual(hd["total_points"], 1)
        self.assertEqual(len(hd["histories"]), 1)

        restored = DashboardHistories.from_dict(hd)
        self.assertEqual(restored.start_date, "2024-01-01")
        self.assertEqual(restored.total_points, 1)

    def test_histories_from_dict_defaults_mode_to_schema_valid_live(self):
        from empower_personal_dashboard.models import DashboardHistories

        # When the payload omits mode, the default must be a schema-supported
        # value ("live"/"sandbox_mock"), never the invalid "historical".
        restored = DashboardHistories.from_dict(
            {"start_date": "2024-01-01", "end_date": "2024-01-31", "histories": []}
        )
        self.assertEqual(restored.mode, "live")
        self.assertEqual(restored.to_dict()["mode"], "live")


if __name__ == "__main__":
    unittest.main()
