"""
test_client.py — Unit tests for EmpowerDashboardClient.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from empower_personal_dashboard.client import EmpowerDashboardClient
from empower_personal_dashboard.exceptions import (
    EmpowerError,
    LoginFailedException,
    RequireTwoFactorException,
    SessionExpiredError,
)
from empower_personal_dashboard.models import (
    DashboardBalances,
    DashboardHoldings,
    DashboardTransactions,
)


class TestClientSessionPersistence(unittest.TestCase):
    def test_save_and_load_session_with_mode_0600(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_file = Path(tmpdir) / "test_session.json"
            client = EmpowerDashboardClient(session_file=session_file, mock_mode=False)
            client.csrf = "test-csrf-token-abc"
            client.session.cookies.set("JSESSIONID", "cookie-12345")

            saved_path = client.save_session(session_file)
            self.assertTrue(saved_path.exists())

            # Verify POSIX permissions are 0600 (owner read/write only)
            mode = session_file.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600)

            # Load session in new client instance
            new_client = EmpowerDashboardClient(session_file=session_file, mock_mode=False)
            self.assertTrue(new_client.load_session(session_file))
            self.assertEqual(new_client.csrf, "test-csrf-token-abc")
            self.assertEqual(new_client.session.cookies.get("JSESSIONID"), "cookie-12345")

    def test_saved_cookies_are_list_of_attribute_dicts(self):
        # The new on-disk format serializes cookies as a list of attribute
        # dicts (not a flattened {name: value} map), bumping the version to 2.
        with tempfile.TemporaryDirectory() as tmpdir:
            session_file = Path(tmpdir) / "session.json"
            client = EmpowerDashboardClient(session_file=session_file, mock_mode=False)
            client.session.cookies.set(
                "JSESSIONID", "abc", domain="pc-api.empower-retirement.com", path="/"
            )

            client.save_session(session_file)
            data = json.loads(session_file.read_text(encoding="utf-8"))

            self.assertEqual(data["version"], 2)
            self.assertIsInstance(data["cookies"], list)
            entry = data["cookies"][0]
            self.assertEqual(entry["name"], "JSESSIONID")
            self.assertEqual(entry["value"], "abc")
            self.assertEqual(entry["domain"], "pc-api.empower-retirement.com")
            self.assertEqual(entry["path"], "/")
            self.assertIn("secure", entry)
            self.assertIn("expires", entry)

    def test_round_trip_preserves_full_cookie_attributes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_file = Path(tmpdir) / "session.json"
            client = EmpowerDashboardClient(session_file=session_file, mock_mode=False)
            client.session.cookies.set(
                "JSESSIONID",
                "cookie-12345",
                domain="pc-api.empower-retirement.com",
                path="/api",
                secure=True,
                expires=2000000000,
                rest={"HttpOnly": True},
            )

            client.save_session(session_file)

            new_client = EmpowerDashboardClient(session_file=session_file, mock_mode=False)
            self.assertTrue(new_client.load_session(session_file))

            loaded = next(iter(new_client.session.cookies))
            self.assertEqual(loaded.name, "JSESSIONID")
            self.assertEqual(loaded.value, "cookie-12345")
            self.assertEqual(loaded.domain, "pc-api.empower-retirement.com")
            self.assertEqual(loaded.path, "/api")
            self.assertTrue(loaded.secure)
            self.assertEqual(loaded.expires, 2000000000)
            self.assertTrue(loaded.has_nonstandard_attr("HttpOnly"))

    def test_same_name_cookies_across_domains_are_both_retained(self):
        # dict_from_cookiejar collapsed same-name cookies to one entry; the full
        # serialization must keep both the apex and host-scoped variants.
        with tempfile.TemporaryDirectory() as tmpdir:
            session_file = Path(tmpdir) / "session.json"
            client = EmpowerDashboardClient(session_file=session_file, mock_mode=False)
            client.session.cookies.set(
                "JSESSIONID", "host-value", domain="pc-api.empower-retirement.com", path="/"
            )
            client.session.cookies.set(
                "JSESSIONID", "apex-value", domain=".empower-retirement.com", path="/"
            )

            client.save_session(session_file)

            new_client = EmpowerDashboardClient(session_file=session_file, mock_mode=False)
            self.assertTrue(new_client.load_session(session_file))

            values = {
                c.domain: c.value
                for c in new_client.session.cookies
                if c.name == "JSESSIONID"
            }
            self.assertEqual(values["pc-api.empower-retirement.com"], "host-value")
            self.assertEqual(values[".empower-retirement.com"], "apex-value")

    def test_legacy_plain_dict_cookies_still_load(self):
        # Backward compatibility: a pre-existing session file whose "cookies"
        # field is a flat {name: value} dict must still load via update().
        with tempfile.TemporaryDirectory() as tmpdir:
            session_file = Path(tmpdir) / "session.json"
            legacy = {
                "version": 1,
                "base_url": "https://pc-api.empower-retirement.com",
                "csrf": "legacy-csrf",
                "cookies": {"JSESSIONID": "legacy-cookie"},
            }
            session_file.write_text(json.dumps(legacy), encoding="utf-8")

            client = EmpowerDashboardClient(session_file=session_file, mock_mode=False)
            self.assertTrue(client.load_session(session_file))
            self.assertEqual(client.csrf, "legacy-csrf")
            self.assertEqual(client.session.cookies.get("JSESSIONID"), "legacy-cookie")

    def test_load_nonexistent_session_returns_false(self):
        client = EmpowerDashboardClient(session_file=Path("/nonexistent/file.json"), mock_mode=False)
        self.assertFalse(client.load_session())

    def test_session_file_from_env_expands_tilde(self):
        # A `~`-prefixed EMPOWER_SESSION_FILE must resolve to the home directory,
        # not a literal "~" folder under the CWD.
        with patch.dict(
            os.environ,
            {"EMPOWER_SESSION_FILE": "~/.empower_personal_dashboard_session.json"},
            clear=False,
        ):
            client = EmpowerDashboardClient(mock_mode=True)
        expected = Path.home() / ".empower_personal_dashboard_session.json"
        self.assertEqual(client.session_file, expected)
        self.assertNotIn("~", str(client.session_file))

    def test_explicit_session_file_expands_tilde(self):
        client = EmpowerDashboardClient(
            session_file="~/some_session.json", mock_mode=True
        )
        self.assertEqual(client.session_file, Path.home() / "some_session.json")


class TestClientAuthentication(unittest.TestCase):
    def setUp(self):
        self.client = EmpowerDashboardClient(session_file=Path("/tmp/dummy_sess.json"), mock_mode=False)

    @patch("requests.Session.get")
    @patch("requests.Session.post")
    def test_login_requires_two_factor(self, mock_post, mock_get):
        mock_get_resp = MagicMock()
        mock_get_resp.text = "<html>window.csrf = 'init-csrf-token';</html>"
        mock_get.return_value = mock_get_resp

        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {
            "spHeader": {
                "success": True,
                "csrf": "csrf-222",
                "authLevel": "USER_IDENTIFIED",
            },
            "spData": {
                "allCredentials": [{"name": "OOB_SMS", "status": "ACTIVE"}],
            },
        }
        mock_post.return_value = mock_post_resp

        with self.assertRaises(RequireTwoFactorException) as ctx:
            self.client.login("user@example.com", "password")

        self.assertEqual(self.client.csrf, "csrf-222")
        self.assertEqual(len(ctx.exception.available_methods), 1)

    @patch("requests.Session.get")
    @patch("requests.Session.post")
    def test_login_auto_routes_on_code_920_migration(self, mock_post, mock_get):
        mock_get_resp = MagicMock()
        mock_get_resp.text = "<html>window.csrf = 'csrf-111';</html>"
        mock_get.return_value = mock_get_resp

        resp_migrated = MagicMock()
        resp_migrated.status_code = 200
        resp_migrated.json.return_value = {
            "spHeader": {
                "success": False,
                "errors": [{"code": 920, "message": "User has migrated to the new site."}],
            }
        }

        resp_success = MagicMock()
        resp_success.status_code = 200
        resp_success.json.return_value = {
            "spHeader": {
                "success": True,
                "csrf": "migrated-csrf",
                "authLevel": "USER_IDENTIFIED",
            }
        }
        mock_post.side_effect = [resp_migrated, resp_success]

        with self.assertRaises(RequireTwoFactorException):
            self.client.login("user@example.com", "password")

        self.assertIn("empower-retirement.com", self.client.base_url)

    @patch("requests.Session.post")
    def test_request_2fa_challenge_sms(self, mock_post):
        self.client.csrf = "csrf-token"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"spHeader": {"success": True}}
        mock_post.return_value = mock_resp

        res = self.client.request_2fa_challenge(mode="SMS")
        self.assertTrue(res["spHeader"]["success"])
        args, kwargs = mock_post.call_args
        self.assertTrue(args[0].endswith("/credential/challengeSms"))

    @patch("requests.Session.post")
    def test_submit_2fa_code(self, mock_post):
        self.client.csrf = "csrf-token"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"spHeader": {"success": True, "csrf": "authed-csrf"}}
        mock_post.return_value = mock_resp

        res = self.client.submit_2fa_code("123456", mode="SMS")
        self.assertTrue(res["spHeader"]["success"])
        self.assertEqual(self.client.csrf, "authed-csrf")


class TestClientDataParsing(unittest.TestCase):
    def setUp(self):
        self.client = EmpowerDashboardClient(session_file=Path("/tmp/dummy_sess.json"), mock_mode=False)
        self.client.csrf = "authed-csrf"

    @patch("requests.Session.post")
    def test_fetch_balances_parses_accounts(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {
                "networth": 250000.0,
                "totalCash": 50000.0,
                "totalInvestment": 200000.0,
                "totalCreditCard": 0.0,
                "totalLoan": 0.0,
                "totalMortgage": 0.0,
                "accounts": [
                    {
                        "accountId": "ACC-1",
                        "name": "Checking Account",
                        "firmName": "Summit Bank",
                        "accountType": "BANK",
                        "currentBalance": 50000.0,
                        "isAsset": True,
                    }
                ],
            },
        }
        mock_post.return_value = mock_resp

        bals = self.client.fetch_balances()
        self.assertIsInstance(bals, DashboardBalances)
        self.assertEqual(bals.net_worth, 250000.0)
        self.assertEqual(len(bals.accounts), 1)
        self.assertEqual(bals.accounts[0]["firm_name"], "Summit Bank")

    @patch("requests.Session.post")
    def test_fetch_holdings_sanitizes_descriptions(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {
                "holdingsTotalValue": 100000.0,
                "holdings": [
                    {
                        "userAccountId": 101,
                        "accountName": "Brokerage",
                        "ticker": "SPY",
                        "description": "SPDR\ufffd S&P 500 ETF Trust",
                        "holdingType": "ETF",
                        "quantity": 100.0,
                        "price": 500.0,
                        "value": 50000.0,
                        "holdingPercentage": 50.0,
                    }
                ],
            },
        }
        mock_post.return_value = mock_resp

        holdings = self.client.fetch_holdings()
        self.assertIsInstance(holdings, DashboardHoldings)
        self.assertEqual(holdings.total_value, 100000.0)
        h0 = holdings.holdings[0]
        self.assertEqual(h0["description"], "SPDR S&P 500 ETF Trust")
        self.assertNotIn("\ufffd", h0["description"])

    @patch("requests.Session.post")
    def test_fetch_transactions_sorts_descending(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {
                "startDate": "2026-09-01",
                "endDate": "2026-09-30",
                "moneyIn": 1000.0,
                "moneyOut": 200.0,
                "netCashflow": 800.0,
                "transactions": [
                    {"transactionDate": "2026-09-05", "description": "Earlier"},
                    {"transactionDate": "2026-09-25", "description": "Later"},
                ],
            },
        }
        mock_post.return_value = mock_resp

        txs = self.client.fetch_transactions()
        self.assertIsInstance(txs, DashboardTransactions)
        self.assertEqual(txs.total_transactions, 2)
        # Ensure newest is first
        self.assertEqual(txs.transactions[0]["transaction_date"], "2026-09-25")
        self.assertEqual(txs.transactions[1]["transaction_date"], "2026-09-05")

    @patch("requests.Session.post")
    def test_fetch_transactions_omits_start_date_when_none(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {
                "startDate": "2018-05-12",
                "endDate": "2026-10-02",
                "moneyIn": 0.0,
                "moneyOut": 0.0,
                "netCashflow": 0.0,
                "transactions": [],
            },
        }
        mock_post.return_value = mock_resp

        txs = self.client.fetch_transactions(start_date=None)
        _, kwargs = mock_post.call_args
        payload = kwargs["data"]
        self.assertNotIn("startDate", payload)
        self.assertEqual(txs.start_date, "2018-05-12")

    @patch("requests.Session.post")
    def test_fetch_histories_live_mocked(self, mock_post):
        from empower_personal_dashboard.models import DashboardHistories

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {
                "startDate": "2024-01-01",
                "endDate": "2024-01-31",
                "histories": [
                    {
                        "date": "2024-01-01",
                        "totalAssets": 150000.0,
                        "totalLiabilities": 5000.0,
                        "netWorth": 145000.0,
                        "balances": {
                            "ACC-INV-001": 120000.0,
                            "ACC-CHK-002": 30000.0,
                            "ACC-CRD-003": -5000.0,
                        },
                    },
                    {
                        "date": "2024-01-15",
                        "totalAssets": 155000.0,
                        "totalLiabilities": 4500.0,
                        "netWorth": 150500.0,
                        "balances": {
                            "ACC-INV-001": 124000.0,
                            "ACC-CHK-002": 31000.0,
                            "ACC-CRD-003": -4500.0,
                        },
                    },
                ],
            },
        }
        mock_post.return_value = mock_resp

        histories = self.client.fetch_histories(start_date="2024-01-01", end_date="2024-01-31")
        self.assertIsInstance(histories, DashboardHistories)
        self.assertEqual(histories.total_points, 2)
        self.assertEqual(histories.histories[0]["date"], "2024-01-01")
        self.assertEqual(histories.histories[0]["net_worth"], 145000.0)
        self.assertEqual(histories.histories[1]["balances"]["ACC-INV-001"], 124000.0)

        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["data"]["startDate"], "2024-01-01")
        self.assertEqual(kwargs["data"]["endDate"], "2024-01-31")

    @patch("requests.Session.post")
    def test_fetch_histories_normalizes_scalar_account_filter_to_array(self, mock_post):
        # GetHistoriesRequest.userAccountIds is an array in the contract. A scalar
        # string (incl. CSV) filter must be expanded to a list on the wire.
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {"startDate": "2024-01-01", "endDate": "2024-01-31", "histories": []},
        }
        mock_post.return_value = mock_resp

        self.client.fetch_histories(
            start_date="2024-01-01", end_date="2024-01-31", user_account_ids="1001"
        )
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["data"]["userAccountIds"], ["1001"])

        self.client.fetch_histories(
            start_date="2024-01-01", end_date="2024-01-31", user_account_ids="1001,1002"
        )
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["data"]["userAccountIds"], ["1001", "1002"])

    @patch("requests.Session.post")
    def test_fetch_transactions_normalizes_scalar_account_filter_to_array(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {"startDate": "2024-01-01", "endDate": "2024-01-31", "transactions": []},
        }
        mock_post.return_value = mock_resp

        self.client.fetch_transactions(user_account_ids="1001,1002")
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["data"]["userAccountIds"], ["1001", "1002"])

    @patch("requests.Session.post")
    def test_fetch_histories_derives_bounds_from_points_when_absent(self, mock_post):
        # When neither the request nor the response supplies startDate/endDate,
        # the range bounds are derived from the returned point dates so the
        # canonical schema's required date bounds are never empty strings.
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {
                # startDate / endDate omitted by upstream.
                "histories": [
                    {"date": "2024-03-02", "netWorth": 100.0},
                    {"date": "2024-01-01", "netWorth": 90.0},
                    {"date": "2024-06-15", "netWorth": 110.0},
                ],
            },
        }
        mock_post.return_value = mock_resp

        histories = self.client.fetch_histories()
        self.assertEqual(histories.start_date, "2024-01-01")
        self.assertEqual(histories.end_date, "2024-06-15")
        exported = histories.to_dict()
        self.assertEqual(exported["start_date"], "2024-01-01")
        self.assertEqual(exported["end_date"], "2024-06-15")

    @patch("requests.Session.post")
    def test_fetch_histories_total_assets_fallback_excludes_liabilities(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {
                "startDate": "2024-01-01",
                "endDate": "2024-01-01",
                "histories": [
                    {
                        # totalAssets and netWorth omitted -> both are derived.
                        "date": "2024-01-01",
                        "balances": {"ACC-1": 100000.0, "ACC-CARD": -5000.0},
                    }
                ],
            },
        }
        mock_post.return_value = mock_resp

        histories = self.client.fetch_histories(start_date="2024-01-01", end_date="2024-01-01")
        point = histories.histories[0]
        # Only positive balances count as assets; the negative card is a liability.
        self.assertEqual(point["total_assets"], 100000.0)
        self.assertEqual(point["total_liabilities"], 5000.0)
        # net_worth = total_assets - total_liabilities
        self.assertEqual(point["net_worth"], 95000.0)

    @patch("requests.Session.post")
    def test_fetch_histories_normalizes_liabilities_to_absolute(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {
                "startDate": "2024-01-01",
                "endDate": "2024-01-01",
                "histories": [
                    {
                        "date": "2024-01-01",
                        "totalAssets": 150000.0,
                        "totalLiabilities": -5000.0,
                        "balances": {},
                    }
                ],
            },
        }
        mock_post.return_value = mock_resp

        histories = self.client.fetch_histories(start_date="2024-01-01", end_date="2024-01-01")
        point = histories.histories[0]
        self.assertEqual(point["total_liabilities"], 5000.0)
        self.assertEqual(point["net_worth"], 145000.0)

    @patch("requests.Session.post")
    def test_fetch_histories_sanitizes_mojibake_date_and_keys(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {
                "startDate": "2024-01-01",
                "endDate": "2024-01-01",
                "histories": [
                    {
                        "date": "2024-01-01�",
                        "totalAssets": 100000.0,
                        "balances": {"ACC�-1": 100000.0},
                    }
                ],
            },
        }
        mock_post.return_value = mock_resp

        histories = self.client.fetch_histories(start_date="2024-01-01", end_date="2024-01-01")
        point = histories.histories[0]
        # The U+FFFD replacement character is stripped from the date and keys.
        self.assertEqual(point["date"], "2024-01-01")
        self.assertNotIn("�", point["date"])
        self.assertIn("ACC-1", point["balances"])
        self.assertTrue(all("�" not in k for k in point["balances"]))

    @patch("requests.Session.post")
    def test_fetch_histories_wraps_malformed_numeric_in_empower_error(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {
                "startDate": "2024-01-01",
                "endDate": "2024-01-01",
                "histories": [
                    {"date": "2024-01-01", "totalAssets": "N/A"}
                ],
            },
        }
        mock_post.return_value = mock_resp

        # A non-numeric totalAssets must surface as the domain EmpowerError
        # rather than a raw ValueError leaking from normalization.
        with self.assertRaises(EmpowerError):
            self.client.fetch_histories(start_date="2024-01-01", end_date="2024-01-01")

    @patch("requests.Session.post")
    def test_fetch_histories_empty_response_has_valid_bounds(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"spHeader": {"success": True}, "spData": {"histories": []}}
        mock_post.return_value = mock_resp
        histories = self.client.fetch_histories()
        self.assertEqual(histories.total_points, 0)
        self.assertEqual(histories.start_date, histories.end_date)
        self.assertRegex(histories.start_date, r"^\d{4}-\d{2}-\d{2}$")
        self.assertRegex(histories.end_date, r"^\d{4}-\d{2}-\d{2}$")

    @patch("requests.Session.post")
    def test_fetch_histories_empty_response_clamps_missing_end_to_start(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "spHeader": {"success": True},
            "spData": {"startDate": "2026-06-01", "histories": []},
        }
        mock_post.return_value = mock_resp
        histories = self.client.fetch_histories(start_date="2026-01-01")
        self.assertEqual(histories.start_date, "2026-06-01")
        self.assertGreaterEqual(histories.end_date, histories.start_date)


class TestClientMockMode(unittest.TestCase):
    def test_offline_sandbox_mock_generators(self):
        client = EmpowerDashboardClient(mock_mode=True)

        bals = client.fetch_balances()
        self.assertEqual(bals.mode, "sandbox_mock")
        self.assertGreater(bals.net_worth, 0)
        self.assertGreater(len(bals.accounts), 0)

        holdings = client.fetch_holdings()
        self.assertEqual(holdings.mode, "sandbox_mock")
        self.assertGreater(holdings.total_value, 0)
        self.assertGreater(len(holdings.holdings), 0)

        txs = client.fetch_transactions()
        self.assertEqual(txs.mode, "sandbox_mock")
        self.assertGreater(txs.total_transactions, 0)

        histories = client.fetch_histories()
        self.assertEqual(histories.mode, "sandbox_mock")
        self.assertGreater(histories.total_points, 0)
        self.assertGreater(histories.histories[0]["net_worth"], 0)

    def test_mock_histories_points_stay_within_requested_range(self):
        client = EmpowerDashboardClient(mock_mode=True)
        histories = client.fetch_histories(start_date="2024-03-01", end_date="2024-09-30")
        dates = [p["date"] for p in histories.histories]
        self.assertEqual(dates[0], "2024-03-01")
        self.assertEqual(dates[-1], "2024-09-30")
        for d in dates:
            self.assertGreaterEqual(d, "2024-03-01")
            self.assertLessEqual(d, "2024-09-30")
        # Dates are non-decreasing (chronological).
        self.assertEqual(dates, sorted(dates))

    def test_mock_histories_respect_account_filter(self):
        client = EmpowerDashboardClient(mock_mode=True)
        histories = client.fetch_histories(
            start_date="2024-01-01",
            end_date="2024-12-31",
            user_account_ids=["ACC-CHK-003"],
        )
        for point in histories.histories:
            self.assertEqual(list(point["balances"].keys()), ["ACC-CHK-003"])
            # Aggregates are recomputed from the single kept (positive) balance.
            self.assertEqual(point["total_liabilities"], 0.0)
            self.assertEqual(point["total_assets"], point["balances"]["ACC-CHK-003"])

    def test_mock_histories_collapse_one_day_range_to_single_point(self):
        # A one-day request makes start == mid == end; the curve must not stamp
        # three contradictory balances on the same date.
        client = EmpowerDashboardClient(mock_mode=True)
        histories = client.fetch_histories(start_date="2024-05-01", end_date="2024-05-01")
        dates = [p["date"] for p in histories.histories]
        self.assertEqual(dates, ["2024-05-01"])
        self.assertEqual(histories.total_points, 1)

    def test_mock_histories_collapse_two_day_range_to_distinct_dates(self):
        # A two-day request can make start == mid; emitted dates must be distinct
        # and chronological with one coherent point per date.
        client = EmpowerDashboardClient(mock_mode=True)
        histories = client.fetch_histories(start_date="2024-05-01", end_date="2024-05-02")
        dates = [p["date"] for p in histories.histories]
        self.assertEqual(dates, sorted(set(dates)))
        self.assertEqual(len(dates), len(set(dates)))
        self.assertEqual(dates[0], "2024-05-01")
        self.assertEqual(dates[-1], "2024-05-02")

    def test_mock_transactions_start_date_reflects_oldest_mock(self):
        client = EmpowerDashboardClient(mock_mode=True)
        txs = client.fetch_transactions(start_date=None)
        self.assertEqual(txs.start_date, min(t["transaction_date"] for t in txs.transactions))

    def test_mock_transactions_explicit_start_date(self):
        client = EmpowerDashboardClient(mock_mode=True)
        txs = client.fetch_transactions(start_date="2024-01-01")
        self.assertEqual(txs.start_date, "2024-01-01")

    def test_mock_transactions_start_date_with_limit(self):
        client = EmpowerDashboardClient(mock_mode=True)
        txs_all = client.fetch_transactions()
        txs_limited = client.fetch_transactions(limit=1)
        self.assertEqual(txs_limited.start_date, txs_all.start_date)


class TestClientDebugEnvFlag(unittest.TestCase):
    """EMPOWER_DEBUG must be parsed as a boolean, not raw truthiness of the string."""

    def _client_with_debug_env(self, value):
        env = {k: v for k, v in os.environ.items() if k != "EMPOWER_DEBUG"}
        if value is not None:
            env["EMPOWER_DEBUG"] = value
        with patch.dict(os.environ, env, clear=True):
            return EmpowerDashboardClient(mock_mode=True)

    def test_falsy_values_disable_debug(self):
        # "0" and other falsy spellings must NOT enable debug (the #security fix:
        # any non-empty string was previously truthy, so EMPOWER_DEBUG=0 leaked logs).
        for value in ("0", "false", "False", "no", "off", "none", "", "  0  "):
            self.assertFalse(
                self._client_with_debug_env(value).debug,
                msg=f"EMPOWER_DEBUG={value!r} should disable debug",
            )

    def test_unset_disables_debug(self):
        self.assertFalse(self._client_with_debug_env(None).debug)

    def test_truthy_values_enable_debug(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(
                self._client_with_debug_env(value).debug,
                msg=f"EMPOWER_DEBUG={value!r} should enable debug",
            )

    def test_explicit_debug_arg_overrides_falsy_env(self):
        env = {k: v for k, v in os.environ.items() if k != "EMPOWER_DEBUG"}
        env["EMPOWER_DEBUG"] = "0"
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(EmpowerDashboardClient(mock_mode=True, debug=True).debug)


if __name__ == "__main__":
    unittest.main()

