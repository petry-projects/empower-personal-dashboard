"""
test_openapi_contract.py — Hermetic contract tests for OpenAPI 3.1 specification.

Validates that:
1. docs/openapi.yaml adheres to OpenAPI 3.1 structure, security schemes, and Zero-PII standards.
2. Raw upstream mock responses match the wire RPC envelope schemas.
3. Canonical domain models (models.py) and CLI/MCP data match domain schemas.
4. Execution remains hermetic, offline, and sub-second.
"""

import json
from pathlib import Path
import re
import unittest

import yaml
from jsonschema import Draft202012Validator

try:
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
    HAS_REFERENCING = True
except ImportError:
    from jsonschema import RefResolver
    HAS_REFERENCING = False

from empower_personal_dashboard import __version__ as PACKAGE_VERSION
from empower_personal_dashboard.client import EmpowerDashboardClient
from empower_personal_dashboard.models import (
    AccountBalance,
    DashboardBalances,
    DashboardHoldings,
    DashboardTransactions,
    InvestmentHolding,
    Transaction,
)


class TestOpenApiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parent.parent
        cls.spec_path = cls.repo_root / "docs" / "openapi.yaml"
        cls.root_spec_path = cls.repo_root / "openapi.yaml"

        if not cls.spec_path.exists():
            raise FileNotFoundError(f"OpenAPI spec not found at {cls.spec_path}")

        with open(cls.spec_path, "r", encoding="utf-8") as f:
            cls.spec = yaml.safe_load(f)

        # Build schema validator helper
        if HAS_REFERENCING:
            resource = Resource(contents=cls.spec, specification=DRAFT202012)
            cls.registry = Registry().with_resource("urn:openapi", resource)
        else:
            cls.resolver = RefResolver.from_schema(cls.spec)

    def _validate_schema(self, instance: dict, schema_name: str) -> None:
        """Validate a Python dictionary instance against a component schema in the spec."""
        self.assertIn("components", self.spec)
        self.assertIn("schemas", self.spec["components"])
        self.assertIn(schema_name, self.spec["components"]["schemas"], f"Schema {schema_name} not in openapi.yaml")

        if HAS_REFERENCING:
            ref_schema = {"$ref": f"urn:openapi#/components/schemas/{schema_name}"}
            validator = Draft202012Validator(
                ref_schema,
                registry=self.registry,
                format_checker=Draft202012Validator.FORMAT_CHECKER,
            )
            errors = list(validator.iter_errors(instance))
            if errors:
                err_msgs = [f"- {e.json_path}: {e.message}" for e in errors]
                self.fail(f"Validation failed for schema '{schema_name}':\n" + "\n".join(err_msgs))
        else:
            schema = self.spec["components"]["schemas"][schema_name]
            from jsonschema import validate
            validate(
                instance=instance,
                schema=schema,
                resolver=self.resolver,
                format_checker=Draft202012Validator.FORMAT_CHECKER,
            )

    def test_spec_files_exist_and_resolve(self):
        """Verify both docs/openapi.yaml and root openapi.yaml exist and parse identically."""
        self.assertTrue(self.spec_path.exists())
        self.assertTrue(self.root_spec_path.exists())
        self.assertTrue(self.root_spec_path.is_symlink())
        self.assertEqual(self.root_spec_path.resolve(), self.spec_path.resolve())

        with open(self.root_spec_path, "r", encoding="utf-8") as f:
            root_spec = yaml.safe_load(f)

        self.assertEqual(self.spec, root_spec)

    def test_spec_metadata_and_servers(self):
        """Verify OpenAPI version, licensing, contact, and dual-server endpoints."""
        self.assertEqual(self.spec.get("openapi"), "3.1.0")

        info = self.spec.get("info", {})
        self.assertEqual(info.get("title"), "Empower Personal Dashboard API")
        self.assertEqual(
            info.get("version"),
            PACKAGE_VERSION,
            "OpenAPI spec version must track the package version "
            f"({PACKAGE_VERSION}); update docs/openapi.yaml when bumping the release.",
        )
        self.assertEqual(info.get("license", {}).get("identifier"), "MIT")

        servers = self.spec.get("servers", [])
        server_urls = [s["url"] for s in servers]
        self.assertIn("https://home.personalcapital.com/api", server_urls)
        self.assertIn("https://pc-api.empower-retirement.com/api", server_urls)

    def test_security_schemes_defined(self):
        """Verify CookieAuth and CsrfToken security schemes."""
        schemes = self.spec.get("components", {}).get("securitySchemes", {})
        self.assertIn("CookieAuth", schemes)
        self.assertEqual(schemes["CookieAuth"]["type"], "apiKey")
        self.assertEqual(schemes["CookieAuth"]["in"], "cookie")
        self.assertEqual(schemes["CookieAuth"]["name"], "JSESSIONID")

        self.assertIn("CsrfToken", schemes)
        self.assertEqual(schemes["CsrfToken"]["type"], "apiKey")
        self.assertEqual(schemes["CsrfToken"]["in"], "header")
        self.assertEqual(schemes["CsrfToken"]["name"], "X-CSRF")

    def test_all_documented_endpoints_exist(self):
        """Ensure all required RPC endpoints and operations are present with 200, 400, 401 responses."""
        expected_paths = [
            "/login/identifyUser",
            "/credential/challengeSms",
            "/credential/challengeEmail",
            "/credential/authenticateSms",
            "/credential/authenticateEmailByCode",
            "/credential/authenticatePassword",
            "/newaccount/getAccounts",
            "/newaccount/getAccounts2",
            "/invest/getHoldings",
            "/transaction/getUserTransactions",
            "/transaction/getUserTransactions2",
        ]

        paths = self.spec.get("paths", {})
        for path in expected_paths:
            self.assertIn(path, paths, f"Path {path} missing from OpenAPI specification")
            op = paths[path].get("post")
            self.assertIsNotNone(op, f"Path {path} missing POST operation")
            self.assertIn("summary", op)
            self.assertIn("responses", op)
            responses = op["responses"]
            self.assertIn("200", responses, f"Endpoint {path} missing 200 response")
            self.assertIn("400", responses, f"Endpoint {path} missing 400 response")
            self.assertIn("401", responses, f"Endpoint {path} missing 401 response")

    def test_specification_contains_no_known_sensitive_patterns(self):
        """Assert specification text contains no known-sensitive email, SSN, or credential patterns."""
        spec_text = yaml.dump(self.spec)

        # Disallowed patterns that could indicate accidental real credential or personal data leakage
        prohibited_terms = [
            r"password123",
            r"(?<!\*)[a-zA-Z0-9_.+-]+@gmail\.com",
            r"(?<!\*)[a-zA-Z0-9_.+-]+@yahoo\.com",
            r"(?<!\*)[a-zA-Z0-9_.+-]+@hotmail\.com",
            r"\b\d{3}-\d{2}-\d{4}\b",  # SSN pattern
        ]

        for pattern in prohibited_terms:
            matches = re.findall(pattern, spec_text, re.IGNORECASE)
            self.assertEqual(
                len(matches), 0,
                f"Potentially sensitive non-synthetic pattern detected in spec: {pattern} -> {matches}"
            )

    # --------------------------------------------------------------------------
    # Tier 1 Contract Tests: Upstream RPC Response Envelopes
    # --------------------------------------------------------------------------

    def test_contract_upstream_get_accounts_envelope(self):
        """Validate raw getAccounts RPC response matches GetAccountsEnvelope schema."""
        raw_response = {
            "spHeader": {
                "success": True,
                "authLevel": "USER_REMEMBERED",
                "csrf": "csrf-synth-12345",
                "status": "OK",
                "code": 0,
            },
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
                        "userAccountId": 1001,
                        "name": "Checking Account",
                        "firmName": "Summit Bank",
                        "accountType": "BANK",
                        "currentBalance": 50000.0,
                        "isAsset": True,
                        "currency": "USD",
                        "lastRefreshed": "2026-10-01T08:00:00Z",
                    }
                ],
            },
        }
        self._validate_schema(raw_response, "GetAccountsEnvelope")

    def test_contract_upstream_get_holdings_envelope(self):
        """Validate raw getHoldings RPC response matches GetHoldingsEnvelope schema."""
        raw_response = {
            "spHeader": {
                "success": True,
                "authLevel": "USER_REMEMBERED",
                "csrf": "csrf-synth-12345",
                "status": "OK",
                "code": 0,
            },
            "spData": {
                "holdingsTotalValue": 100000.0,
                "holdings": [
                    {
                        "userAccountId": 101,
                        "accountName": "Brokerage",
                        "ticker": "SPY",
                        "cusip": "78462F103",
                        "description": "SPDR S&P 500 ETF Trust",
                        "holdingType": "ETF",
                        "quantity": 100.0,
                        "price": 500.0,
                        "value": 50000.0,
                        "costBasis": 40000.0,
                        "holdingPercentage": 50.0,
                        "oneDayPercentChange": 0.5,
                        "oneDayValueChange": 250.0,
                    }
                ],
            },
        }
        self._validate_schema(raw_response, "GetHoldingsEnvelope")

    def test_contract_upstream_get_user_transactions_envelope(self):
        """Validate raw getUserTransactions RPC response matches GetUserTransactionsEnvelope schema."""
        raw_response = {
            "spHeader": {
                "success": True,
                "authLevel": "USER_REMEMBERED",
                "csrf": "csrf-synth-12345",
                "status": "OK",
                "code": 0,
            },
            "spData": {
                "startDate": "2026-09-01",
                "endDate": "2026-09-30",
                "moneyIn": 1000.0,
                "moneyOut": 200.0,
                "netCashflow": 800.0,
                "transactions": [
                    {
                        "userTransactionId": "TXN-001",
                        "accountId": "ACC-CHK-001",
                        "userAccountId": 1003,
                        "accountName": "Apex Premier Checking",
                        "transactionDate": "2026-09-25",
                        "description": "Payroll Direct Deposit",
                        "originalDescription": "ACH DIRECT DEPOSIT",
                        "amount": 1000.0,
                        "isCredit": True,
                        "isCashIn": True,
                        "isCashOut": False,
                        "isIncome": True,
                        "isSpending": False,
                        "transactionType": "Deposit",
                        "status": "posted",
                        "categoryId": 1,
                    }
                ],
            },
        }
        self._validate_schema(raw_response, "GetUserTransactionsEnvelope")

    def test_contract_upstream_identify_user_envelope(self):
        """Validate identifyUser 2FA challenge response matches IdentifyUserEnvelope schema."""
        raw_response = {
            "spHeader": {
                "success": True,
                "authLevel": "USER_IDENTIFIED",
                "csrf": "csrf-synth-identify",
                "status": "OK",
            },
            "spData": {
                "allCredentials": [
                    {"name": "OOB_SMS", "status": "ACTIVE", "display": "(***) ***-1234"}
                ]
            },
        }
        self._validate_schema(raw_response, "IdentifyUserEnvelope")

    def test_contract_upstream_rpc_error_envelope(self):
        """Validate error and migration (Code 920) responses match RpcEnvelope schema."""
        raw_error = {
            "spHeader": {
                "success": False,
                "status": "ERROR",
                "code": 920,
                "errors": [
                    {"code": 920, "message": "User has migrated to the new site."}
                ],
            }
        }
        self._validate_schema(raw_error, "RpcEnvelope")

    # --------------------------------------------------------------------------
    # Tier 2 Contract Tests: Canonical Domain Models
    # --------------------------------------------------------------------------

    def test_contract_domain_dashboard_balances(self):
        """Validate DashboardBalances dataclass serialization against DashboardBalances schema."""
        client = EmpowerDashboardClient(mock_mode=True)
        balances = client.fetch_balances()
        self._validate_schema(balances.to_dict(), "DashboardBalances")

    def test_contract_domain_account_balance(self):
        """Validate AccountBalance dataclass serialization against AccountBalance schema."""
        acct = AccountBalance(
            account_id="ACC-001",
            account_name="Horizon Checking",
            firm_name="Horizon Bank",
            account_type="BANK",
            balance=12500.50,
            is_asset=True,
            currency="USD",
            last_refreshed="2026-10-01T08:00:00Z",
            user_account_id=1001,
        )
        self._validate_schema(acct.to_dict(), "AccountBalance")

    def test_contract_domain_dashboard_holdings(self):
        """Validate DashboardHoldings dataclass serialization against DashboardHoldings schema."""
        client = EmpowerDashboardClient(mock_mode=True)
        holdings = client.fetch_holdings()
        self._validate_schema(holdings.to_dict(), "DashboardHoldings")

    def test_contract_domain_investment_holding(self):
        """Validate InvestmentHolding dataclass serialization against InvestmentHolding schema."""
        holding = InvestmentHolding(
            user_account_id=1001,
            account_name="Horizon 401(k) Retirement Plan",
            ticker="VTI",
            cusip="922908769",
            description="Vanguard Total Stock Market ETF",
            holding_type="ETF",
            quantity=100.0,
            price=275.50,
            value=27550.00,
            cost_basis=20000.00,
            holding_percentage=50.0,
            one_day_percent_change=0.45,
            one_day_value_change=123.95,
        )
        self._validate_schema(holding.to_dict(), "InvestmentHolding")

    def test_contract_domain_dashboard_transactions(self):
        """Validate DashboardTransactions dataclass serialization against DashboardTransactions schema."""
        client = EmpowerDashboardClient(mock_mode=True)
        transactions = client.fetch_transactions()
        self._validate_schema(transactions.to_dict(), "DashboardTransactions")

    def test_contract_domain_transaction(self):
        """Validate Transaction dataclass serialization against Transaction schema."""
        tx = Transaction(
            user_transaction_id="TXN-001",
            account_id="ACC-CHK-001",
            user_account_id=1001,
            account_name="Apex Premier Checking",
            transaction_date="2026-09-15",
            description="Payroll Direct Deposit",
            original_description="ACH DIRECT DEPOSIT PAYROLL",
            amount=4500.00,
            is_credit=True,
            is_cash_in=True,
            is_cash_out=False,
            is_income=True,
            is_spending=False,
            transaction_type="Deposit",
            status="posted",
            category_id=1,
        )
        self._validate_schema(tx.to_dict(), "Transaction")

    def test_contract_domain_net_worth_summary(self):
        """Validate NetWorthSummary serialization against NetWorthSummary schema."""
        try:
            from empower_personal_dashboard.mcp_server import FastMCP, create_mcp_server
            if FastMCP is not None:
                import asyncio
                server = create_mcp_server(client=EmpowerDashboardClient(mock_mode=True))

                async def get_summary():
                    res = await server.call_tool("get_net_worth_summary", {"format": "markdown"})
                    return json.loads(res.content[0].text)

                summary = asyncio.run(get_summary())
            else:
                raise ImportError("FastMCP not installed")
        except ImportError:
            client = EmpowerDashboardClient(mock_mode=True)
            balances = client.fetch_balances()
            summary = {
                "status": "success",
                "net_worth": balances.net_worth,
                "total_cash": balances.total_cash,
                "total_investment": balances.total_investment,
                "total_credit": balances.total_credit_card,
                "total_credit_card": balances.total_credit_card,
                "total_mortgage": balances.total_mortgage,
                "total_loan": balances.total_loan,
                "total_other_assets": balances.total_other_assets,
                "total_other_liabilities": balances.total_other_liabilities,
                "accounts_count": len(balances.accounts),
                "formatted_output": f"### Empower Net Worth Summary ({balances.as_of_date})",
            }
        self._validate_schema(summary, "NetWorthSummary")


if __name__ == "__main__":
    unittest.main()
