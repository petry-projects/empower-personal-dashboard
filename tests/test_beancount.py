"""
test_beancount.py — Hermetic unit tests for Beancount plain-text accounting export engine.
"""

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from empower_personal_dashboard.models import (
    AccountBalance,
    DashboardBalances,
    DashboardHoldings,
    DashboardTransactions,
    InvestmentHolding,
    Transaction,
)
from empower_personal_dashboard.beancount import (
    BeancountGenerator,
    BeancountMapper,
    slugify_account_name,
)

# Standard Beancount account regex
BEANCOUNT_ACCOUNT_REGEX = re.compile(
    r"^(Assets|Liabilities|Equity|Income|Expenses):[A-Z0-9][A-Za-z0-9\-]*"
    r"(:[A-Z0-9][A-Za-z0-9\-]*)*$"
)


class TestBeancountAccountSlugification(unittest.TestCase):
    """Tests for direct firm naming and Beancount account slugification."""

    def test_direct_firm_naming_asset_accounts(self):
        slug = slugify_account_name(
            firm_name="Ally Bank",
            account_name="Interest Checking - 1234",
            account_type="bank",
        )
        self.assertEqual(slug, "Assets:AllyBank:InterestChecking-1234")
        self.assertTrue(BEANCOUNT_ACCOUNT_REGEX.match(slug))

    def test_direct_firm_naming_liability_accounts(self):
        slug = slugify_account_name(
            firm_name="Chase",
            account_name="Freedom Unlimited (...5678)",
            account_type="credit",
        )
        self.assertEqual(slug, "Liabilities:Chase:FreedomUnlimited-5678")
        self.assertTrue(BEANCOUNT_ACCOUNT_REGEX.match(slug))

    def test_mortgage_and_loan_slugification(self):
        slug = slugify_account_name(
            firm_name="Rocket Mortgage",
            account_name="Primary Home Loan",
            account_type="mortgage",
        )
        self.assertEqual(slug, "Liabilities:RocketMortgage:PrimaryHomeLoan")
        self.assertTrue(BEANCOUNT_ACCOUNT_REGEX.match(slug))

    def test_investment_account_slugification(self):
        slug = slugify_account_name(
            firm_name="Vanguard",
            account_name="Taxable Brokerage",
            account_type="investment",
        )
        self.assertEqual(slug, "Assets:Vanguard:TaxableBrokerage")
        self.assertTrue(BEANCOUNT_ACCOUNT_REGEX.match(slug))

    def test_sanitizes_unicode_artifacts_and_special_chars(self):
        slug = slugify_account_name(
            firm_name="Wells Fargo & Co. \ufffd",
            account_name="Way2Save\u2122 Checking ###",
            account_type="bank",
        )
        self.assertEqual(slug, "Assets:WellsFargoCo:Way2SaveChecking")
        self.assertTrue(BEANCOUNT_ACCOUNT_REGEX.match(slug))


class TestBeancountMapper(unittest.TestCase):
    """Tests for YAML mapping, payee regex rules, and category overrides."""

    def setUp(self):
        self.yaml_content = """
accounts:
  "Chase - Freedom Unlimited": "Liabilities:Chase:Freedom"
  "1001": "Assets:Custom:Checking"

categories:
  "Groceries": "Expenses:Food:Groceries"
  "Dining Out": "Expenses:Food:Restaurants"
  "Paycheck": "Income:Job:Salary"
  "Transfer": "Equity:Transfers"

regex_rules:
  - pattern: "(?i)whole foods|trader joe"
    account: "Expenses:Food:Groceries"
  - pattern: "(?i)starbucks"
    account: "Expenses:Food:Coffee"
"""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.map_file = Path(self.temp_dir.name) / "map.yaml"
        self.map_file.write_text(self.yaml_content, encoding="utf-8")
        self.mapper = BeancountMapper(mapping_path=self.map_file)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_account_override_from_yaml(self):
        account = self.mapper.resolve_account(
            firm_name="Chase",
            account_name="Chase - Freedom Unlimited",
            account_id="9999",
            account_type="credit",
        )
        self.assertEqual(account, "Liabilities:Chase:Freedom")

    def test_account_override_by_id(self):
        account = self.mapper.resolve_account(
            firm_name="AnyFirm",
            account_name="Any Name",
            account_id="1001",
            account_type="bank",
        )
        self.assertEqual(account, "Assets:Custom:Checking")

    def test_category_resolution(self):
        acct = self.mapper.resolve_category_or_payee(
            category="Groceries",
            description="Acme Mart",
            is_spending=True,
            is_income=False,
        )
        self.assertEqual(acct, "Expenses:Food:Groceries")

    def test_payee_regex_rule_match(self):
        acct = self.mapper.resolve_category_or_payee(
            category="General Merchandise",
            description="TRADER JOE #1234 AUSTIN TX",
            is_spending=True,
            is_income=False,
        )
        self.assertEqual(acct, "Expenses:Food:Groceries")

    def test_default_fallbacks(self):
        spending = self.mapper.resolve_category_or_payee(
            category=None,
            description="Unknown Shop",
            is_spending=True,
            is_income=False,
        )
        self.assertEqual(spending, "Expenses:Uncategorized")

        income = self.mapper.resolve_category_or_payee(
            category=None,
            description="Mystery Deposit",
            is_spending=False,
            is_income=True,
        )
        self.assertEqual(income, "Income:Uncategorized")

    def test_yaml_mapping_without_pyyaml(self):
        with patch.dict("sys.modules", {"yaml": None}):
            mapper = BeancountMapper(mapping_path=self.map_file)
            acct = mapper.resolve_account("Chase", "Chase - Freedom Unlimited", "9999", "credit")
            self.assertEqual(acct, "Liabilities:Chase:Freedom")
            cat = mapper.resolve_category_or_payee("Groceries", "Any Store", True, False)
            self.assertEqual(cat, "Expenses:Food:Groceries")
            regex_acct = mapper.resolve_category_or_payee("Other", "WHOLE FOODS #1023", True, False)
            self.assertEqual(regex_acct, "Expenses:Food:Groceries")

    def test_yaml_mapping_quoted_key_with_colons(self):
        content = """
accounts:
  "Acme Benefits: Plan A": "Assets:AcmeBenefits:PlanA"
  'Acme Corp: Plan B': 'Assets:AcmeCorp:PlanB'
"""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(content)
            temp_path = f.name

        with patch.dict("sys.modules", {"yaml": None}):
            mapper = BeancountMapper(mapping_path=temp_path)
            self.assertEqual(
                mapper.accounts.get("Acme Benefits: Plan A"),
                "Assets:AcmeBenefits:PlanA",
            )
            self.assertEqual(
                mapper.accounts.get("Acme Corp: Plan B"),
                "Assets:AcmeCorp:PlanB",
            )


class TestBeancountGenerator(unittest.TestCase):
    """Tests for generating compliant Beancount directives."""

    def setUp(self):
        self.generator = BeancountGenerator()

        self.synthetic_balances = DashboardBalances(
            as_of_date="2026-10-01",
            net_worth=15000.0,
            total_cash=5000.0,
            total_investment=12000.0,
            total_card_liabilities=2000.0,
            total_loan=0.0,
            total_mortgage=0.0,
            accounts=[
                {
                    "account_id": "acc-1",
                    "account_name": "Everyday Checking",
                    "firm_name": "Ally Bank",
                    "account_type": "bank",
                    "balance": 5000.0,
                    "is_asset": True,
                    "currency": "USD",
                },
                {
                    "account_id": "acc-2",
                    "account_name": "Sapphire Reserve",
                    "firm_name": "Chase",
                    "account_type": "credit",
                    "balance": 2000.0,
                    "is_asset": False,
                    "currency": "USD",
                },
            ],
        )

        self.synthetic_holdings = DashboardHoldings(
            as_of_date="2026-10-01",
            total_value=12000.0,
            holdings=[
                {
                    "user_account_id": 101,
                    "account_name": "Brokerage",
                    "firm_name": "Vanguard",
                    "ticker": "VTI",
                    "cusip": "922908769",
                    "description": "Vanguard Total Stock Market ETF",
                    "holding_type": "equity",
                    "quantity": 40.0,
                    "price": 280.0,
                    "value": 11200.0,
                    "cost_basis": 8800.0,
                },
                {
                    "user_account_id": 101,
                    "account_name": "Brokerage",
                    "firm_name": "Vanguard",
                    "ticker": "BND",
                    "cusip": "921937835",
                    "description": "Vanguard Total Bond Market ETF",
                    "holding_type": "equity",
                    "quantity": 10.0,
                    "price": 80.0,
                    "value": 800.0,
                    "cost_basis": None,
                },
            ],
        )

        self.synthetic_transactions = DashboardTransactions(
            start_date="2026-09-01",
            end_date="2026-09-30",
            total_transactions=3,
            money_in=3000.0,
            money_out=150.0,
            net_cashflow=2850.0,
            transactions=[
                {
                    "user_transaction_id": "tx-101",
                    "account_id": "acc-2",
                    "account_name": "Sapphire Reserve",
                    "firm_name": "Chase",
                    "account_type": "credit",
                    "transaction_date": "2026-09-15",
                    "description": "WHOLE FOODS MARKET",
                    "original_description": "WHOLE FOODS #1023",
                    "amount": 100.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": True,
                    "transaction_type": "spending",
                    "category_id": 12,
                    "category": "Groceries",
                },
                {
                    "user_transaction_id": "tx-102",
                    "account_id": "acc-1",
                    "account_name": "Everyday Checking",
                    "firm_name": "Ally Bank",
                    "account_type": "bank",
                    "transaction_date": "2026-09-20",
                    "description": "EMPLOYER PAYROLL",
                    "original_description": "DIRECT DEP ACME CORP",
                    "amount": 3000.0,
                    "is_credit": True,
                    "is_cash_in": True,
                    "is_cash_out": False,
                    "is_income": True,
                    "is_spending": False,
                    "transaction_type": "income",
                    "category_id": 1,
                    "category": "Paycheck",
                },
                {
                    "user_transaction_id": "tx-103",
                    "account_id": "acc-1",
                    "account_name": "Everyday Checking",
                    "firm_name": "Ally Bank",
                    "account_type": "bank",
                    "transaction_date": "2026-09-25",
                    "description": "COFFEE SHOP",
                    "original_description": "COFFEE SHOP MAIN ST",
                    "amount": 5.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": True,
                    "transaction_type": "spending",
                    "category_id": 15,
                    "category": "Restaurants",
                },
            ],
        )

    def test_generate_account_open_and_pad_directives(self):
        output = self.generator.generate_accounts_bean(self.synthetic_balances)
        self.assertIn("open Assets:AllyBank:EverydayChecking USD", output)
        self.assertIn("open Liabilities:Chase:SapphireReserve USD", output)
        self.assertIn("open Equity:Opening-Balances USD", output)
        self.assertIn("open Equity:Transfers USD", output)
        self.assertIn("pad Assets:AllyBank:EverydayChecking Equity:Opening-Balances", output)
        self.assertIn("pad Liabilities:Chase:SapphireReserve Equity:Opening-Balances", output)

    def test_generate_balance_assertions(self):
        output = self.generator.generate_balances_bean(
            balances=self.synthetic_balances,
            holdings=self.synthetic_holdings,
        )
        # Cash asset balance is positive
        self.assertIn("2026-10-01 balance Assets:AllyBank:EverydayChecking 5000.00 USD", output)
        # Credit liability balance is asserted as negative in Beancount
        self.assertIn("2026-10-01 balance Liabilities:Chase:SapphireReserve -2000.00 USD", output)
        # Commodity balance assertions are dated the day after the snapshot (D+1)
        # so same-day trades are counted. These positions carry no deficit-floor
        # shortfall reconciliation (no transactions here), so they stay at D+1
        # rather than being pushed to D+2.
        self.assertIn("2026-10-02 balance Assets:Vanguard:Brokerage 40.000000 VTI", output)
        self.assertIn("2026-10-02 balance Assets:Vanguard:Brokerage 10.000000 BND", output)

    def test_generate_prices_bean(self):
        output = self.generator.generate_prices_bean(self.synthetic_holdings)
        self.assertIn("2026-10-01 price VTI 280.0000 USD", output)
        self.assertIn("2026-10-01 price BND 80.0000 USD", output)

    def test_generate_transactions_double_entry_balance(self):
        output = self.generator.generate_transactions_bean(self.synthetic_transactions)

        # Check Whole Foods transaction
        self.assertIn('2026-09-15 * "WHOLE FOODS MARKET" "Groceries"', output)
        self.assertIn('empower_id: "tx-101"', output)
        self.assertIn('^empower-tx-101', output)
        self.assertIn("Liabilities:Chase:SapphireReserve     -100.00 USD", output)
        self.assertIn("Expenses:Uncategorized                 100.00 USD", output)

        # Check Payroll income transaction
        self.assertIn('2026-09-20 * "EMPLOYER PAYROLL" "Paycheck"', output)
        self.assertIn("Assets:AllyBank:EverydayChecking      3000.00 USD", output)
        self.assertIn("Income:Uncategorized                 -3000.00 USD", output)

        # Check Coffee Shop spending
        self.assertIn('2026-09-25 * "COFFEE SHOP" "Restaurants"', output)
        self.assertIn("Assets:AllyBank:EverydayChecking        -5.00 USD", output)
        self.assertIn("Expenses:Uncategorized                   5.00 USD", output)

    def test_generate_modular_directory_export(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "ledger"
            created_files = self.generator.export_modular_ledger(
                destination_dir=out_path,
                balances=self.synthetic_balances,
                holdings=self.synthetic_holdings,
                transactions=self.synthetic_transactions,
            )

            self.assertTrue((out_path / "main.bean").exists())
            self.assertTrue((out_path / "accounts.bean").exists())
            self.assertTrue((out_path / "balances.bean").exists())
            self.assertTrue((out_path / "holdings.bean").exists())
            self.assertTrue((out_path / "prices.bean").exists())
            self.assertTrue((out_path / "transactions.bean").exists())

            main_content = (out_path / "main.bean").read_text(encoding="utf-8")
            self.assertIn('include "accounts.bean"', main_content)
            self.assertIn('include "balances.bean"', main_content)
            self.assertIn('include "holdings.bean"', main_content)
            self.assertIn('include "prices.bean"', main_content)
            self.assertIn('include "transactions.bean"', main_content)

    def test_generate_holdings_lots_with_cost_basis(self):
        output = self.generator.generate_holdings_bean(self.synthetic_holdings)
        # VTI has cost basis 8800.0 using total-cost syntax
        self.assertIn("40.000000 VTI {{8800.000000 USD}}", output)
        # BND has no cost basis -> formatted with @ price
        self.assertIn("10.000000 BND @ 80.0000 USD", output)

    def test_generate_single_file_export(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            single_file = Path(tmp_dir) / "all.bean"
            self.generator.export_single_file(
                filepath=single_file,
                balances=self.synthetic_balances,
                holdings=self.synthetic_holdings,
                transactions=self.synthetic_transactions,
            )
            self.assertTrue(single_file.exists())
            content = single_file.read_text(encoding="utf-8")
            self.assertIn("open Assets:AllyBank:EverydayChecking USD", content)
            self.assertIn("2026-10-01 balance Assets:AllyBank:EverydayChecking 5000.00 USD", content)
            self.assertIn("40.000000 VTI {{8800.000000 USD}}", content)
            self.assertIn("2026-10-01 price VTI 280.0000 USD", content)
            self.assertIn('2026-09-15 * "WHOLE FOODS MARKET"', content)

    def test_export_single_file_refuses_symlinked_target(self):
        # A symlinked .bean target could redirect the write outside the intended
        # path; export_single_file must refuse it before resolving/following it.
        with tempfile.TemporaryDirectory() as tmp_dir:
            real = Path(tmp_dir) / "real.bean"
            real.write_text(";; original\n", encoding="utf-8")
            link = Path(tmp_dir) / "ledger.bean"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                self.generator.export_single_file(
                    filepath=link,
                    balances=self.synthetic_balances,
                    holdings=self.synthetic_holdings,
                    transactions=self.synthetic_transactions,
                )
            # The real target behind the link is left untouched.
            self.assertEqual(real.read_text(encoding="utf-8"), ";; original\n")

    def test_export_single_file_refuses_symlinked_parent_dir(self):
        # A symlinked *parent directory* can redirect the write outside the
        # intended location just as a symlinked leaf can; export_single_file
        # must refuse it before resolving/following the link.
        with tempfile.TemporaryDirectory() as tmp_dir:
            real_dir = Path(tmp_dir) / "real_dir"
            real_dir.mkdir()
            link_dir = Path(tmp_dir) / "link_dir"
            link_dir.symlink_to(real_dir)
            target_inside_link = link_dir / "ledger.bean"
            with self.assertRaises(ValueError):
                self.generator.export_single_file(
                    filepath=target_inside_link,
                    balances=self.synthetic_balances,
                    holdings=self.synthetic_holdings,
                    transactions=self.synthetic_transactions,
                )
            # Nothing was written through the symlinked directory.
            self.assertEqual(list(real_dir.iterdir()), [])

    def test_generate_main_bean_contains_fava_option(self):
        output = self.generator.generate_main_bean()
        self.assertIn('1970-01-01 custom "fava-option" "invert-income-liabilities-equity" "true"', output)
        self.assertIn('1970-01-01 custom "fava-option" "locale" "en_US"', output)
        self.assertIn('option "render_commas" "TRUE"', output)

    def test_generate_holdings_bean_with_opening_date(self):
        output = self.generator.generate_holdings_bean(self.synthetic_holdings, opening_date="2020-01-01")
        self.assertIn('2020-01-01 * "Vanguard Portfolio Snapshot"', output)

    def test_determine_opening_date_logic(self):
        from empower_personal_dashboard.beancount import _determine_opening_date
        # Explicit opening date takes priority
        self.assertEqual(_determine_opening_date(self.synthetic_transactions, opening_date="2019-01-01"), "2019-01-01")
        # With transactions, min("2020-01-01", min_tx) is returned
        self.assertEqual(_determine_opening_date(self.synthetic_transactions), "2020-01-01")
        # With balances only, "2020-01-01" is returned
        self.assertEqual(_determine_opening_date(balances=self.synthetic_balances), "2020-01-01")
        # Pre-2020 history: baseline lot is dated strictly before the earliest trade
        old_txs = DashboardTransactions(
            start_date="2019-03-10",
            end_date="2019-03-10",
            total_transactions=1,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[{"transaction_date": "2019-03-10", "amount": 1.0}],
        )
        self.assertEqual(_determine_opening_date(old_txs), "2019-03-09")
        # Earliest trade exactly on clamp anchor 2020-01-01: yields 2019-12-31
        clamp_anchor_txs = DashboardTransactions(
            start_date="2020-01-01",
            end_date="2020-01-01",
            total_transactions=1,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[{"transaction_date": "2020-01-01", "amount": 1.0}],
        )
        self.assertEqual(_determine_opening_date(clamp_anchor_txs), "2019-12-31")
        # Month-boundary rollover: 2020-03-01 yields 2020-02-29, clamped to 2020-01-01
        month_boundary_txs = DashboardTransactions(
            start_date="2020-03-01",
            end_date="2020-03-01",
            total_transactions=1,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[{"transaction_date": "2020-03-01", "amount": 1.0}],
        )
        self.assertEqual(_determine_opening_date(month_boundary_txs), "2020-01-01")

    def test_credit_card_types_slugify_as_liabilities(self):
        self.assertEqual(slugify_account_name("Chase", "Sapphire", "credit_card"), "Liabilities:Chase:Sapphire")
        self.assertEqual(slugify_account_name("Citi", "DoubleCash", "creditcard"), "Liabilities:Citi:DoubleCash")

    def test_string_escaping_quotes_and_newlines(self):
        tx_data = DashboardTransactions(
            start_date="2026-09-01",
            end_date="2026-09-30",
            total_transactions=1,
            money_in=0.0,
            money_out=50.0,
            net_cashflow=-50.0,
            transactions=[
                {
                    "user_transaction_id": "tx-quote-1",
                    "account_id": "ACC-CHK-001",
                    "account_name": "Checking",
                    "transaction_date": "2026-09-10",
                    "description": 'Bob\'s "Super" Store\nDiscount',
                    "original_description": "BOBS STORE \t DISCOUNT",
                    "amount": 50.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": True,
                    "category": 'Shopping "Retail"',
                }
            ],
        )
        output = self.generator.generate_transactions_bean(tx_data)
        self.assertIn('Bob\'s \\"Super\\" Store Discount', output)
        self.assertIn('Shopping \\"Retail\\"', output)
        self.assertNotIn("\nDiscount", output)

    def test_malformed_and_scalar_yaml_handling(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("just a raw string, not a dict")
            scalar_path = f.name

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("- item1\n- item2\n")
            list_path = f.name

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("accounts: [1, 2, 3]\ncategories: 'scalar'\nregex_rules: ['invalid']\n")
            weird_path = f.name

        # None of these should raise exceptions
        mapper1 = BeancountMapper(mapping_path=scalar_path)
        self.assertEqual(mapper1.accounts, {})
        self.assertEqual(mapper1.categories, {})

        mapper2 = BeancountMapper(mapping_path=list_path)
        self.assertEqual(mapper2.accounts, {})

        mapper3 = BeancountMapper(mapping_path=weird_path)
        self.assertEqual(mapper3.accounts, {})
        self.assertEqual(mapper3.categories, {})
        self.assertEqual(mapper3.regex_rules, [])

    def test_holdings_resolved_from_balances(self):
        # Holdings without firm_name should resolve from balances lookup
        holdings_data = DashboardHoldings(
            as_of_date="2026-10-01",
            total_value=5000.0,
            holdings=[
                {
                    "user_account_id": 1001,
                    "account_name": "Everyday Checking",
                    "ticker": "AAPL",
                    "quantity": 10.0,
                    "price": 200.0,
                    "cost_basis": 1500.0,
                }
            ],
        )
        output = self.generator.generate_holdings_bean(holdings_data, balances=self.synthetic_balances)
        self.assertIn("Assets:AllyBank:EverydayChecking", output)
        self.assertNotIn("Vanguard", output)

    def test_transactions_resolved_from_balances_with_liabilities(self):
        tx_data = DashboardTransactions(
            start_date="2026-09-01",
            end_date="2026-09-30",
            total_transactions=1,
            money_in=0.0,
            money_out=75.0,
            net_cashflow=-75.0,
            transactions=[
                {
                    "user_transaction_id": "tx-credit-99",
                    "account_id": "ACC-CRD-002",
                    "account_name": "Sapphire Reserve",
                    "transaction_date": "2026-09-12",
                    "description": "Restaurant Dining",
                    "amount": 75.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": True,
                }
            ],
        )
        output = self.generator.generate_transactions_bean(tx_data, balances=self.synthetic_balances)
        # Should resolve to Liabilities:Chase:SapphireReserve and charge should be negative
        self.assertIn("Liabilities:Chase:SapphireReserve      -75.00 USD", output)
        self.assertIn("Expenses:Uncategorized                  75.00 USD", output)

    def test_fractional_share_precision(self):
        holdings_data = DashboardHoldings(
            as_of_date="2026-10-01",
            total_value=123.45,
            holdings=[
                {
                    "ticker": "BTC",
                    "quantity": 0.0042,
                    "price": 60000.0,
                    "cost_basis": 210.0,
                    "account_name": "Crypto",
                }
            ],
        )
        output = self.generator.generate_holdings_bean(holdings_data)
        self.assertIn("0.004200 BTC", output)
        # Cost basis 210.0 using total-cost syntax
        self.assertIn("{{210.000000 USD}}", output)

    def test_transfer_routing_to_equity_transfers(self):
        tx_data = DashboardTransactions(
            start_date="2026-09-01",
            end_date="2026-09-30",
            total_transactions=2,
            money_in=500.0,
            money_out=500.0,
            net_cashflow=0.0,
            transactions=[
                {
                    "user_transaction_id": "tx-xfer-1",
                    "account_id": "ACC-CHK-001",
                    "account_name": "Everyday Checking",
                    "transaction_date": "2026-09-05",
                    "description": "Transfer to Savings",
                    "amount": 500.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": False,
                },
                {
                    "user_transaction_id": "tx-xfer-2",
                    "account_id": "ACC-CHK-001",
                    "account_name": "Everyday Checking",
                    "transaction_date": "2026-09-06",
                    "description": "Incoming Account Transfer",
                    "amount": 500.0,
                    "is_credit": True,
                    "is_cash_in": True,
                    "is_cash_out": False,
                    "is_income": False,
                    "is_spending": False,
                },
            ],
        )
        output = self.generator.generate_transactions_bean(tx_data, balances=self.synthetic_balances)
        self.assertIn("Equity:Transfers", output)
        self.assertNotIn("Expenses:Uncategorized", output)

    def test_single_file_export_append_preserves_content(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            single_file = Path(tmp_dir) / "ledger.bean"
            # Write initial version
            self.generator.export_single_file(
                filepath=single_file,
                balances=self.synthetic_balances,
                transactions=self.synthetic_transactions,
            )
            initial_content = single_file.read_text(encoding="utf-8")
            self.assertIn('option "title"', initial_content)
            self.assertIn("tx-101", initial_content)

            # Export with append=True and an additional transaction
            new_tx = DashboardTransactions(
                start_date="2026-10-01",
                end_date="2026-10-02",
                total_transactions=1,
                money_in=0.0,
                money_out=15.0,
                net_cashflow=-15.0,
                transactions=[
                    {
                        "user_transaction_id": "tx-new-999",
                        "account_id": "ACC-CHK-001",
                        "account_name": "Everyday Checking",
                        "transaction_date": "2026-10-02",
                        "description": "New Bakery",
                        "amount": 15.0,
                        "is_credit": False,
                        "is_cash_in": False,
                        "is_cash_out": True,
                        "is_income": False,
                        "is_spending": True,
                    }
                ],
            )
            self.generator.export_single_file(
                filepath=single_file,
                balances=self.synthetic_balances,
                transactions=new_tx,
                append=True,
            )
            appended_content = single_file.read_text(encoding="utf-8")
            # Original title should appear once
            self.assertEqual(appended_content.count('option "title"'), 1)
            # Both old and new transaction should exist
            self.assertIn("tx-101", appended_content)
            self.assertIn("tx-new-999", appended_content)

    def test_selective_export_opens_all_referenced_accounts(self):
        # Even with no balances supplied, open directives are created for transaction accounts
        tx_data = DashboardTransactions(
            start_date="2026-09-01",
            end_date="2026-09-30",
            total_transactions=1,
            money_in=0.0,
            money_out=10.0,
            net_cashflow=-10.0,
            transactions=[
                {
                    "user_transaction_id": "tx-selective-1",
                    "account_id": "ACC-STANDALONE-001",
                    "firm_name": "Standalone Bank",
                    "account_name": "Free Checking",
                    "account_type": "bank",
                    "transaction_date": "2026-09-01",
                    "description": "Snack",
                    "amount": 10.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": True,
                }
            ],
        )
        accounts_bean = self.generator.generate_accounts_bean(balances=None, transactions=tx_data)
        self.assertIn("open Assets:StandaloneBank:FreeChecking USD", accounts_bean)
        # Accounts without balance assertions must NOT have pad directives
        self.assertNotIn("pad Assets:StandaloneBank:FreeChecking", accounts_bean)

    def test_generate_transactions_handles_negative_upstream_amount(self):
        # Even if upstream API sends a negative spending amount, it must be normalized with abs()
        tx_data = DashboardTransactions(
            start_date="2026-09-01",
            end_date="2026-09-30",
            total_transactions=1,
            money_in=0.0,
            money_out=45.50,
            net_cashflow=-45.50,
            transactions=[
                {
                    "user_transaction_id": "tx-neg-1",
                    "account_id": "ACC-NEG-001",
                    "firm_name": "Ally Bank",
                    "account_name": "Everyday Checking",
                    "account_type": "bank",
                    "transaction_date": "2026-09-15",
                    "description": "Coffee Shop",
                    "amount": -45.50,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": True,
                }
            ],
        )
        output = self.generator.generate_transactions_bean(tx_data, balances=self.synthetic_balances)
        self.assertIn("Assets:AllyBank:EverydayChecking       -45.50 USD", output)
        self.assertIn("Expenses:Uncategorized                  45.50 USD", output)

    def test_holdings_lots_aggregation_and_deduplication(self):
        # Multiple lots of the same ticker in the same account are aggregated into one position
        holdings_data = DashboardHoldings(
            as_of_date="2026-10-01",
            total_value=6000.0,
            holdings=[
                {
                    "user_account_id": 1001,
                    "account_name": "Everyday Checking",
                    "ticker": "AAPL",
                    "quantity": 10.0,
                    "price": 200.0,
                    "cost_basis": 1500.0,
                },
                {
                    "user_account_id": 1001,
                    "account_name": "Everyday Checking",
                    "ticker": "AAPL",
                    "quantity": 5.0,
                    "price": 200.0,
                    "cost_basis": 750.0,
                },
            ],
        )
        output = self.generator.generate_holdings_bean(holdings_data, balances=self.synthetic_balances)
        self.assertEqual(output.count("AAPL Position"), 1)
        self.assertIn("15.000000 AAPL {{2250.000000 USD}}", output)
        self.assertIn('empower_holding: "Assets:AllyBank:EverydayChecking:AAPL"', output)

    def test_holdings_lots_skip_existing_on_append(self):
        holdings_data = DashboardHoldings(
            as_of_date="2026-10-01",
            total_value=2000.0,
            holdings=[
                {
                    "user_account_id": 1001,
                    "account_name": "Everyday Checking",
                    "ticker": "AAPL",
                    "quantity": 10.0,
                    "price": 200.0,
                    "cost_basis": 1500.0,
                },
            ],
        )
        existing = '2026-09-30 * "Snapshot" "AAPL Position"\n  empower_holding: "Assets:AllyBank:EverydayChecking:AAPL"\n'
        output = self.generator.generate_holdings_bean(
            holdings_data,
            balances=self.synthetic_balances,
            existing_content=existing,
        )
        # The position should be skipped because it is already in existing_content
        self.assertNotIn("10.000000 AAPL", output)


class TestBeancountInvestmentGrowthReconstruction(unittest.TestCase):
    """Hermetic unit tests for reconstructing portfolio growth curves from investment transactions."""

    def setUp(self):
        self.generator = BeancountGenerator()

        # Multi-Year Investment Life-Cycle Fixture (100% Synthetic, PII-Free)
        # Brokerage Account
        self.balances = DashboardBalances(
            as_of_date="2024-10-01",
            net_worth=21000.0,
            total_cash=0.0,
            total_investment=21000.0,
            total_card_liabilities=0.0,
            total_loan=0.0,
            total_mortgage=0.0,
            accounts=[
                {
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "account_type": "investment",
                    "balance": 21000.0,
                    "is_asset": True,
                    "currency": "USD",
                    "user_account_id": 5001,
                }
            ],
        )

        # Snapshot at Year 4 (2024-10-01): 60 VTI @ $280, 15 AAPL @ $220
        self.holdings_snapshot = DashboardHoldings(
            as_of_date="2024-10-01",
            total_value=20100.0,
            holdings=[
                {
                    "user_account_id": 5001,
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "ticker": "VTI",
                    "quantity": 60.0,
                    "price": 280.0,
                    "cost_basis": 12600.0,  # $210 / share average
                },
                {
                    "user_account_id": 5001,
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "ticker": "AAPL",
                    "quantity": 15.0,
                    "price": 220.0,
                    "cost_basis": 2250.0,  # $150 / share
                },
            ],
        )

        # Multi-Year Investment Transactions:
        # Year 1 (2021-06-15): Buy 20 VTI @ $200 = $4000
        # Year 2 (2022-03-10): Buy 15 AAPL @ $150 = $2250
        # Year 3 (2023-08-20): Sell 10 VTI @ $220 = $2200
        # Year 4 (2024-06-30): Dividend 60 VTI = $45
        self.transactions_history = DashboardTransactions(
            start_date="2021-01-01",
            end_date="2024-10-01",
            total_transactions=4,
            money_in=2245.0,
            money_out=6250.0,
            net_cashflow=-4005.0,
            transactions=[
                {
                    "user_transaction_id": "tx-12345",
                    "account_id": "ACC-BRK-001",
                    "user_account_id": 5001,
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2021-06-15",
                    "description": "Buy VTI",
                    "original_description": "BUY 20 VTI @ 200",
                    "amount": 4000.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": False,
                    "transaction_type": "Buy",
                    "investment_type": "Buy",
                    "symbol": "VTI",
                    "price": 200.0,
                    "quantity": 20.0,
                },
                {
                    "user_transaction_id": "tx-23456",
                    "account_id": "ACC-BRK-001",
                    "user_account_id": 5001,
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2022-03-10",
                    "description": "Buy AAPL",
                    "original_description": "BUY 15 AAPL @ 150",
                    "amount": 2250.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": False,
                    "transaction_type": "Buy",
                    "investment_type": "Buy",
                    "symbol": "AAPL",
                    "price": 150.0,
                    "quantity": 15.0,
                },
                {
                    "user_transaction_id": "tx-67890",
                    "account_id": "ACC-BRK-001",
                    "user_account_id": 5001,
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2023-08-20",
                    "description": "Sell VTI",
                    "original_description": "SELL 10 VTI @ 220",
                    "amount": 2200.0,
                    "is_credit": True,
                    "is_cash_in": True,
                    "is_cash_out": False,
                    "is_income": False,
                    "is_spending": False,
                    "transaction_type": "Sell",
                    "investment_type": "Sell",
                    "symbol": "VTI",
                    "price": 220.0,
                    "quantity": 10.0,
                },
                {
                    "user_transaction_id": "tx-11223",
                    "account_id": "ACC-BRK-001",
                    "user_account_id": 5001,
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2024-06-30",
                    "description": "Dividend VTI",
                    "original_description": "DIVIDEND ON VTI",
                    "amount": 45.0,
                    "is_credit": True,
                    "is_cash_in": True,
                    "is_cash_out": False,
                    "is_income": True,
                    "is_spending": False,
                    "transaction_type": "Dividend Received",
                    "investment_type": "Dividend",
                    "symbol": "VTI",
                    "price": 0.0,
                    "quantity": 0.0,
                },
            ],
        )

    def test_accounts_bean_declares_capital_gains_and_dividends(self):
        output = self.generator.generate_accounts_bean(balances=self.balances, holdings=self.holdings_snapshot)
        self.assertIn("open Income:CapitalGains", output)
        self.assertIn("open Income:Dividends USD", output)

    def test_buy_transaction_directive_emits_commodity_and_cash_legs(self):
        buy_tx = DashboardTransactions(
            start_date="2024-03-15",
            end_date="2024-03-15",
            total_transactions=1,
            money_in=0.0,
            money_out=2205.0,
            net_cashflow=-2205.0,
            transactions=[
                {
                    "user_transaction_id": "12345",
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2024-03-15",
                    "description": "Buy VTI",
                    "amount": 2205.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": False,
                    "transaction_type": "Buy",
                    "symbol": "VTI",
                    "price": 220.50,
                    "quantity": 10.0,
                }
            ],
        )
        output = self.generator.generate_transactions_bean(buy_tx, balances=self.balances)
        self.assertIn('2024-03-15 * "Acme Brokerage" "Buy VTI" ^empower-tx-12345', output)
        self.assertIn("Assets:AcmeBrokerage:TaxableBrokerage  10.000000 VTI {{2205.000000 USD}}", output)
        self.assertIn("Assets:AcmeBrokerage:TaxableBrokerage -2205.00 USD", output)
        self.assertNotIn("Expenses:Uncategorized", output)

    def test_sell_transaction_directive_emits_disposal_and_capital_gains(self):
        sell_tx = DashboardTransactions(
            start_date="2025-06-20",
            end_date="2025-06-20",
            total_transactions=1,
            money_in=1250.0,
            money_out=0.0,
            net_cashflow=1250.0,
            transactions=[
                {
                    "user_transaction_id": "67890",
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2025-06-20",
                    "description": "Sell VTI",
                    "amount": 1250.0,
                    "is_credit": True,
                    "is_cash_in": True,
                    "is_cash_out": False,
                    "is_income": False,
                    "is_spending": False,
                    "transaction_type": "Sell",
                    "symbol": "VTI",
                    "price": 250.0,
                    "quantity": 5.0,
                }
            ],
        )
        output = self.generator.generate_transactions_bean(sell_tx, balances=self.balances)
        self.assertIn('2025-06-20 * "Acme Brokerage" "Sell VTI" ^empower-tx-67890', output)
        self.assertIn("Assets:AcmeBrokerage:TaxableBrokerage -5.000000 VTI {} @ 250.0000 USD", output)
        self.assertIn("Assets:AcmeBrokerage:TaxableBrokerage  1250.00 USD", output)
        self.assertIn("Income:CapitalGains", output)

    def test_dividend_transaction_directive(self):
        div_tx = DashboardTransactions(
            start_date="2024-06-30",
            end_date="2024-06-30",
            total_transactions=1,
            money_in=45.0,
            money_out=0.0,
            net_cashflow=45.0,
            transactions=[
                {
                    "user_transaction_id": "11223",
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2024-06-30",
                    "description": "Dividend VTI",
                    "amount": 45.0,
                    "is_credit": True,
                    "is_cash_in": True,
                    "is_cash_out": False,
                    "is_income": True,
                    "is_spending": False,
                    "transaction_type": "Dividend Received",
                    "symbol": "VTI",
                }
            ],
        )
        output = self.generator.generate_transactions_bean(div_tx, balances=self.balances)
        self.assertIn('2024-06-30 * "Acme Brokerage" "Dividend VTI" ^empower-tx-11223', output)
        self.assertIn("Assets:AcmeBrokerage:TaxableBrokerage    45.00 USD", output)
        self.assertIn("Income:Dividends                       -45.00 USD", output)

    def test_reinvested_dividend_funds_shares_from_dividend_income(self):
        # A reinvestment buys shares funded by dividend income, so it must not
        # drain brokerage cash; the funding leg posts to Income:Dividends.
        reinvest_tx = DashboardTransactions(
            start_date="2024-07-01",
            end_date="2024-07-01",
            total_transactions=1,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[
                {
                    "user_transaction_id": "tx-44556",
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2024-07-01",
                    "description": "Reinvest VTI",
                    "amount": 100.0,
                    "transaction_type": "Reinvest",
                    "investment_type": "Reinvest",
                    "symbol": "VTI",
                    "price": 250.0,
                    "quantity": 0.4,
                }
            ],
        )
        output = self.generator.generate_transactions_bean(reinvest_tx, balances=self.balances)
        self.assertIn("Assets:AcmeBrokerage:TaxableBrokerage   0.400000 VTI {{100.000000 USD}}", output)
        self.assertIn("Income:Dividends                      -100.00 USD", output)
        # Brokerage cash is not drained for a reinvestment.
        self.assertNotIn("TaxableBrokerage  -100.00 USD", output)

    def test_buy_cash_leg_balances_when_amount_bundles_a_fee(self):
        # The reported amount (520) includes a $20 commission; the cash leg
        # preserves the full reported amount (520), and a separate Expenses:Fees
        # posting records the $20 difference between amount and qty × price (500).
        buy_tx = DashboardTransactions(
            start_date="2024-03-15",
            end_date="2024-03-15",
            total_transactions=1,
            money_in=0.0,
            money_out=520.0,
            net_cashflow=-520.0,
            transactions=[
                {
                    "user_transaction_id": "tx-77",
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2024-03-15",
                    "description": "Buy VTI",
                    "amount": 520.0,
                    "transaction_type": "Buy",
                    "investment_type": "Buy",
                    "symbol": "VTI",
                    "price": 250.0,
                    "quantity": 2.0,
                }
            ],
        )
        output = self.generator.generate_transactions_bean(buy_tx, balances=self.balances)
        self.assertIn("Assets:AcmeBrokerage:TaxableBrokerage   2.000000 VTI {{500.000000 USD}}", output)
        self.assertIn("Assets:AcmeBrokerage:TaxableBrokerage  -520.00 USD", output)
        self.assertIn("Expenses:Fees", output)
        self.assertIn("20.00 USD", output)

    def test_buy_derived_price_large_fractional_quantity_balances(self):
        # Price is absent, so it is derived as amount / qty. A large fractional
        # quantity makes a per-unit cost spec drift (qty × rounded-price would
        # not equal the reported amount); the total-cost lot keeps the entry
        # balanced against the full cash outflow with no spurious fee.
        buy_tx = DashboardTransactions(
            start_date="2024-03-15",
            end_date="2024-03-15",
            total_transactions=1,
            money_in=0.0,
            money_out=1000.0,
            net_cashflow=-1000.0,
            transactions=[
                {
                    "user_transaction_id": "tx-frac",
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2024-03-15",
                    "description": "Buy VTI",
                    "amount": 1000.0,
                    "transaction_type": "Buy",
                    "investment_type": "Buy",
                    "symbol": "VTI",
                    "price": 0.0,
                    "quantity": 3.333333,
                }
            ],
        )
        output = self.generator.generate_transactions_bean(buy_tx, balances=self.balances)
        # Lot is booked at the exact reported total so it balances the cash leg.
        self.assertIn("3.333333 VTI {{1000.000000 USD}}", output)
        self.assertIn("Assets:AcmeBrokerage:TaxableBrokerage -1000.00 USD", output)
        # No fee leg: the full reported amount is the lot cost.
        self.assertNotIn("Expenses:Fees", output)

    def test_banking_dividend_category_alone_is_not_investment_dividend(self):
        # A plain banking transaction categorized "dividend" (no ticker, no
        # investment type) must go through category resolution, not Income:Dividends.
        bank_tx = DashboardTransactions(
            start_date="2024-07-02",
            end_date="2024-07-02",
            total_transactions=1,
            money_in=12.0,
            money_out=0.0,
            net_cashflow=12.0,
            transactions=[
                {
                    "user_transaction_id": "tx-99",
                    "account_id": "acc-chk",
                    "account_name": "Checking",
                    "firm_name": "Ally Bank",
                    "transaction_date": "2024-07-02",
                    "description": "Interest payout",
                    "amount": 12.0,
                    "is_income": True,
                    "category_name": "Dividend",
                }
            ],
        )
        output = self.generator.generate_transactions_bean(bank_tx, balances=self.balances)
        self.assertNotIn("Income:Dividends", output)

    def test_transaction_only_investment_account_opened_without_currency(self):
        # Investment accounts seen only via transactions must be opened without a
        # USD currency constraint so ticker commodity postings are accepted.
        tx_only = DashboardTransactions(
            start_date="2021-06-15",
            end_date="2021-06-15",
            total_transactions=1,
            money_in=0.0,
            money_out=4000.0,
            net_cashflow=-4000.0,
            transactions=[
                {
                    "user_transaction_id": "tx-1",
                    "account_id": "ACC-BRK-009",
                    "account_name": "Growth Brokerage",
                    "firm_name": "Acme Brokerage",
                    "account_type": "investment",
                    "transaction_date": "2021-06-15",
                    "description": "Buy VTI",
                    "amount": 4000.0,
                    "transaction_type": "Buy",
                    "investment_type": "Buy",
                    "symbol": "VTI",
                    "price": 200.0,
                    "quantity": 20.0,
                }
            ],
        )
        accounts_bean = self.generator.generate_accounts_bean(transactions=tx_only)
        self.assertIn("2000-01-01 open Assets:AcmeBrokerage:GrowthBrokerage\n", accounts_bean)
        self.assertNotIn("open Assets:AcmeBrokerage:GrowthBrokerage USD", accounts_bean)

    def test_generated_ledger_headers_set_fifo_booking_method(self):
        main_bean = self.generator.generate_main_bean()
        self.assertIn('option "booking_method" "FIFO"', main_bean)
        with tempfile.TemporaryDirectory() as tmp_dir:
            single = Path(tmp_dir) / "ledger.bean"
            self.generator.export_single_file(
                single,
                balances=self.balances,
                transactions=self.transactions_history,
                append=False,
            )
            self.assertIn('option "booking_method" "FIFO"', single.read_text(encoding="utf-8"))

    def test_reconciliation_baseline_holdings_lots(self):
        # 60 VTI snapshot - (20 buys - 10 sells) = 50 baseline shares
        # 15 AAPL snapshot - (15 buys - 0 sells) = 0 baseline shares
        holdings_output = self.generator.generate_holdings_bean(
            holdings=self.holdings_snapshot,
            balances=self.balances,
            transactions=self.transactions_history,
            opening_date="2020-01-01",
        )

        # Assert VTI opening lot of 50 shares exists
        self.assertIn("50.000000 VTI", holdings_output)
        self.assertIn('2020-01-01 * "Acme Brokerage Portfolio Snapshot" "VTI Position"', holdings_output)

        # Assert AAPL has 0 opening lots in holdings.bean
        self.assertNotIn("AAPL", holdings_output)

    def test_reconciliation_opening_lot_for_fully_sold_security(self):
        # A pre-window GE position is completely sold within the window: it is
        # absent from the holdings snapshot but has negative net buys, so a
        # synthetic opening lot must be reconstructed or the sale would reduce an
        # empty inventory and the historical ledger would fail to load.
        fully_sold = DashboardTransactions(
            start_date="2023-01-01",
            end_date="2024-10-01",
            total_transactions=1,
            money_in=2500.0,
            money_out=0.0,
            net_cashflow=2500.0,
            transactions=[
                {
                    "user_transaction_id": "tx-ge-sell",
                    "account_id": "ACC-BRK-001",
                    "user_account_id": 5001,
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2023-05-01",
                    "description": "Sell GE",
                    "amount": 2500.0,
                    "is_credit": True,
                    "is_cash_in": True,
                    "is_cash_out": False,
                    "transaction_type": "Sell",
                    "investment_type": "Sell",
                    "symbol": "GE",
                    "price": 100.0,
                    "quantity": 25.0,
                }
            ],
        )
        holdings_output = self.generator.generate_holdings_bean(
            holdings=self.holdings_snapshot,  # only VTI + AAPL, no GE
            balances=self.balances,
            transactions=fully_sold,
            opening_date="2020-01-01",
        )
        # 25-share GE opening lot reconstructed at the trade price so the later
        # sale has real inventory to reduce.
        self.assertIn("25.000000 GE {100.000000 USD}", holdings_output)
        self.assertIn('"GE Opening Position"', holdings_output)

    def test_reconciliation_transactions_and_balances_assertions(self):
        tx_output = self.generator.generate_transactions_bean(self.transactions_history, balances=self.balances)
        # AAPL only appears on its genuine trade date: 2022-03-10
        self.assertIn('2022-03-10 * "Acme Brokerage" "Buy AAPL"', tx_output)

        # Final commodity balance assertions are dated the day after the snapshot
        # (2024-10-02) and assert full portfolio snapshot quantities: 60 VTI and 15 AAPL.
        # Neither position is deficit-floored (VTI nets +10 with no intermediate
        # deficit; AAPL's baseline is zero), so no shortfall reconciliation posts
        # and both assertions stay at D+1 rather than D+2.
        bal_output = self.generator.generate_balances_bean(
            balances=self.balances,
            holdings=self.holdings_snapshot,
            transactions=self.transactions_history,
        )
        self.assertIn("2024-10-02 balance Assets:AcmeBrokerage:TaxableBrokerage 60.000000 VTI", bal_output)
        self.assertIn("2024-10-02 balance Assets:AcmeBrokerage:TaxableBrokerage 15.000000 AAPL", bal_output)

    def test_modular_ledger_export_lifecycle_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "ledger"
            created = self.generator.export_modular_ledger(
                destination_dir=dest,
                balances=self.balances,
                holdings=self.holdings_snapshot,
                transactions=self.transactions_history,
                opening_date="2020-01-01",
            )
            self.assertEqual(len(created), 6)

            holdings_bean = (dest / "holdings.bean").read_text(encoding="utf-8")
            # Baseline lot of 50 VTI, 0 AAPL
            self.assertIn("50.000000 VTI", holdings_bean)
            self.assertNotIn("AAPL", holdings_bean)

            tx_bean = (dest / "transactions.bean").read_text(encoding="utf-8")
            self.assertIn("15.000000 AAPL", tx_bean)

            bal_bean = (dest / "balances.bean").read_text(encoding="utf-8")
            self.assertIn("60.000000 VTI", bal_bean)
            self.assertIn("15.000000 AAPL", bal_bean)

            accounts_bean = (dest / "accounts.bean").read_text(encoding="utf-8")
            self.assertIn("open Income:CapitalGains", accounts_bean)
            self.assertIn("open Income:Dividends USD", accounts_bean)

    def test_rejected_backward_append_leaves_ledger_unmodified(self):
        """A backward-broadening append must abort before rewriting any file.

        The window guard is pre-flighted in ``export_modular_ledger`` ahead of
        every write, so a rejected append leaves main.bean / accounts.bean /
        balances.bean byte-for-byte unchanged instead of partially updated.
        """
        from empower_personal_dashboard.exceptions import LedgerAppendError

        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "ledger"
            self.generator.export_modular_ledger(
                destination_dir=dest,
                balances=self.balances,
                holdings=self.holdings_snapshot,
                transactions=self.transactions_history,
                opening_date="2020-01-01",
            )
            before = {
                p.name: p.read_text(encoding="utf-8")
                for p in sorted(dest.glob("*.bean"))
            }
            # A second account the first export never saw: if any write runs
            # before the guard fires, accounts.bean/balances.bean would gain this
            # account, making a partial update observable.
            mutated_balances = DashboardBalances(
                as_of_date="2024-10-01",
                net_worth=26000.0,
                total_cash=5000.0,
                total_investment=21000.0,
                total_card_liabilities=0.0,
                total_loan=0.0,
                total_mortgage=0.0,
                accounts=[
                    self.balances.accounts[0],
                    {
                        "account_id": "ACC-CASH-999",
                        "account_name": "New Cash Reserve",
                        "firm_name": "Zenith Bank",
                        "account_type": "cash",
                        "balance": 5000.0,
                        "is_asset": True,
                        "currency": "USD",
                        "user_account_id": 5099,
                    },
                ],
            )
            # Opening the lot in 2018 broadens the persisted 2020 window into the
            # past, which the guard rejects.
            backward_txns = DashboardTransactions(
                start_date="2018-01-01",
                end_date="2024-10-01",
                total_transactions=1,
                money_in=0.0,
                money_out=4000.0,
                net_cashflow=-4000.0,
                transactions=[
                    {
                        "user_transaction_id": "tx-2018",
                        "account_id": "ACC-BRK-001",
                        "user_account_id": 5001,
                        "account_name": "Taxable Brokerage",
                        "firm_name": "Acme Brokerage",
                        "transaction_date": "2018-06-15",
                        "description": "Buy VTI",
                        "amount": 4000.0,
                        "is_cash_out": True,
                        "transaction_type": "Buy",
                        "investment_type": "Buy",
                        "symbol": "VTI",
                        "price": 200.0,
                        "quantity": 20.0,
                    },
                ],
            )
            with self.assertRaises(LedgerAppendError):
                self.generator.export_modular_ledger(
                    destination_dir=dest,
                    balances=mutated_balances,
                    holdings=self.holdings_snapshot,
                    transactions=backward_txns,
                    opening_date="2018-01-01",
                )
            after = {
                p.name: p.read_text(encoding="utf-8")
                for p in sorted(dest.glob("*.bean"))
            }
            self.assertEqual(before, after)

    def test_single_file_export_lifecycle_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "full_ledger.bean"
            self.generator.export_single_file(
                filepath=target,
                balances=self.balances,
                holdings=self.holdings_snapshot,
                transactions=self.transactions_history,
                opening_date="2020-01-01",
            )
            content = target.read_text(encoding="utf-8")
            self.assertIn("50.000000 VTI", content)
            self.assertIn("60.000000 VTI", content)
            self.assertIn("15.000000 AAPL", content)
            self.assertIn("open Income:CapitalGains", content)
            self.assertIn("open Income:Dividends USD", content)

    def test_double_entry_balance_mathematical_integrity(self):
        # Verify that all Buy transactions have matching lot cost and cash outflow
        buy_tx = DashboardTransactions(
            start_date="2024-03-15",
            end_date="2024-03-15",
            total_transactions=1,
            money_in=0.0,
            money_out=2205.0,
            net_cashflow=-2205.0,
            transactions=[
                {
                    "user_transaction_id": "tx-check-buy",
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2024-03-15",
                    "description": "Buy VTI",
                    "amount": 2205.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "is_spending": False,
                    "transaction_type": "Buy",
                    "symbol": "VTI",
                    "price": 220.50,
                    "quantity": 10.0,
                }
            ],
        )
        output = self.generator.generate_transactions_bean(buy_tx, balances=self.balances)
        # The lot is booked with total-cost syntax {{TOTAL USD}} so the lot cost
        # balances the cash leg exactly: total 2205.00 USD and cash -2205.00 USD
        # sum to 0.00.
        lot_match = re.search(r"([\d\.]+)\s+VTI\s+\{\{([\d\.]+)\s+USD\}\}", output)
        cash_match = re.search(r"\n\s+Assets:\S+\s+(-?[\d\.]+)\s+USD\n", output)
        self.assertIsNotNone(lot_match)
        self.assertIsNotNone(cash_match)
        total_cost = float(lot_match.group(2))
        cash = float(cash_match.group(1))
        self.assertAlmostEqual(total_cost + cash, 0.0, places=2)

    def test_investment_tx_uses_brokerage_fallback_without_balances(self):
        # With no balances lookup, a security buy must resolve to the SAME
        # Brokerage account that holdings reconciliation and the quantity
        # assertion use — not Institution — or the assertion fails to reconcile.
        holdings = DashboardHoldings(
            as_of_date="2024-10-01",
            total_value=5600.0,
            holdings=[
                {
                    "user_account_id": 9001,
                    "ticker": "VTI",
                    "quantity": 20.0,
                    "price": 280.0,
                    "cost_basis": 5600.0,
                }
            ],
        )
        txns = DashboardTransactions(
            start_date="2024-01-01",
            end_date="2024-10-01",
            total_transactions=1,
            money_in=0.0,
            money_out=5600.0,
            net_cashflow=-5600.0,
            transactions=[
                {
                    "user_transaction_id": "tx-1",
                    "account_id": "9001",
                    "user_account_id": 9001,
                    "transaction_date": "2024-05-01",
                    "description": "Buy VTI",
                    "amount": 5600.0,
                    "transaction_type": "Buy",
                    "investment_type": "Buy",
                    "symbol": "VTI",
                    "price": 280.0,
                    "quantity": 20.0,
                }
            ],
        )
        tx_out = self.generator.generate_transactions_bean(txns)
        acct_out = self.generator.generate_accounts_bean(transactions=txns, holdings=holdings)
        bal_out = self.generator.generate_balances_bean(holdings=holdings, transactions=txns)

        self.assertIn("Assets:Brokerage", tx_out)
        self.assertNotIn("Assets:Institution", tx_out)
        # The commodity assertion account must match the buy posting + open.
        m = re.search(r"(Assets:Brokerage\S*)\s+20\.0+\s+VTI", bal_out)
        self.assertIsNotNone(m)
        self.assertIn(m.group(1), tx_out)
        self.assertIn(m.group(1), acct_out)

    def test_generate_holdings_bean_positional_compat(self):
        # Legacy positional API: (holdings, balances, existing_content, opening_date).
        # The third positional must still bind to existing_content, not transactions.
        existing = (
            '2020-01-01 * "x" "y"\n'
            '  empower_holding: "Assets:AcmeBrokerage:TaxableBrokerage:VTI"\n'
        )
        output = self.generator.generate_holdings_bean(
            self.holdings_snapshot, self.balances, existing, "2020-01-01"
        )
        # VTI is present in existing_content, so its snapshot lot is skipped.
        self.assertNotIn('"VTI Position"', output)

    def test_generate_holdings_bean_transactions_is_keyword_only(self):
        # transactions is keyword-only, so a legacy caller cannot accidentally
        # pass it positionally in the old existing_content/opening_date slots.
        with self.assertRaises(TypeError):
            self.generator.generate_holdings_bean(
                self.holdings_snapshot,
                self.balances,
                None,
                "2020-01-01",
                self.transactions_history,
            )

    def test_modular_append_injects_fifo_into_legacy_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "ledger"
            dest.mkdir()
            legacy_main = (
                'option "title" "Legacy"\n'
                'option "operating_currency" "USD"\n\n'
                'plugin "beancount.plugins.auto_accounts"\n'
                'include "transactions.bean"\n'
            )
            (dest / "main.bean").write_text(legacy_main, encoding="utf-8")

            self.generator.export_modular_ledger(
                dest, transactions=self.transactions_history, append=True
            )
            updated = (dest / "main.bean").read_text(encoding="utf-8")
            self.assertEqual(updated.count('option "booking_method" "FIFO"'), 1)
            self.assertIn('include "holdings.bean"', updated)

    def test_single_file_append_injects_fifo_into_legacy_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ledger.bean"
            legacy = (
                'option "title" "Legacy"\n'
                'option "operating_currency" "USD"\n\n'
                "2020-01-01 open Assets:Foo USD\n"
            )
            target.write_text(legacy, encoding="utf-8")

            self.generator.export_single_file(
                target, transactions=self.transactions_history, append=True
            )
            updated = target.read_text(encoding="utf-8")
            self.assertEqual(updated.count('option "booking_method" "FIFO"'), 1)

    def test_single_file_append_preserves_existing_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ledger.bean"
            current = (
                'option "operating_currency" "USD"\n'
                'option "booking_method" "FIFO"\n\n'
                "2020-01-01 open Assets:Foo USD\n"
            )
            target.write_text(current, encoding="utf-8")

            self.generator.export_single_file(
                target, transactions=self.transactions_history, append=True
            )
            updated = target.read_text(encoding="utf-8")
            # No duplicate option is injected when one already exists.
            self.assertEqual(updated.count('option "booking_method" "FIFO"'), 1)

    def test_symbol_less_dividend_posts_to_brokerage_without_balances(self):
        # A dividend whose optional symbol is null must still be recognised as an
        # investment and post to the brokerage account, not split into Institution.
        div_tx = DashboardTransactions(
            start_date="2024-06-30",
            end_date="2024-06-30",
            total_transactions=1,
            money_in=45.0,
            money_out=0.0,
            net_cashflow=45.0,
            transactions=[
                {
                    "user_transaction_id": "tx-divnull",
                    "account_name": "Taxable Brokerage",
                    "transaction_date": "2024-06-30",
                    "description": "Dividend",
                    "amount": 45.0,
                    "is_credit": True,
                    "is_cash_in": True,
                    "is_income": True,
                    "investment_type": "Dividend",
                    "symbol": None,
                }
            ],
        )
        output = self.generator.generate_transactions_bean(div_tx)
        self.assertIn("Assets:Brokerage:TaxableBrokerage", output)
        self.assertNotIn("Assets:Institution", output)

    def test_dividend_debit_correction_reverses_posting_signs(self):
        # A dividend correction (not a credit, flagged cash-out) reverses the
        # normal receipt: cash leaves the account and income is reduced.
        corr_tx = DashboardTransactions(
            start_date="2024-06-30",
            end_date="2024-06-30",
            total_transactions=1,
            money_in=0.0,
            money_out=45.0,
            net_cashflow=-45.0,
            transactions=[
                {
                    "user_transaction_id": "tx-divcorr",
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2024-06-30",
                    "description": "Dividend reversal",
                    "amount": 45.0,
                    "is_credit": False,
                    "is_cash_in": False,
                    "is_cash_out": True,
                    "is_income": False,
                    "transaction_type": "Dividend",
                    "symbol": "VTI",
                }
            ],
        )
        output = self.generator.generate_transactions_bean(corr_tx, balances=self.balances)
        # Assert the account and the sign-precise amount independently of the
        # column padding emitted by _emit_investment_dividend, so a cosmetic
        # alignment change does not break a test about amount and direction.
        self.assertRegex(output, r"Assets:AcmeBrokerage:TaxableBrokerage\s+-45\.00 USD")
        self.assertRegex(output, r"Income:Dividends\s+45\.00 USD")

    def test_buy_with_missing_amount_derives_cost_from_quantity_and_price(self):
        # A Buy that omits amount but supplies quantity and price must derive the
        # cash outflow from qty x price instead of booking free shares.
        buy_tx = DashboardTransactions(
            start_date="2024-03-15",
            end_date="2024-03-15",
            total_transactions=1,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[
                {
                    "user_transaction_id": "tx-noamt",
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "transaction_date": "2024-03-15",
                    "description": "Buy VTI",
                    "amount": 0.0,
                    "transaction_type": "Buy",
                    "investment_type": "Buy",
                    "symbol": "VTI",
                    "price": 52.0,
                    "quantity": 10.0,
                }
            ],
        )
        output = self.generator.generate_transactions_bean(buy_tx, balances=self.balances)
        self.assertIn("10.000000 VTI {{520.000000 USD}}", output)
        self.assertIn("Assets:AcmeBrokerage:TaxableBrokerage  -520.00 USD", output)

    def test_reconstructs_opening_lot_for_sell_then_buy_net_zero(self):
        # A sell-10-then-buy-10 sequence nets to zero but the earlier sale needs
        # an opening lot covering the maximum running deficit, or the sale would
        # reduce an empty inventory.
        churn = DashboardTransactions(
            start_date="2024-01-01",
            end_date="2024-12-31",
            total_transactions=2,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[
                {
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "account_type": "investment",
                    "transaction_date": "2024-03-01",
                    "description": "Sell GE",
                    "amount": 100.0,
                    "transaction_type": "Sell",
                    "symbol": "GE",
                    "price": 10.0,
                    "quantity": 10.0,
                },
                {
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "account_type": "investment",
                    "transaction_date": "2024-08-01",
                    "description": "Buy GE",
                    "amount": 120.0,
                    "transaction_type": "Buy",
                    "symbol": "GE",
                    "price": 12.0,
                    "quantity": 10.0,
                },
            ],
        )
        output = self.generator.generate_holdings_bean(None, transactions=churn)
        self.assertIn('"GE Opening Position"', output)
        self.assertIn("10.000000 GE {", output)

    def test_snapshot_lot_without_cost_basis_gets_cost_when_sold(self):
        # A snapshot holding with no cost basis whose ticker is sold in-window
        # must emit a costed opening lot so the empty {} reduction can book.
        holdings = DashboardHoldings(
            as_of_date="2024-10-01",
            total_value=800.0,
            holdings=[
                {
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "ticker": "BND",
                    "quantity": 10.0,
                    "price": 80.0,
                }
            ],
        )
        sale = DashboardTransactions(
            start_date="2024-01-01",
            end_date="2024-10-01",
            total_transactions=1,
            money_in=160.0,
            money_out=0.0,
            net_cashflow=160.0,
            transactions=[
                {
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "account_type": "investment",
                    "transaction_date": "2024-05-01",
                    "description": "Sell BND",
                    "amount": 160.0,
                    "transaction_type": "Sell",
                    "symbol": "BND",
                    "price": 80.0,
                    "quantity": 2.0,
                }
            ],
        )
        output = self.generator.generate_holdings_bean(holdings, transactions=sale)
        # Costed lot ({price USD}), not an uncosted @ price annotation.
        self.assertIn("BND {80.000000 USD}", output)
        self.assertNotIn("BND @ 80.0000 USD", output)

    def test_micro_position_lot_and_assertion_stay_consistent(self):
        # A sub-micro position that still rounds to a nonzero six-decimal quantity
        # must appear in BOTH the opening lot and the balance assertion.
        holdings = DashboardHoldings(
            as_of_date="2024-10-01",
            total_value=1.0,
            holdings=[
                {
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "ticker": "MCR",
                    "quantity": 0.0000008,
                    "price": 100.0,
                    "cost_basis": 0.00008,
                }
            ],
        )
        holdings_output = self.generator.generate_holdings_bean(holdings)
        self.assertIn("0.000001 MCR", holdings_output)
        assertion_lines: list = []
        self.generator._emit_commodity_unit_assertions(holdings, {}, assertion_lines)
        self.assertIn("balance Assets:AcmeBrokerage:TaxableBrokerage 0.000001 MCR", "".join(assertion_lines))

    def test_transactions_only_investment_account_not_asserted_as_cash(self):
        # empower --transactions --beancount fetches balances but not holdings.
        # The investment account's reported balance is portfolio value; it must
        # not be asserted as USD cash (double-counting the generated lots).
        txns = DashboardTransactions(
            start_date="2024-01-01",
            end_date="2024-10-01",
            total_transactions=1,
            money_in=0.0,
            money_out=4000.0,
            net_cashflow=-4000.0,
            transactions=[
                {
                    "account_id": "ACC-BRK-001",
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "account_type": "investment",
                    "transaction_date": "2024-05-01",
                    "description": "Buy VTI",
                    "amount": 4000.0,
                    "transaction_type": "Buy",
                    "investment_type": "Buy",
                    "symbol": "VTI",
                    "price": 200.0,
                    "quantity": 20.0,
                }
            ],
        )
        output = self.generator.generate_balances_bean(
            balances=self.balances, holdings=None, transactions=txns
        )
        self.assertNotIn("balance Assets:AcmeBrokerage:TaxableBrokerage", output)

    def test_reconstructed_opening_lot_prices_from_sale_amount_when_price_null(self):
        # A fully-sold security whose sale omits price but reports amount+quantity
        # must still produce a COSTED opening lot (price derived from amount/qty),
        # so the {} reduction in the sell can book against it.
        sale = DashboardTransactions(
            start_date="2024-01-01",
            end_date="2024-10-01",
            total_transactions=1,
            money_in=250.0,
            money_out=0.0,
            net_cashflow=250.0,
            transactions=[
                {
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "account_type": "investment",
                    "transaction_date": "2024-05-01",
                    "description": "Sell GE",
                    "amount": 250.0,
                    "transaction_type": "Sell",
                    "symbol": "GE",
                    "price": None,
                    "quantity": 10.0,
                }
            ],
        )
        output = self.generator.generate_holdings_bean(None, transactions=sale)
        self.assertIn('"GE Opening Position"', output)
        # Derived price = 250 / 10 = 25.00 => costed lot, not a bare uncosted lot.
        self.assertIn("10.000000 GE {25.000000 USD}", output)

    def test_reconstructed_micro_lot_emitted_at_six_decimal_precision(self):
        # A 0.000001-share sale of a transaction-only security must emit an equally
        # sized opening lot (same six-decimal rounding as the snapshot path), or
        # the sale has no inventory to reduce.
        micro_sale = DashboardTransactions(
            start_date="2024-01-01",
            end_date="2024-10-01",
            total_transactions=1,
            money_in=1.0,
            money_out=0.0,
            net_cashflow=1.0,
            transactions=[
                {
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "account_type": "investment",
                    "transaction_date": "2024-05-01",
                    "description": "Sell MCR",
                    "amount": 1.0,
                    "transaction_type": "Sell",
                    "symbol": "MCR",
                    "price": 100.0,
                    "quantity": 0.000001,
                }
            ],
        )
        output = self.generator.generate_holdings_bean(None, transactions=micro_sale)
        self.assertIn("0.000001 MCR", output)

    def test_snapshot_opening_lot_floored_at_running_deficit(self):
        # A still-held snapshot position can dip below its naive opening quantity
        # mid-window (sell-then-buy). The opening lot must be floored at the deepest
        # intermediate deficit so the earlier sale books, mirroring the reconstructed
        # path. Naive baseline = 5 - net(0) = 5, but the window sells 10 first.
        holdings = DashboardHoldings(
            as_of_date="2024-10-01",
            total_value=60.0,
            holdings=[
                {
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "ticker": "GE",
                    "quantity": 5.0,
                    "price": 12.0,
                }
            ],
        )
        churn = DashboardTransactions(
            start_date="2024-01-01",
            end_date="2024-10-01",
            total_transactions=2,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[
                {
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "account_type": "investment",
                    "transaction_date": "2024-03-01",
                    "description": "Sell GE",
                    "amount": 100.0,
                    "transaction_type": "Sell",
                    "symbol": "GE",
                    "price": 10.0,
                    "quantity": 10.0,
                },
                {
                    "account_name": "Taxable Brokerage",
                    "firm_name": "Acme Brokerage",
                    "account_type": "investment",
                    "transaction_date": "2024-08-01",
                    "description": "Buy GE",
                    "amount": 120.0,
                    "transaction_type": "Buy",
                    "symbol": "GE",
                    "price": 12.0,
                    "quantity": 10.0,
                },
            ],
        )
        output = self.generator.generate_holdings_bean(holdings, transactions=churn)
        self.assertIn("10.000000 GE {", output)

    def test_only_reconciled_assertion_is_delayed_to_two_days(self):
        # GE is deficit-floored (sell 10 then buy 10, net 0, snapshot 5) so it
        # carries a shortfall reconciliation that posts on D+1; its assertion must
        # wait until D+2. VTI nets +20 with no intermediate deficit, so it has no
        # reconciliation and its assertion stays at D+1. Blanket-delaying every
        # assertion to D+2 would make VTI's assertion wrongly absorb a genuine
        # D+1 trade on append (see beancount.py:1079 review).
        holdings = DashboardHoldings(
            as_of_date="2024-10-01",
            total_value=16860.0,
            holdings=[
                {"account_name": "Taxable Brokerage", "firm_name": "Acme Brokerage",
                 "ticker": "GE", "quantity": 5.0, "price": 12.0},
                {"account_name": "Taxable Brokerage", "firm_name": "Acme Brokerage",
                 "ticker": "VTI", "quantity": 60.0, "price": 280.0, "cost_basis": 12600.0},
            ],
        )
        txns = DashboardTransactions(
            start_date="2024-01-01", end_date="2024-10-01", total_transactions=3,
            money_in=100.0, money_out=4120.0, net_cashflow=-4020.0,
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
                {"account_name": "Taxable Brokerage", "firm_name": "Acme Brokerage",
                 "account_type": "investment", "transaction_date": "2021-06-15",
                 "description": "Buy VTI", "amount": 4000.0, "is_cash_out": True,
                 "transaction_type": "Buy", "symbol": "VTI",
                 "price": 200.0, "quantity": 20.0},
            ],
        )
        bal_output = self.generator.generate_balances_bean(holdings=holdings, transactions=txns)
        # Reconciled GE waits for D+2; unreconciled VTI stays at D+1.
        self.assertIn("2024-10-03 balance Assets:AcmeBrokerage:TaxableBrokerage 5.000000 GE", bal_output)
        self.assertIn("2024-10-02 balance Assets:AcmeBrokerage:TaxableBrokerage 60.000000 VTI", bal_output)

    def test_inferred_opening_date_never_precedes_account_opens(self):
        from empower_personal_dashboard.beancount import _determine_opening_date
        pre_2000 = DashboardTransactions(
            start_date="2000-01-01",
            end_date="2000-01-01",
            total_transactions=1,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[{"transaction_date": "2000-01-01", "amount": 1.0}],
        )
        # Earliest trade on 2000-01-01 would subtract to 1999-12-31; clamp to the
        # 2000-01-01 account-open date so the lot never predates the open.
        self.assertEqual(_determine_opening_date(pre_2000), "2000-01-01")
        deep_past = DashboardTransactions(
            start_date="1998-05-05",
            end_date="1998-05-05",
            total_transactions=1,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[{"transaction_date": "1998-05-05", "amount": 1.0}],
        )
        self.assertEqual(_determine_opening_date(deep_past), "2000-01-01")

    def test_append_replaces_incompatible_strict_booking_with_fifo(self):
        from empower_personal_dashboard.beancount import _ensure_fifo_booking_method
        patched = _ensure_fifo_booking_method(
            'option "booking_method" "STRICT"\n2020-01-01 open Assets:Foo\n'
        )
        self.assertIn('option "booking_method" "FIFO"', patched)
        self.assertNotIn("STRICT", patched)
        self.assertEqual(patched.count('option "booking_method"'), 1)






class TestOpeningDateClamp(unittest.TestCase):
    def test_explicit_opening_date_after_trade_is_clamped_before_it(self):
        from empower_personal_dashboard.beancount import _clamp_before_earliest_trade

        txs = DashboardTransactions(
            start_date="2024-01-01",
            end_date="2024-12-31",
            total_transactions=1,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[{"transaction_date": "2024-05-10", "symbol": "VTI", "amount": 100.0}],
        )
        self.assertEqual(_clamp_before_earliest_trade("2025-01-01", txs), "2024-05-09")
        self.assertEqual(_clamp_before_earliest_trade("2020-01-01", txs), "2020-01-01")
        self.assertEqual(_clamp_before_earliest_trade("2025-01-01", None), "2025-01-01")
        old_txs = DashboardTransactions(
            start_date="2000-01-01",
            end_date="2000-01-01",
            total_transactions=1,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[{"transaction_date": "2000-01-01", "symbol": "VTI", "amount": 1.0}],
        )
        # Account-open floor is preserved
        self.assertEqual(_clamp_before_earliest_trade("2000-01-01", old_txs), "2000-01-01")

    def test_transaction_overrides(self):
        mapper = BeancountMapper()
        mapper.transaction_overrides = {
            "tx-12345": "Expenses:Rental:3535BrokenBow:Repairs",
        }
        res = mapper.resolve_category_or_payee(
            category="Home Improvement",
            description="The Home Depot",
            tx_id="tx-12345",
        )
        self.assertEqual(res, "Expenses:Rental:3535BrokenBow:Repairs")

        # Fallback when tx_id not in overrides
        res_default = mapper.resolve_category_or_payee(
            category="Home Improvement",
            description="The Home Depot",
            tx_id="tx-99999",
        )
        self.assertEqual(res_default, "Expenses:Uncategorized")

    def test_modular_extra_bean_includes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir)
            extra_bean = dest / "broken_bow.bean"
            extra_bean.write_text("2026-01-01 * \"Custom\"\n", encoding="utf-8")
            gen = BeancountGenerator()
            main_path = gen._write_modular_main(dest, append=False)
            content = main_path.read_text(encoding="utf-8")
            self.assertIn('include "broken_bow.bean"', content)


class TestWindowStartGuardFallback(unittest.TestCase):
    """The backward-broadening guard must still fire on a marker-less holdings file."""

    def test_markerless_existing_content_uses_earliest_lot_date(self):
        from empower_personal_dashboard.exceptions import LedgerAppendError
        # A holdings.bean body written by an older release (or any append, whose
        # stripped body drops the header marker) carries no window-start marker.
        markerless = (
            '2022-06-15 * "Acme Brokerage Portfolio Snapshot" "VTI Position"\n'
            '  empower_holding: "Assets:AcmeBrokerage:Taxable:VTI"\n'
            "  Assets:AcmeBrokerage:Taxable  20.000000 VTI @ 200.0000 USD\n"
            "  Equity:Opening-Balances\n\n"
        )
        # An append whose opening lot predates the earliest existing dated
        # directive broadens the window into the past and must be rejected.
        with self.assertRaises(LedgerAppendError):
            BeancountGenerator._guard_window_not_broadened(markerless, "2020-01-01")
        # An append on/after the earliest existing date is permitted.
        BeancountGenerator._guard_window_not_broadened(markerless, "2022-06-15")
        BeancountGenerator._guard_window_not_broadened(markerless, "2023-01-01")


if __name__ == "__main__":
    unittest.main()

