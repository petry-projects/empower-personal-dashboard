"""
test_beancount_ledger_load.py — Ledger-load validation for Beancount reconstruction.

Every follow-up deferred from PR #46 (issue #48) requires validating the
generated ledger against a *real* Beancount loader rather than string matching,
so these tests load each export through ``beancount.loader.load_string`` and
assert the ledger parses and balances with zero errors. All data is synthetic
and PII-free.
"""

import tempfile
import unittest
from pathlib import Path

try:
    from beancount import loader
    _HAS_BEANCOUNT = True
except ImportError:  # pragma: no cover - exercised on Python 3.9 CI leg
    # ``beancount>=3.0`` is gated to Python >= 3.10 in the test extra (its early
    # 3.0.x releases dropped 3.9), so on 3.9 the real loader is unavailable and
    # these validation tests are skipped rather than erroring at import time.
    loader = None
    _HAS_BEANCOUNT = False

from empower_personal_dashboard.beancount import BeancountGenerator
from empower_personal_dashboard.exceptions import LedgerAppendError
from empower_personal_dashboard.models import (
    DashboardBalances,
    DashboardHoldings,
    DashboardTransactions,
)


def assert_ledger_loads(testcase, content):
    """Load a single-file ledger string and fail the test on any Beancount error."""
    entries, errors, _options = loader.load_string(content)
    if errors:
        rendered = "\n".join(f"  - {e.message}" for e in errors)
        testcase.fail(f"Beancount reported {len(errors)} load error(s):\n{rendered}")
    return entries


def _brokerage_balances(as_of="2024-10-01", balance=20100.0):
    return DashboardBalances(
        as_of_date=as_of,
        net_worth=balance,
        total_cash=0.0,
        total_investment=balance,
        total_card_liabilities=0.0,
        total_loan=0.0,
        total_mortgage=0.0,
        accounts=[
            {
                "account_id": "ACC-BRK-001",
                "account_name": "Taxable Brokerage",
                "firm_name": "Acme Brokerage",
                "account_type": "investment",
                "balance": balance,
                "is_asset": True,
                "currency": "USD",
                "user_account_id": 5001,
            }
        ],
    )


@unittest.skipUnless(_HAS_BEANCOUNT, "requires beancount>=3.0 (Python >= 3.10)")
class TestLedgerLoadRegression(unittest.TestCase):
    """The pre-existing lifecycle ledger must still load clean (loader harness sanity)."""

    def setUp(self):
        self.generator = BeancountGenerator()

    def test_full_lifecycle_single_file_loads(self):
        balances = _brokerage_balances()
        holdings = DashboardHoldings(
            as_of_date="2024-10-01",
            total_value=20100.0,
            holdings=[
                {"user_account_id": 5001, "account_name": "Taxable Brokerage",
                 "firm_name": "Acme Brokerage", "ticker": "VTI",
                 "quantity": 60.0, "price": 280.0, "cost_basis": 12600.0},
            ],
        )
        transactions = DashboardTransactions(
            start_date="2021-01-01", end_date="2024-10-01", total_transactions=1,
            money_in=0.0, money_out=4000.0, net_cashflow=-4000.0,
            transactions=[
                {"user_transaction_id": "tx-1", "account_id": "ACC-BRK-001",
                 "user_account_id": 5001, "account_name": "Taxable Brokerage",
                 "firm_name": "Acme Brokerage", "transaction_date": "2021-06-15",
                 "description": "Buy VTI", "amount": 4000.0, "is_cash_out": True,
                 "transaction_type": "Buy", "investment_type": "Buy",
                 "symbol": "VTI", "price": 200.0, "quantity": 20.0},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ledger.bean"
            self.generator.export_single_file(
                filepath=target, balances=balances, holdings=holdings,
                transactions=transactions, opening_date="2020-01-01",
            )
            assert_ledger_loads(self, target.read_text(encoding="utf-8"))


@unittest.skipUnless(_HAS_BEANCOUNT, "requires beancount>=3.0 (Python >= 3.10)")
class TestShortPositions(unittest.TestCase):
    """Item 1 — negative snapshot quantities preserved end to end."""

    def setUp(self):
        self.generator = BeancountGenerator()

    def test_current_short_emits_negative_lot_and_assertion_and_loads(self):
        # A currently-held short: the snapshot reports a negative quantity and no
        # offsetting trades. It must survive aggregation, produce a negative
        # opening lot, and a negative unit balance assertion that loads.
        holdings = DashboardHoldings(
            as_of_date="2024-10-01",
            total_value=-1200.0,
            holdings=[
                {"user_account_id": 5001, "account_name": "Taxable Brokerage",
                 "firm_name": "Acme Brokerage", "ticker": "GE",
                 "quantity": -10.0, "price": 12.0},
            ],
        )
        balances = _brokerage_balances(balance=-1200.0)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "short.bean"
            self.generator.export_single_file(
                filepath=target, balances=balances, holdings=holdings,
            )
            content = target.read_text(encoding="utf-8")
        self.assertIn("-10.000000 GE", content)
        self.assertRegex(content, r"balance\s+Assets:AcmeBrokerage:TaxableBrokerage\s+-10\.000000 GE")
        assert_ledger_loads(self, content)

    def test_current_short_distinct_from_fully_sold_security(self):
        # SHORTCO is a current short (in the snapshot, negative) while SOLDCO is a
        # pre-window long sold to zero (absent from the snapshot, net-sold). The
        # short yields a negative opening lot; the fully-sold yields a positive
        # reconstructed opening lot.
        holdings = DashboardHoldings(
            as_of_date="2024-10-01", total_value=-600.0,
            holdings=[
                {"user_account_id": 5001, "account_name": "Taxable Brokerage",
                 "firm_name": "Acme Brokerage", "ticker": "SHORTCO",
                 "quantity": -5.0, "price": 12.0},
            ],
        )
        transactions = DashboardTransactions(
            start_date="2024-01-01", end_date="2024-10-01", total_transactions=1,
            money_in=1000.0, money_out=0.0, net_cashflow=1000.0,
            transactions=[
                {"user_transaction_id": "tx-sold", "account_id": "ACC-BRK-001",
                 "user_account_id": 5001, "account_name": "Taxable Brokerage",
                 "firm_name": "Acme Brokerage", "transaction_date": "2024-05-01",
                 "description": "Sell SOLDCO", "amount": 1000.0, "is_cash_in": True,
                 "is_credit": True, "transaction_type": "Sell",
                 "investment_type": "Sell", "symbol": "SOLDCO",
                 "price": 100.0, "quantity": 10.0},
            ],
        )
        balances = _brokerage_balances(balance=-600.0)
        holdings_out = self.generator.generate_holdings_bean(
            holdings=holdings, balances=balances, transactions=transactions,
        )
        self.assertIn('"SHORTCO Position"', holdings_out)
        self.assertRegex(holdings_out, r"-5\.000000 SHORTCO")
        self.assertIn('"SOLDCO Opening Position"', holdings_out)
        self.assertIn("10.000000 SOLDCO", holdings_out)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mix.bean"
            self.generator.export_single_file(
                filepath=target, balances=balances, holdings=holdings,
                transactions=transactions,
            )
            assert_ledger_loads(self, target.read_text(encoding="utf-8"))


@unittest.skipUnless(_HAS_BEANCOUNT, "requires beancount>=3.0 (Python >= 3.10)")
class TestDeficitFloorShortfall(unittest.TestCase):
    """Item 4 — a deficit-floored opening lot must still match the asserted snapshot."""

    def setUp(self):
        self.generator = BeancountGenerator()

    def test_deficit_floor_reconciles_to_snapshot_and_loads(self):
        # Snapshot holds 5 GE. The window sells 10 then buys 10 (net 0), so the
        # running deficit (10) floors the opening lot to 10 — which would leave a
        # final quantity of 10, contradicting the 5-share snapshot assertion.
        # An explicit shortfall reconciliation must bring the final to 5.
        holdings = DashboardHoldings(
            as_of_date="2024-10-01", total_value=60.0,
            holdings=[
                {"account_name": "Taxable Brokerage", "firm_name": "Acme Brokerage",
                 "ticker": "GE", "quantity": 5.0, "price": 12.0},
            ],
        )
        churn = DashboardTransactions(
            start_date="2024-01-01", end_date="2024-10-01", total_transactions=2,
            money_in=0.0, money_out=0.0, net_cashflow=0.0,
            transactions=[
                {"account_name": "Taxable Brokerage", "firm_name": "Acme Brokerage",
                 "account_type": "investment", "transaction_date": "2024-03-01",
                 "description": "Sell GE", "amount": 100.0, "is_cash_in": True,
                 "is_credit": True, "transaction_type": "Sell", "symbol": "GE",
                 "price": 10.0, "quantity": 10.0},
                {"account_name": "Taxable Brokerage", "firm_name": "Acme Brokerage",
                 "account_type": "investment", "transaction_date": "2024-08-01",
                 "description": "Buy GE", "amount": 120.0, "is_cash_out": True,
                 "transaction_type": "Buy", "symbol": "GE",
                 "price": 12.0, "quantity": 10.0},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deficit.bean"
            self.generator.export_single_file(
                filepath=target, holdings=holdings, transactions=churn,
            )
            content = target.read_text(encoding="utf-8")
        # The floored opening lot of 10 is still emitted...
        self.assertIn("10.000000 GE {", content)
        # ...and the ledger loads: the shortfall reconciliation makes the final
        # quantity equal the 5-share snapshot assertion.
        assert_ledger_loads(self, content)


@unittest.skipUnless(_HAS_BEANCOUNT, "requires beancount>=3.0 (Python >= 3.10)")
class TestBackwardAppend(unittest.TestCase):
    """Item 3 — an append that broadens the window into the past is rejected."""

    def setUp(self):
        self.generator = BeancountGenerator()
        self.balances = _brokerage_balances()
        self.holdings = DashboardHoldings(
            as_of_date="2024-10-01", total_value=16800.0,
            holdings=[
                {"user_account_id": 5001, "account_name": "Taxable Brokerage",
                 "firm_name": "Acme Brokerage", "ticker": "VTI",
                 "quantity": 60.0, "price": 280.0, "cost_basis": 12600.0},
            ],
        )

    def _txns(self, start, end, date):
        return DashboardTransactions(
            start_date=start, end_date=end, total_transactions=1,
            money_in=0.0, money_out=4000.0, net_cashflow=-4000.0,
            transactions=[
                {"user_transaction_id": f"tx-{date}", "account_id": "ACC-BRK-001",
                 "user_account_id": 5001, "account_name": "Taxable Brokerage",
                 "firm_name": "Acme Brokerage", "transaction_date": date,
                 "description": "Buy VTI", "amount": 4000.0, "is_cash_out": True,
                 "transaction_type": "Buy", "investment_type": "Buy",
                 "symbol": "VTI", "price": 200.0, "quantity": 20.0},
            ],
        )

    def test_backward_broadened_append_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ledger"
            self.generator.export_modular_ledger(
                destination_dir=dest, balances=self.balances, holdings=self.holdings,
                transactions=self._txns("2022-01-01", "2024-10-01", "2022-06-15"),
                opening_date="2022-01-01",
            )
            # A second export whose opening lot predates the persisted window start
            # must be rejected rather than silently leaving the stale opening lot.
            with self.assertRaises(LedgerAppendError):
                self.generator.export_modular_ledger(
                    destination_dir=dest, balances=self.balances, holdings=self.holdings,
                    transactions=self._txns("2020-01-01", "2024-10-01", "2020-06-15"),
                    opening_date="2020-01-01",
                )

    def test_forward_append_still_works_and_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ledger"
            self.generator.export_modular_ledger(
                destination_dir=dest, balances=self.balances, holdings=self.holdings,
                transactions=self._txns("2020-01-01", "2024-10-01", "2020-06-15"),
                opening_date="2020-01-01",
            )
            # Re-exporting the same data with an equal window start appends
            # cleanly (the transaction is a duplicate, so no new lot or trade is
            # added) and the ledger still balances.
            self.generator.export_modular_ledger(
                destination_dir=dest, balances=self.balances, holdings=self.holdings,
                transactions=self._txns("2020-01-01", "2024-10-01", "2020-06-15"),
                opening_date="2020-01-01",
            )
            main = (dest / "main.bean").read_text(encoding="utf-8")
            # Resolve includes into one string for the loader.
            combined = []
            for inc in ["accounts.bean", "holdings.bean", "balances.bean",
                        "prices.bean", "transactions.bean"]:
                combined.append((dest / inc).read_text(encoding="utf-8"))
            header = "\n".join(
                line for line in main.splitlines() if not line.startswith("include")
            )
            assert_ledger_loads(self, header + "\n" + "\n".join(combined))


if __name__ == "__main__":
    unittest.main()
