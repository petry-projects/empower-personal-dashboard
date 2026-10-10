"""
test_cli.py — Unit tests for empower CLI.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from empower_personal_dashboard.cli import main as cli_main

class TestCLI(unittest.TestCase):
    def test_cli_sandbox_all_with_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_balances = Path(tmpdir) / "balances.json"
            out_holdings = Path(tmpdir) / "holdings.json"
            out_txs = Path(tmpdir) / "transactions.jsonl"

            argv = [
                "empower",
                "--sandbox",
                "--all",
                "--csv",
                "--output",
                str(out_balances),
                "--output-holdings",
                str(out_holdings),
                "--output-transactions",
                str(out_txs),
                "--format",
                "markdown",
                "--quiet",
            ]

            with patch.object(sys, "argv", argv):
                exit_code = cli_main()
                self.assertEqual(exit_code, 0)

            # Check JSON/JSONL outputs
            self.assertTrue(out_balances.exists())
            self.assertTrue(out_holdings.exists())
            self.assertTrue(out_txs.exists())

            # Check CSV outputs
            self.assertTrue(out_balances.with_suffix(".csv").exists())
            self.assertTrue(out_holdings.with_suffix(".csv").exists())
            self.assertTrue(out_txs.with_suffix(".csv").exists())

            with open(out_balances, "r", encoding="utf-8") as f:
                b_data = json.load(f)
                self.assertEqual(b_data["mode"], "sandbox_mock")
                self.assertGreater(b_data["net_worth"], 0)

    def test_cli_sandbox_json_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_balances = Path(tmpdir) / "balances.json"
            argv = [
                "empower",
                "--sandbox",
                "--balances",
                "--output",
                str(out_balances),
                "--format",
                "json",
                "--quiet",
            ]

            with patch.object(sys, "argv", argv):
                exit_code = cli_main()
                self.assertEqual(exit_code, 0)

            self.assertTrue(out_balances.exists())

    def test_cli_sandbox_beancount_modular_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_ledger = Path(tmpdir) / "ledger"
            argv = [
                "empower",
                "--sandbox",
                "--all",
                "--beancount",
                "--output-beancount",
                str(out_ledger),
                "--quiet",
            ]

            with patch.object(sys, "argv", argv):
                exit_code = cli_main()
                self.assertEqual(exit_code, 0)

            self.assertTrue((out_ledger / "main.bean").exists())
            self.assertTrue((out_ledger / "accounts.bean").exists())
            self.assertTrue((out_ledger / "balances.bean").exists())
            self.assertTrue((out_ledger / "prices.bean").exists())
            self.assertTrue((out_ledger / "transactions.bean").exists())

    def test_cli_sandbox_beancount_single_file_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "export.bean"
            argv = [
                "empower",
                "--sandbox",
                "--all",
                "--beancount",
                "--output-beancount",
                str(out_file),
                "--quiet",
            ]

            with patch.object(sys, "argv", argv):
                exit_code = cli_main()
                self.assertEqual(exit_code, 0)

            self.assertTrue(out_file.exists())
            content = out_file.read_text(encoding="utf-8")
            self.assertIn("option \"title\" \"Empower Personal Dashboard Ledger\"", content)
            self.assertIn("open Equity:Opening-Balances USD", content)

    def test_cli_rejects_conflicting_append_and_overwrite(self):
        argv = [
            "empower",
            "--sandbox",
            "--all",
            "--beancount",
            "--append",
            "--overwrite-ledger",
        ]
        with patch.object(sys, "argv", argv):
            exit_code = cli_main()
            self.assertNotEqual(exit_code, 0)

    def test_cli_beancount_format_routes_progress_to_stderr(self):
        import io
        argv = [
            "empower",
            "--sandbox",
            "--holdings",
            "--format",
            "beancount",
        ]
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch.object(sys, "argv", argv), patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        stdout_val = stdout_buf.getvalue()
        stderr_val = stderr_buf.getvalue()
        # Directives should be on stdout
        self.assertIn("open Equity:Opening-Balances", stdout_val)
        # Progress messages must NOT be on stdout
        self.assertNotIn("[*] Querying Empower", stdout_val)
        # Progress messages must be on stderr
        self.assertIn("[*] Querying Empower", stderr_val)

    def test_cli_holdings_only_beancount_includes_balance_assertions(self):
        import io
        argv = [
            "empower",
            "--sandbox",
            "--holdings",
            "--format",
            "beancount",
        ]
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch.object(sys, "argv", argv), patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        stdout_val = stdout_buf.getvalue()
        # Holdings-only format must include commodity unit balance assertions
        self.assertIn("balance Assets:", stdout_val)

    def test_cli_from_data_dir_beancount_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            data_dir.mkdir()
            balances_file = data_dir / "empower_balances.json"
            holdings_file = data_dir / "empower_holdings.json"
            txs_file = data_dir / "empower_transactions.jsonl"
            out_ledger = Path(tmpdir) / "ledger"

            # Write synthetic offline test datasets
            balances_file.write_text(json.dumps({
                "as_of_date": "2026-10-01",
                "net_worth": 100000.0,
                "total_cash": 25000.0,
                "total_investment": 75000.0,
                "total_credit_card": 0.0,
                "total_loan": 0.0,
                "total_mortgage": 0.0,
                "accounts": [
                    {"account_id": "ACC1", "firm_name": "Test Firm", "account_name": "Checking", "account_type": "bank", "balance": 25000.0, "is_asset": True}
                ],
                "mode": "historical"
            }), encoding="utf-8")

            holdings_file.write_text(json.dumps({
                "as_of_date": "2026-10-01",
                "total_value": 75000.0,
                "holdings": [
                    {"user_account_id": 1, "account_name": "Checking", "ticker": "VTI", "quantity": 100.0, "price": 250.0, "value": 25000.0, "cost_basis": 200.0}
                ],
                "mode": "historical"
            }), encoding="utf-8")

            txs_file.write_text(json.dumps({
                "user_transaction_id": "TX999",
                "account_id": "ACC1",
                "account_name": "Checking",
                "transaction_date": "2026-09-30",
                "description": "Test Paycheck",
                "amount": 5000.0,
                "is_credit": True,
                "is_income": True,
                "is_spending": False,
                "transaction_type": "Deposit",
                "category_id": 1,
            }) + "\n", encoding="utf-8")

            argv = [
                "empower",
                "--all",
                "--from-data-dir",
                str(data_dir),
                "--beancount",
                "--output-beancount",
                str(out_ledger),
                "--quiet",
            ]

            with patch.object(sys, "argv", argv):
                exit_code = cli_main()
                self.assertEqual(exit_code, 0)

            self.assertTrue((out_ledger / "main.bean").exists())
            self.assertTrue((out_ledger / "accounts.bean").exists())
            self.assertTrue((out_ledger / "balances.bean").exists())
            self.assertTrue((out_ledger / "holdings.bean").exists())
            self.assertTrue((out_ledger / "prices.bean").exists())
            self.assertTrue((out_ledger / "transactions.bean").exists())

            tx_content = (out_ledger / "transactions.bean").read_text(encoding="utf-8")
            self.assertIn("Test Paycheck", tx_content)
            self.assertIn("^empower-tx-TX999", tx_content)

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_balances")
    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_holdings")
    def test_cli_all_without_start_date_passes_none_to_client(self, mock_holdings, mock_balances, mock_txs):
        from empower_personal_dashboard.models import DashboardBalances, DashboardHoldings, DashboardTransactions

        mock_balances.return_value = DashboardBalances(
            as_of_date="2026-10-02",
            net_worth=1000.0,
            total_cash=1000.0,
            total_investment=0.0,
            total_card_liabilities=0.0,
            total_loan=0.0,
            total_mortgage=0.0,
            accounts=[],
        )
        mock_holdings.return_value = DashboardHoldings(as_of_date="2026-10-02", total_value=0.0, holdings=[])
        mock_txs.return_value = DashboardTransactions(
            start_date="2018-01-01",
            end_date="2026-10-02",
            total_transactions=0,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[],
        )

        argv = ["empower", "--all", "--quiet", "--session-file", "/nonexistent/session.json", "--mock"]
        with patch.object(sys, "argv", argv):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        mock_txs.assert_called_once()
        _, kwargs = mock_txs.call_args
        self.assertIsNone(kwargs.get("start_date"))

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_cli_transactions_without_start_date_passes_none_to_client(self, mock_txs):
        from empower_personal_dashboard.models import DashboardTransactions

        mock_txs.return_value = DashboardTransactions(
            start_date="2018-01-01",
            end_date="2026-10-02",
            total_transactions=0,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[],
        )

        argv = ["empower", "--transactions", "--quiet", "--session-file", "/nonexistent/session.json", "--mock"]
        with patch.object(sys, "argv", argv):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        mock_txs.assert_called_once()
        _, kwargs = mock_txs.call_args
        self.assertIsNone(kwargs.get("start_date"))

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_cli_transactions_with_explicit_start_date(self, mock_txs):
        from empower_personal_dashboard.models import DashboardTransactions

        mock_txs.return_value = DashboardTransactions(
            start_date="2024-01-01",
            end_date="2026-10-02",
            total_transactions=0,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[],
        )

        argv = [
            "empower",
            "--transactions",
            "--start-date",
            "2024-01-01",
            "--quiet",
            "--session-file",
            "/nonexistent/session.json",
            "--mock",
        ]
        with patch.object(sys, "argv", argv):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        mock_txs.assert_called_once()
        _, kwargs = mock_txs.call_args
        self.assertEqual(kwargs.get("start_date"), "2024-01-01")

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_cli_beancount_format_with_transactions_ignores_limit(self, mock_txs):
        from empower_personal_dashboard.models import DashboardTransactions

        mock_txs.return_value = DashboardTransactions(
            start_date="2024-01-01",
            end_date="2026-10-02",
            total_transactions=0,
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=[],
        )

        argv = [
            "empower",
            "--transactions",
            "--format",
            "beancount",
            "--limit",
            "50",
            "--quiet",
            "--session-file",
            "/nonexistent/session.json",
            "--mock",
        ]
        with patch.object(sys, "argv", argv):
            exit_code = cli_main()
            self.assertEqual(exit_code, 0)

        mock_txs.assert_called_once()
        _, kwargs = mock_txs.call_args
        # When --format beancount is used with --transactions, limit should be None
        # to fetch complete history for accurate Beancount reconstruction
        self.assertIsNone(kwargs.get("limit"))


class TestMergeTransactionRecords(unittest.TestCase):
    def test_updates_in_place_appends_and_preserves_order(self):
        from empower_personal_dashboard.cli import merge_transaction_records

        existing = [
            {"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0, "status": "posted"},
            {"user_transaction_id": "TX2", "transaction_date": "2026-09-01", "description": "Coffee", "amount": 5.0, "status": "pending"},
        ]
        incoming = [
            {"user_transaction_id": "TX2", "transaction_date": "2026-09-01", "description": "Coffee", "amount": 5.25, "status": "posted"},
            {"user_transaction_id": "TX3", "transaction_date": "2026-09-20", "description": "New", "amount": 7.0, "status": "posted"},
        ]

        merged = merge_transaction_records(existing, incoming)

        ids = [m["user_transaction_id"] for m in merged]
        # Stable order: preserved historical first, then original position of TX2, then new TX3
        self.assertEqual(ids, ["OLD1", "TX2", "TX3"])
        # Historical record outside the queried window is preserved untouched
        self.assertEqual(merged[0]["description"], "Old")
        # Matching record updated in place (pending -> posted, revised amount)
        self.assertEqual(merged[1]["status"], "posted")
        self.assertEqual(merged[1]["amount"], 5.25)

    def test_preserves_records_without_id(self):
        from empower_personal_dashboard.cli import merge_transaction_records

        existing = [
            {"transaction_date": "2019-05-01", "description": "No id", "amount": 1.0},
            {"user_transaction_id": "A", "description": "has id", "amount": 2.0},
        ]
        incoming = [
            {"user_transaction_id": "A", "description": "updated", "amount": 3.0},
        ]

        merged = merge_transaction_records(existing, incoming)
        self.assertEqual(len(merged), 2)
        # Keyless historical record survives
        self.assertTrue(any(m.get("description") == "No id" for m in merged))
        # Keyed record updated
        self.assertEqual(next(m for m in merged if m.get("user_transaction_id") == "A")["amount"], 3.0)


class TestCliMerge(unittest.TestCase):
    def _mk_result(self, transactions):
        from empower_personal_dashboard.models import DashboardTransactions

        return DashboardTransactions(
            start_date="2026-09-01",
            end_date="2026-10-01",
            total_transactions=len(transactions),
            money_in=0.0,
            money_out=0.0,
            net_cashflow=0.0,
            transactions=transactions,
        )

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_jsonl_preserves_history_and_applies_updates(self, mock_txs):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            out.write_text(
                json.dumps({"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0, "status": "posted"}) + "\n"
                + json.dumps({"user_transaction_id": "TX2", "transaction_date": "2026-09-01", "description": "Coffee", "amount": 5.0, "status": "pending"}) + "\n",
                encoding="utf-8",
            )
            mock_txs.return_value = self._mk_result([
                {"user_transaction_id": "TX2", "transaction_date": "2026-09-01", "description": "Coffee", "amount": 5.25, "status": "posted"},
                {"user_transaction_id": "TX3", "transaction_date": "2026-09-20", "description": "New", "amount": 7.0, "status": "posted"},
            ])

            argv = [
                "empower", "--transactions", "--merge",
                "--output-transactions", str(out),
                "--quiet", "--session-file", "/nonexistent/session.json", "--mock",
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(cli_main(), 0)

            records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
            by_id = {r["user_transaction_id"]: r for r in records}
            self.assertIn("OLD1", by_id)  # historical outside the window preserved
            self.assertEqual(by_id["TX2"]["status"], "posted")  # updated in place
            self.assertEqual(by_id["TX2"]["amount"], 5.25)
            self.assertIn("TX3", by_id)  # new appended
            self.assertEqual(len(records), 3)

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_tolerates_null_transaction_date(self, mock_txs):
        # A record with an explicit null transaction_date must not crash the merge:
        # the newest-first ordering uses a None-safe key rather than comparing None
        # against str.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            out.write_text(
                json.dumps({"user_transaction_id": "OLD1", "transaction_date": None, "description": "No date", "amount": 10.0}) + "\n"
                + json.dumps({"user_transaction_id": "TX2", "transaction_date": "2026-09-01", "description": "Coffee", "amount": 5.0}) + "\n",
                encoding="utf-8",
            )
            mock_txs.return_value = self._mk_result([
                {"user_transaction_id": "TX3", "transaction_date": None, "description": "New no date", "amount": 7.0},
            ])

            argv = [
                "empower", "--transactions", "--merge",
                "--output-transactions", str(out),
                "--quiet", "--session-file", "/nonexistent/session.json", "--mock",
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(cli_main(), 0)

            records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
            ids = [r["user_transaction_id"] for r in records]
            # Newest-first with a None-safe key: dated records lead, undated ones
            # follow in their merged order (archived OLD1, then newly fetched TX3).
            self.assertEqual(ids, ["TX2", "OLD1", "TX3"])

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_json_output(self, mock_txs):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.json"
            out.write_text(json.dumps({
                "start_date": "2020-01-15",
                "end_date": "2026-09-01",
                "total_transactions": 1,
                "transactions": [
                    {"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0},
                ],
            }), encoding="utf-8")
            mock_txs.return_value = self._mk_result([
                {"user_transaction_id": "TX3", "transaction_date": "2026-09-20", "description": "New", "amount": 7.0},
            ])

            argv = [
                "empower", "--transactions", "--merge",
                "--output-transactions", str(out),
                "--quiet", "--session-file", "/nonexistent/session.json", "--mock",
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(cli_main(), 0)

            data = json.loads(out.read_text(encoding="utf-8"))
            ids = {t["user_transaction_id"] for t in data["transactions"]}
            self.assertEqual(ids, {"OLD1", "TX3"})
            self.assertEqual(data["total_transactions"], 2)

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_auto_detects_earliest_start_date(self, mock_txs):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            out.write_text(
                json.dumps({"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0}) + "\n"
                + json.dumps({"user_transaction_id": "OLD2", "transaction_date": "2021-06-10", "description": "Older", "amount": 20.0}) + "\n",
                encoding="utf-8",
            )
            mock_txs.return_value = self._mk_result([])

            argv = [
                "empower", "--transactions", "--merge",
                "--output-transactions", str(out),
                "--quiet", "--session-file", "/nonexistent/session.json", "--mock",
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(cli_main(), 0)

            mock_txs.assert_called_once()
            _, kwargs = mock_txs.call_args
            # Earliest archived transaction date is queried forward
            self.assertEqual(kwargs.get("start_date"), "2020-01-15")

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_explicit_start_date_overrides_earliest_detection(self, mock_txs):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            out.write_text(
                json.dumps({"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0}) + "\n",
                encoding="utf-8",
            )
            mock_txs.return_value = self._mk_result([])

            argv = [
                "empower", "--transactions", "--merge",
                "--start-date", "2025-01-01",
                "--output-transactions", str(out),
                "--quiet", "--session-file", "/nonexistent/session.json", "--mock",
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(cli_main(), 0)

            _, kwargs = mock_txs.call_args
            self.assertEqual(kwargs.get("start_date"), "2025-01-01")

    def test_earliest_transaction_date_ignores_invalid_dates(self):
        # Only canonical YYYY-MM-DD strings may be selected: non-string, null,
        # missing, and malformed values must never become the merge start date.
        from empower_personal_dashboard.cli import _earliest_transaction_date

        self.assertIsNone(_earliest_transaction_date([]))
        self.assertIsNone(
            _earliest_transaction_date([
                {"transaction_date": None},
                {"transaction_date": 20200115},          # non-string
                {"transaction_date": "not-a-date"},        # malformed
                {"transaction_date": "2020-13-01"},        # out-of-range month
                {"transaction_date": "2020-1-5"},          # non-canonical padding
                {"transaction_date": "2020-01-15T00:00"},  # extra time component
                {},                                        # missing key
            ])
        )
        # A single valid date surrounded by junk is still selected cleanly.
        self.assertEqual(
            _earliest_transaction_date([
                {"transaction_date": 20200101},
                {"transaction_date": "2021-06-10"},
                {"transaction_date": "garbage"},
                {"transaction_date": "2020-07-04"},
            ]),
            "2020-07-04",
        )

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_skips_invalid_archive_date_for_start(self, mock_txs):
        # A malformed/non-string archived date must not be forwarded as start_date;
        # the earliest *valid* canonical date is queried instead.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            out.write_text(
                json.dumps({"user_transaction_id": "BAD", "transaction_date": "garbage", "amount": 1.0}) + "\n"
                + json.dumps({"user_transaction_id": "OLD1", "transaction_date": "2021-06-10", "amount": 10.0}) + "\n",
                encoding="utf-8",
            )
            mock_txs.return_value = self._mk_result([])

            code, err = self._run_merge(out)
            self.assertEqual(code, 0, err)
            _, kwargs = mock_txs.call_args
            self.assertEqual(kwargs.get("start_date"), "2021-06-10")

    def test_archive_lock_is_exclusive(self):
        # The merge holds an exclusive lock on the archive's directory for its
        # whole read/fetch/merge/replace sequence. Locking the directory itself
        # means every process targeting the archive contends on the same lock
        # whatever its TMPDIR, and no lock file is created anywhere.
        from empower_personal_dashboard.cli import _archive_lock

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            try:
                import fcntl
            except ImportError:
                # No advisory locking on this platform: the manager is a no-op.
                with _archive_lock(out):
                    pass
                return

            def try_lock():
                fd = os.open(tmpdir, os.O_RDONLY)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)

            with _archive_lock(out):
                self.assertEqual(list(Path(tmpdir).iterdir()), [])
                with self.assertRaises(BlockingIOError):
                    try_lock()
            # Released on exit: the same lock can be taken again.
            try_lock()

    def test_archive_lock_tolerates_missing_directory(self):
        # A first run into a directory that does not exist yet has no archive to
        # race on; the lock must not fail or create anything.
        from empower_personal_dashboard.cli import _archive_lock

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "not-created-yet" / "transactions.jsonl"
            with _archive_lock(out):
                pass
            self.assertEqual(list(Path(tmpdir).iterdir()), [])

    def _run_merge(self, out, extra_args=()):
        argv = [
            "empower", "--transactions", "--merge",
            "--output-transactions", str(out),
            "--quiet", "--session-file", "/nonexistent/session.json", "--mock",
        ] + list(extra_args)
        stderr = io.StringIO()
        with patch.object(sys, "argv", argv), contextlib.redirect_stderr(stderr):
            code = cli_main()
        return code, stderr.getvalue()

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_output_is_newest_first(self, mock_txs):
        # The fetcher returns newest-first, so a merged archive must stay
        # newest-first: newly fetched rows lead instead of trailing the archive.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            out.write_text(
                json.dumps({"user_transaction_id": "TX2", "transaction_date": "2026-09-01", "description": "Coffee", "amount": 5.0}) + "\n"
                + json.dumps({"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0}) + "\n",
                encoding="utf-8",
            )
            mock_txs.return_value = self._mk_result([
                {"user_transaction_id": "TX3", "transaction_date": "2026-09-20", "description": "New", "amount": 7.0},
                {"user_transaction_id": "TX2", "transaction_date": "2026-09-01", "description": "Coffee", "amount": 5.25},
            ])

            code, _ = self._run_merge(out)
            self.assertEqual(code, 0)

            records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual([r["user_transaction_id"] for r in records], ["TX3", "TX2", "OLD1"])
            self.assertEqual(records[1]["amount"], 5.25)

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_failed_write_leaves_archive_intact(self, mock_txs):
        # A serialization error mid-write must not truncate the archive: the
        # merged output is written to a temp file and only then swapped in.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            original = (
                json.dumps({"user_transaction_id": "TX2", "transaction_date": "2026-09-01", "description": "Coffee", "amount": 5.0}) + "\n"
                + json.dumps({"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0}) + "\n"
            )
            out.write_text(original, encoding="utf-8")
            # A set is not JSON-serializable, so writing the updated TX2 fails.
            mock_txs.return_value = self._mk_result([
                {"user_transaction_id": "TX2", "transaction_date": "2026-09-01", "description": "Coffee", "amount": 5.25, "tags": {"a", "b"}},
            ])

            code, _ = self._run_merge(out)

            self.assertEqual(code, 1)
            self.assertEqual(out.read_text(encoding="utf-8"), original)
            self.assertEqual(sorted(p.name for p in Path(tmpdir).iterdir()), ["transactions.jsonl"])

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_json_write_is_atomic_on_failure(self, mock_txs):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.json"
            original = json.dumps({
                "start_date": "2020-01-15",
                "end_date": "2020-01-15",
                "total_transactions": 1,
                "transactions": [
                    {"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0},
                ],
            })
            out.write_text(original, encoding="utf-8")
            mock_txs.return_value = self._mk_result([
                {"user_transaction_id": "TX3", "transaction_date": "2026-09-20", "description": "New", "amount": 7.0, "tags": {"a"}},
            ])

            code, _ = self._run_merge(out)

            self.assertEqual(code, 1)
            self.assertEqual(out.read_text(encoding="utf-8"), original)
            self.assertEqual(sorted(p.name for p in Path(tmpdir).iterdir()), ["transactions.json"])

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_preserves_existing_file_mode(self, mock_txs):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            out.write_text(
                json.dumps({"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0}) + "\n",
                encoding="utf-8",
            )
            # Owner-only, with an execute bit no umask default can produce, so the
            # assertion below can only pass if the existing mode was carried over.
            os.chmod(out, 0o700)
            mock_txs.return_value = self._mk_result([
                {"user_transaction_id": "TX3", "transaction_date": "2026-09-20", "description": "New", "amount": 7.0},
            ])

            code, _ = self._run_merge(out)

            self.assertEqual(code, 0)
            self.assertEqual(out.stat().st_mode & 0o777, 0o700)

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_new_archive_honours_umask(self, mock_txs):
        # A brand-new archive must get the process umask default, exactly as a
        # plain open() would: a strict umask keeps financial data owner-only.
        mock_txs.return_value = self._mk_result([
            {"user_transaction_id": "TX3", "transaction_date": "2026-09-20", "description": "New", "amount": 7.0},
        ])
        for umask, expected in ((0o077, 0o600), (0o022, 0o644)):
            with self.subTest(umask=oct(umask)), tempfile.TemporaryDirectory() as tmpdir:
                out = Path(tmpdir) / "transactions.jsonl"
                previous = os.umask(umask)
                try:
                    code, _ = self._run_merge(out)
                finally:
                    os.umask(previous)
                self.assertEqual(code, 0)
                self.assertEqual(out.stat().st_mode & 0o777, expected)
                self.assertEqual(sorted(p.name for p in Path(tmpdir).iterdir()), ["transactions.jsonl"])

    def test_existing_archive_mode_applies_before_content_is_written(self):
        # An owner-only archive must never be staged in a wider temp file: the
        # existing mode is applied before any content is written, not after.
        from empower_personal_dashboard.cli import _atomic_write

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            out.write_text("{}\n", encoding="utf-8")
            os.chmod(out, 0o600)
            seen = []

            def write_body(f):
                temp_files = [p for p in Path(tmpdir).iterdir() if p.name != out.name]
                seen.extend(p.stat().st_mode & 0o777 for p in temp_files)
                f.write("{}\n")

            previous = os.umask(0o022)
            try:
                _atomic_write(out, write_body)
            finally:
                os.umask(previous)

            self.assertEqual(seen, [0o600])
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_keeps_records_containing_unicode_line_separators(self, mock_txs):
        # U+2028/U+2029 are legal inside JSON strings and are written raw
        # (ensure_ascii=False). They must not be treated as record boundaries.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            description = "Line one\u2028line two\u2029end"
            out.write_text(
                json.dumps({"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": description, "amount": 10.0}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            mock_txs.return_value = self._mk_result([
                {"user_transaction_id": "TX3", "transaction_date": "2026-09-20", "description": "New", "amount": 7.0},
            ])

            code, err = self._run_merge(out)
            self.assertEqual(code, 0, err)
            # A second merge must be able to read what the first one wrote.
            code, err = self._run_merge(out)
            self.assertEqual(code, 0, err)

            records = [json.loads(line) for line in out.read_text(encoding="utf-8").split("\n") if line.strip()]
            by_id = {r["user_transaction_id"]: r for r in records}
            self.assertEqual(sorted(by_id), ["OLD1", "TX3"])
            self.assertEqual(by_id["OLD1"]["description"], description)

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_refuses_corrupt_jsonl_archive(self, mock_txs):
        # A malformed line must stop the merge before anything is fetched or
        # written; skipping it would silently drop history on the rewrite.
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.jsonl"
            original = (
                json.dumps({"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0}) + "\n"
                + '{"user_transaction_id": "TX2", "transaction_da\n'
            )
            out.write_text(original, encoding="utf-8")
            mock_txs.return_value = self._mk_result([])

            code, err = self._run_merge(out)

            self.assertEqual(code, 1)
            self.assertIn("line 2", err)
            self.assertIn("left unchanged", err)
            self.assertNotIn("Unexpected error", err)
            mock_txs.assert_not_called()
            self.assertEqual(out.read_text(encoding="utf-8"), original)

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_refuses_invalid_json_archive(self, mock_txs):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.json"
            original = '{"transactions": [{"user_transaction_id": "OLD1"'
            out.write_text(original, encoding="utf-8")
            mock_txs.return_value = self._mk_result([])

            code, err = self._run_merge(out)

            self.assertEqual(code, 1)
            self.assertIn("left unchanged", err)
            self.assertNotIn("Unexpected error", err)
            mock_txs.assert_not_called()
            self.assertEqual(out.read_text(encoding="utf-8"), original)

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_refuses_non_object_archive_entries(self, mock_txs):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "transactions.json"
            original = json.dumps([{"user_transaction_id": "OLD1", "transaction_date": "2020-01-15"}, 42])
            out.write_text(original, encoding="utf-8")
            mock_txs.return_value = self._mk_result([])

            code, err = self._run_merge(out)

            self.assertEqual(code, 1)
            self.assertIn("left unchanged", err)
            mock_txs.assert_not_called()
            self.assertEqual(out.read_text(encoding="utf-8"), original)

    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_transactions")
    def test_merge_applies_to_default_output_path(self, mock_txs):
        # --output-transactions has a default, so --merge without it is not a
        # no-op: it merges into the default archive.
        with tempfile.TemporaryDirectory() as tmpdir:
            default_out = Path(tmpdir) / "empower_transactions.jsonl"
            default_out.write_text(
                json.dumps({"user_transaction_id": "OLD1", "transaction_date": "2020-01-15", "description": "Old", "amount": 10.0}) + "\n",
                encoding="utf-8",
            )
            mock_txs.return_value = self._mk_result([
                {"user_transaction_id": "TX3", "transaction_date": "2026-09-20", "description": "New", "amount": 7.0},
            ])
            argv = [
                "empower", "--transactions", "--merge",
                "--quiet", "--session-file", "/nonexistent/session.json", "--mock",
            ]
            with patch("empower_personal_dashboard.cli.DEFAULT_TRANSACTIONS_FILE", default_out), \
                    patch.object(sys, "argv", argv):
                self.assertEqual(cli_main(), 0)

            records = [json.loads(line) for line in default_out.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual([r["user_transaction_id"] for r in records], ["TX3", "OLD1"])


class TestSafeOutputPath(unittest.TestCase):
    def test_resolves_regular_path_and_rejects_symlink(self):
        from empower_personal_dashboard.cli import _safe_output_path

        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "out.json"
            self.assertEqual(_safe_output_path(real), real.resolve())
            link = Path(tmp) / "link.json"
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                _safe_output_path(link)

    def test_rejects_output_inside_symlinked_directory(self):
        from empower_personal_dashboard.cli import _safe_output_path

        with tempfile.TemporaryDirectory() as tmp:
            real_dir = Path(tmp) / "real"
            real_dir.mkdir()
            link_dir = Path(tmp) / "link"
            link_dir.symlink_to(real_dir)
            output_inside_link = link_dir / "out.json"
            with self.assertRaises(ValueError):
                _safe_output_path(output_inside_link)


class TestCliLoginChaining(unittest.TestCase):
    @patch("empower_personal_dashboard.cli.interactive_login", return_value=0)
    @patch("empower_personal_dashboard.cli._build_client")
    def test_login_alone_exits_cleanly(self, mock_build_client, mock_interactive_login):
        from empower_personal_dashboard.cli import main
        test_args = ["empower", "--login"]
        with patch.object(sys, "argv", test_args):
            ret = main()
            self.assertEqual(ret, 0)
            mock_interactive_login.assert_called_once()

    @patch("empower_personal_dashboard.cli.interactive_login", return_value=0)
    @patch("empower_personal_dashboard.cli.EmpowerDashboardClient.fetch_balances")
    @patch("empower_personal_dashboard.cli._build_client")
    def test_login_with_all_continues_to_extraction(self, mock_build_client, mock_fetch_balances, mock_interactive_login):
        from empower_personal_dashboard.cli import main
        from empower_personal_dashboard.models import DashboardBalances
        client_mock = MagicMock()
        client_mock.fetch_balances.return_value = DashboardBalances(
            as_of_date="2026-10-04",
            net_worth=1000.0,
            total_cash=1000.0,
            total_investment=0.0,
            total_card_liabilities=0.0,
            total_loan=0.0,
            total_mortgage=0.0,
            total_other_assets=0.0,
            total_other_liabilities=0.0,
            accounts=[],
            mode="live"
        )
        client_mock.fetch_holdings.return_value = None
        client_mock.fetch_transactions.return_value = None
        mock_build_client.return_value = client_mock

        test_args = ["empower", "--login", "--balances", "--format", "json"]
        with patch.object(sys, "argv", test_args):
            ret = main()
            self.assertEqual(ret, 0)
            mock_interactive_login.assert_called_once()
            client_mock.fetch_balances.assert_called_once()


if __name__ == "__main__":
    unittest.main()

