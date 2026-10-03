"""
client.py — Client for Empower Personal Dashboard (formerly Personal Capital) API.

Web Dashboard: https://home.personalcapital.com
Migrated API:  https://pc-api.empower-retirement.com
"""

import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import requests

from .exceptions import (
    EmpowerError,
    LoginFailedException,
    RequireTwoFactorException,
    SessionExpiredError,
)
from .models import (
    DashboardBalances,
    DashboardHistories,
    DashboardHoldings,
    DashboardTransactions,
)
from .sanitizers import clean_api_text

DEFAULT_BASE_URL = "https://home.personalcapital.com"
MIGRATED_BASE_URL = "https://pc-api.empower-retirement.com"
DEFAULT_SESSION_FILE = Path.home() / ".empower_personal_dashboard_session.json"
CSRF_REGEX = re.compile(r"window\.csrf\s*=\s*['\"]([^'\"]+)['\"]")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}

# Values that mean "off" for a boolean environment variable. Without this, any
# non-empty string (including "0" / "false") would be truthy, so EMPOWER_DEBUG=0
# would silently enable debug logging and expose sensitive auth details.
_FALSY_ENV_VALUES = frozenset({"", "0", "false", "no", "off", "none"})


def _env_flag(value: Optional[str]) -> bool:
    """Interpret an environment-variable string as a boolean.

    Unset, empty, and common falsy spellings ("0", "false", "no", "off",
    "none", case-insensitive) are False; every other value is True.
    """
    if value is None:
        return False
    return value.strip().lower() not in _FALSY_ENV_VALUES


class EmpowerDashboardClient:
    """Client for interacting with Empower Personal Dashboard API."""

    def __init__(
        self,
        session_file: Optional[Union[str, Path]] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 90,
        mock_mode: bool = False,
        debug: bool = False,
        log_file: Optional[Union[str, Path]] = None,
    ):
        # `.expanduser()` so a `~`-prefixed EMPOWER_SESSION_FILE resolves to the
        # home directory rather than a literal "~" folder in the CWD.
        self.session_file = Path(session_file or os.environ.get("EMPOWER_SESSION_FILE", DEFAULT_SESSION_FILE)).expanduser()
        self.base_url = base_url.rstrip("/")
        self.api_endpoint = f"{self.base_url}/api"
        self.timeout = timeout_seconds
        self.mock_mode = mock_mode
        self.debug = debug or _env_flag(os.environ.get("EMPOWER_DEBUG"))
        self.log_file = Path(log_file) if log_file else None
        self.session = requests.Session()
        self.csrf: str = ""
        self.available_challenge_methods: List[Dict[str, Any]] = []

        # Configure logger
        self.logger = logging.getLogger("EmpowerDashboardClient")
        self._setup_logging()

        # Auto-load session if exists and not explicitly in mock mode
        if not self.mock_mode and self.session_file.exists():
            try:
                self.load_session(self.session_file)
            except Exception as e:
                self._log_debug(f"Failed to auto-load session from {self.session_file}: {e}")

    def _setup_logging(self) -> None:
        """Configure debug logging handlers."""
        if not self.logger.handlers:
            self.logger.setLevel(logging.DEBUG if self.debug else logging.INFO)
            formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setLevel(logging.DEBUG if self.debug else logging.WARNING)
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

            if self.log_file:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(self.log_file, mode="a", encoding="utf-8")
                file_handler.setLevel(logging.DEBUG)
                file_handler.setFormatter(formatter)
                self.logger.addHandler(file_handler)

    def _log_debug(self, msg: str) -> None:
        if self.debug or self.log_file:
            self.logger.debug(msg)

    def _sanitize_payload(self, data: Any) -> Any:
        """Sanitize secrets from payload before logging."""
        if isinstance(data, dict):
            sanitized = {}
            for k, v in data.items():
                if k.lower() in ("passwd", "password", "code", "pin", "token", "secret", "cvv"):
                    sanitized[k] = "***REDACTED***"
                elif isinstance(v, (dict, list)):
                    sanitized[k] = self._sanitize_payload(v)
                else:
                    sanitized[k] = v
            return sanitized
        elif isinstance(data, list):
            return [self._sanitize_payload(i) for i in data]
        return data

    def _get_csrf_from_homepage(self) -> Optional[str]:
        """Fetch homepage and extract initial CSRF token from window.csrf."""
        url = f"{self.base_url}/page/login/goHome" if "empower-retirement" in self.base_url else self.base_url
        self._log_debug(f"Fetching homepage CSRF from {url}...")
        try:
            r = self.session.get(url, headers=DEFAULT_HEADERS, timeout=self.timeout)
            m = CSRF_REGEX.search(r.text)
            if m:
                token = m.group(1)
                self._log_debug(f"Scraped initial CSRF token from {self.base_url}: {token}")
                return token
        except Exception as e:
            self._log_debug(f"Failed to extract CSRF from {url}: {e}")
        return None

    def post(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> requests.Response:
        """Execute raw HTTP POST against the API."""
        url = f"{self.api_endpoint}{endpoint}"
        payload = data or {}

        headers = DEFAULT_HEADERS.copy()
        if self.csrf:
            headers["X-CSRF"] = self.csrf

        self._log_debug(f"HTTP POST {url}\n{json.dumps({'data': self._sanitize_payload(payload), 'headers': headers}, indent=2)}")

        resp = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)

        try:
            json_preview = json.dumps(self._sanitize_payload(resp.json()), indent=2)
            self._log_debug(f"HTTP {resp.status_code} Response from {endpoint}\n{json_preview}")
        except Exception:
            self._log_debug(f"HTTP {resp.status_code} Non-JSON Response from {endpoint}: {resp.text[:300]}")

        return resp

    def fetch(self, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute an authenticated API call and validate success response."""
        payload = {
            "lastServerChangeId": "-1",
            "csrf": self.csrf,
            "apiClient": "WEB",
        }
        if data:
            payload.update(data)

        resp = self.post(endpoint, payload)
        if resp.status_code != 200:
            raise EmpowerError(
                f"HTTP {resp.status_code} error from {endpoint}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        try:
            res_json = resp.json()
        except Exception as e:
            raise EmpowerError(f"Failed to parse JSON response from {endpoint}: {e}", response_body=resp.text) from e

        sp_header = res_json.get("spHeader", {})
        if sp_header.get("csrf"):
            self.csrf = sp_header["csrf"]

        if not sp_header.get("success", False):
            errors = sp_header.get("errors", [])
            err_msg = "Unknown API error"
            err_code = None
            if errors and isinstance(errors, list) and len(errors) > 0:
                first_err = errors[0]
                if isinstance(first_err, dict):
                    err_msg = first_err.get("message", err_msg)
                    err_code = first_err.get("code")

            if err_code in (202, 203) or "session" in err_msg.lower():
                raise SessionExpiredError(f"Session expired ({err_msg})", status_code=200, error_code=err_code)

            raise EmpowerError(f"API request failed: {err_msg}", status_code=200, error_code=err_code, response_body=resp.text)

        return res_json

    def login(self, username: str, password: Optional[str] = None) -> Dict[str, Any]:
        """
        Authenticate user with username and password, or initiate 2FA.
        Automatically handles unified platform migration (Code 920).
        """
        if self.mock_mode:
            return {"spHeader": {"success": True, "authLevel": "USER_REMEMBERED"}}

        initial_csrf = self._get_csrf_from_homepage()
        if not initial_csrf:
            initial_csrf = "default-csrf-token"

        payload = {
            "username": username,
            "csrf": initial_csrf,
            "apiClient": "WEB",
            "bindDevice": "false",
            "skipLinkAccount": "false",
            "redirectTo": "",
            "skipFirstUse": "",
            "referrerId": "",
        }

        resp = self.post("/login/identifyUser", payload)
        if resp.status_code != 200:
            raise LoginFailedException(f"Failed to reach login endpoint (HTTP {resp.status_code})")

        res_json = resp.json()
        sp_header = res_json.get("spHeader", {})
        sp_data = res_json.get("spData", {})

        # Handle Account Migration (Error Code 920)
        errors = sp_header.get("errors", [])
        if errors and isinstance(errors, list):
            for err in errors:
                if isinstance(err, dict) and (err.get("code") == 920 or "migrated" in err.get("message", "").lower()):
                    self._log_debug(f"Account migrated detected. Re-routing base endpoint to {MIGRATED_BASE_URL}...")
                    self.base_url = MIGRATED_BASE_URL
                    self.api_endpoint = f"{self.base_url}/api"
                    new_csrf = self._get_csrf_from_homepage() or initial_csrf
                    payload["csrf"] = new_csrf
                    resp = self.post("/login/identifyUser", payload)
                    res_json = resp.json()
                    sp_header = res_json.get("spHeader", {})
                    sp_data = res_json.get("spData", {})
                    break

        if not sp_header.get("success", False):
            errors = sp_header.get("errors", [])
            err_msg = "User identification failed"
            err_code = None
            if errors and isinstance(errors, list) and len(errors) > 0:
                first_err = errors[0]
                if isinstance(first_err, dict):
                    err_msg = first_err.get("message", err_msg)
                    err_code = first_err.get("code")
            raise LoginFailedException(f"Login failed (code {err_code}): {err_msg}", error_code=err_code)

        if sp_header.get("csrf"):
            self.csrf = sp_header["csrf"]

        # Parse challenge options
        self.available_challenge_methods = sp_data.get("allCredentials", []) or sp_data.get("challengeMethods", [])

        auth_level = sp_header.get("authLevel", "")
        if auth_level in ("MFA_REQUIRED", "USER_IDENTIFIED", "NONE", ""):
            raise RequireTwoFactorException(
                "Two-factor authentication required for this device.",
                available_methods=self.available_challenge_methods,
            )

        if password:
            return self.authenticate_password(password)

        return res_json

    def request_2fa_challenge(self, mode: str = "SMS") -> Dict[str, Any]:
        """Request 2FA code via SMS or Email."""
        mode_upper = mode.upper()
        if mode_upper in ("SMS", "PHONE"):
            endpoint = "/credential/challengeSms"
            challenge_type = "challengeSMS"
        elif mode_upper == "EMAIL":
            endpoint = "/credential/challengeEmail"
            challenge_type = "challengeEmail"
        else:
            raise ValueError(f"Unsupported 2FA mode: {mode}. Must be 'SMS' or 'EMAIL'.")

        payload = {
            "challengeReason": "DEVICE_AUTH",
            "challengeMethod": "OP",
            "challengeType": challenge_type,
            "apiClient": "WEB",
            "bindDevice": "false",
            "csrf": self.csrf,
        }

        resp = self.post(endpoint, payload)
        res_json = resp.json()
        sp_header = res_json.get("spHeader", {})

        if not sp_header.get("success", False):
            errors = sp_header.get("errors", [])
            err_msg = f"Failed to send 2FA challenge via {mode}"
            err_code = None
            if errors and isinstance(errors, list) and len(errors) > 0:
                first_err = errors[0]
                if isinstance(first_err, dict):
                    err_msg = first_err.get("message", err_msg)
                    err_code = first_err.get("code")
            raise EmpowerError(f"2FA challenge error: {err_msg}", error_code=err_code)

        if sp_header.get("csrf"):
            self.csrf = sp_header["csrf"]

        return res_json

    def submit_2fa_code(self, code: str, mode: str = "SMS") -> Dict[str, Any]:
        """Submit 2FA verification code."""
        mode_upper = mode.upper()
        if mode_upper in ("SMS", "PHONE"):
            endpoint = "/credential/authenticateSms"
        elif mode_upper == "EMAIL":
            endpoint = "/credential/authenticateEmailByCode"
        else:
            raise ValueError(f"Unsupported 2FA mode: {mode}. Must be 'SMS' or 'EMAIL'.")

        payload = {
            "challengeReason": "DEVICE_AUTH",
            "challengeMethod": "OP",
            "apiClient": "WEB",
            "bindDevice": "false",
            "code": code.strip(),
            "csrf": self.csrf,
        }

        resp = self.post(endpoint, payload)
        res_json = resp.json()
        sp_header = res_json.get("spHeader", {})

        if not sp_header.get("success", False):
            errors = sp_header.get("errors", [])
            err_msg = "Invalid verification code"
            err_code = None
            if errors and isinstance(errors, list) and len(errors) > 0:
                first_err = errors[0]
                if isinstance(first_err, dict):
                    err_msg = first_err.get("message", err_msg)
                    err_code = first_err.get("code")
            raise LoginFailedException(f"2FA verification failed: {err_msg}", error_code=err_code)

        if sp_header.get("csrf"):
            self.csrf = sp_header["csrf"]

        return res_json

    def authenticate_password(self, password: str, device_name: str = "Empower CLI Client") -> Dict[str, Any]:
        """Submit account password and bind device for persistent sessions."""
        payload = {
            "bindDevice": "true",
            "deviceName": device_name,
            "redirectTo": "",
            "skipFirstUse": "",
            "skipLinkAccount": "false",
            "referrerId": "",
            "passwd": password,
            "apiClient": "WEB",
            "csrf": self.csrf,
        }

        resp = self.post("/credential/authenticatePassword", payload)
        res_json = resp.json()
        sp_header = res_json.get("spHeader", {})

        if not sp_header.get("success", False):
            errors = sp_header.get("errors", [])
            err_msg = "Password authentication failed"
            err_code = None
            if errors and isinstance(errors, list) and len(errors) > 0:
                first_err = errors[0]
                if isinstance(first_err, dict):
                    err_msg = first_err.get("message", err_msg)
                    err_code = first_err.get("code")
            raise LoginFailedException(f"Authentication failed: {err_msg}", error_code=err_code)

        if sp_header.get("csrf"):
            self.csrf = sp_header["csrf"]

        return res_json

    def save_session(self, filepath: Optional[Union[str, Path]] = None) -> Path:
        """
        Atomically save session cookies and CSRF token with private POSIX owner-only permissions (0600).
        """
        target_path = Path(filepath or self.session_file)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        session_data = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "base_url": self.base_url,
            "csrf": self.csrf,
            "cookies": requests.utils.dict_from_cookiejar(self.session.cookies),
        }

        fd, tmp_file = tempfile.mkstemp(dir=target_path.parent, prefix=".session_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                os.chmod(tmp_file, 0o600)
                json.dump(session_data, f, indent=2)
            os.replace(tmp_file, target_path)
            os.chmod(target_path, 0o600)
        except Exception:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)
            raise

        self._log_debug(f"Saved session to {target_path} (base_url: {self.base_url}, csrf: {self.csrf[:8]}...)")
        return target_path

    def load_session(self, filepath: Optional[Union[str, Path]] = None) -> bool:
        """Load session cookies and CSRF token from saved file."""
        target_path = Path(filepath or self.session_file)
        if not target_path.exists():
            return False

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.csrf = data.get("csrf", "")
            if data.get("base_url"):
                self.base_url = data["base_url"].rstrip("/")
                self.api_endpoint = f"{self.base_url}/api"

            cookies = data.get("cookies", {})
            self.session.cookies.update(cookies)
            self._log_debug(f"Loaded session from {target_path} (base_url: {self.base_url}, csrf: {self.csrf[:8]}...)")
            return True
        except Exception as e:
            self._log_debug(f"Failed to load session from {target_path}: {e}")
            return False

    def fetch_balances(self) -> DashboardBalances:
        """Fetch all linked accounts, balances, and net worth totals."""
        if self.mock_mode:
            return self._generate_mock_balances()

        try:
            result = self.fetch("/newaccount/getAccounts")
        except SessionExpiredError:
            raise
        except Exception as e:
            raise EmpowerError(f"Failed to fetch accounts from dashboard: {e}") from e

        sp_data = result.get("spData", {})
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        net_worth = float(sp_data.get("networth") or sp_data.get("netWorth") or 0.0)
        total_cash = float(sp_data.get("cashAccountsTotal") or sp_data.get("totalCash") or 0.0)
        total_inv = float(sp_data.get("investmentAccountsTotal") or sp_data.get("totalInvestment") or 0.0)
        total_cc = float(sp_data.get("creditCardAccountsTotal") or sp_data.get("totalCreditCard") or 0.0)
        total_loan = float(sp_data.get("loanAccountsTotal") or sp_data.get("totalLoan") or 0.0)
        total_mort = float(sp_data.get("mortgageAccountsTotal") or sp_data.get("totalMortgage") or 0.0)
        total_other_assets = float(sp_data.get("otherAssetAccountsTotal") or sp_data.get("totalOtherAssets") or 0.0)
        total_other_liabilities = float(sp_data.get("otherLiabilitiesAccountsTotal") or sp_data.get("totalOtherLiabilities") or 0.0)

        raw_accounts = sp_data.get("accounts", [])
        normalized_accounts = []
        for acct in raw_accounts:
            normalized_accounts.append({
                "account_id": str(acct.get("accountId", "")),
                "account_name": clean_api_text(acct.get("name") or acct.get("accountName", "Account")),
                "firm_name": clean_api_text(acct.get("firmName", "Unknown Institution")),
                "account_type": acct.get("accountType", "OTHER"),
                "balance": float(acct.get("currentBalance") or acct.get("balance", 0.0)),
                "is_asset": acct.get("isAsset", True),
                "currency": acct.get("currency", "USD"),
                "last_refreshed": acct.get("lastRefreshed"),
                "user_account_id": acct.get("userAccountId"),
            })

        return DashboardBalances(
            as_of_date=today_str,
            net_worth=net_worth,
            total_cash=total_cash,
            total_investment=total_inv,
            total_credit_card=total_cc,
            total_loan=total_loan,
            total_mortgage=total_mort,
            total_other_assets=total_other_assets,
            total_other_liabilities=total_other_liabilities,
            accounts=normalized_accounts,
            mode="live",
            raw_response=result,
        )

    def fetch_holdings(self) -> DashboardHoldings:
        """Fetch investment holdings and positions across linked brokerage/investment accounts."""
        if self.mock_mode:
            return self._generate_mock_holdings()

        try:
            result = self.fetch("/invest/getHoldings")
        except SessionExpiredError:
            raise
        except Exception as e:
            raise EmpowerError(f"Failed to fetch holdings from dashboard: {e}") from e

        sp_data = result.get("spData", {})
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total_val = float(sp_data.get("holdingsTotalValue") or 0.0)

        raw_holdings = sp_data.get("holdings", [])
        normalized_holdings = []
        for h in raw_holdings:
            cost_basis = h.get("costBasis")
            cost_basis_val = float(cost_basis) if cost_basis is not None else None
            one_day_pct = h.get("oneDayPercentChange")
            one_day_pct_val = float(one_day_pct) if one_day_pct is not None else None
            one_day_val = h.get("oneDayValueChange")
            one_day_val_change = float(one_day_val) if one_day_val is not None else None
            holding_pct = h.get("holdingPercentage")
            holding_pct_val = float(holding_pct) if holding_pct is not None else None

            normalized_holdings.append({
                "user_account_id": h.get("userAccountId"),
                "account_name": clean_api_text(h.get("accountName")),
                "ticker": clean_api_text(h.get("ticker") or h.get("originalTicker") or ""),
                "cusip": clean_api_text(h.get("cusip") or h.get("originalCusip") or ""),
                "description": clean_api_text(h.get("description") or h.get("originalDescription") or h.get("external") or ""),
                "holding_type": clean_api_text(h.get("holdingType") or h.get("type") or "OTHER"),
                "quantity": float(h.get("quantity") or 0.0),
                "price": float(h.get("price") or 0.0),
                "value": float(h.get("value") or 0.0),
                "cost_basis": cost_basis_val,
                "holding_percentage": holding_pct_val,
                "one_day_percent_change": one_day_pct_val,
                "one_day_value_change": one_day_val_change,
            })

        return DashboardHoldings(
            as_of_date=today_str,
            total_value=total_val,
            holdings=normalized_holdings,
            mode="live",
            raw_response=result,
        )

    def fetch_transactions(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        user_account_ids: Optional[Union[str, List[Union[str, int]], Tuple[Union[str, int], ...], Set[Union[str, int]]]] = None,
        limit: Optional[int] = None,
    ) -> DashboardTransactions:
        """Fetch account transactions for a date range, optionally filtered by account."""
        if self.mock_mode:
            return self._generate_mock_transactions(start_date=start_date, end_date=end_date, limit=limit)

        payload: Dict[str, Union[str, int, List[Union[str, int]], Tuple[Union[str, int], ...], Set[Union[str, int]]]] = {}
        if start_date:
            payload["startDate"] = start_date
        if end_date:
            payload["endDate"] = end_date
        if user_account_ids is not None:
            payload["userAccountIds"] = user_account_ids

        try:
            result = self.fetch("/transaction/getUserTransactions", data=payload)
        except SessionExpiredError:
            raise
        except Exception as e:
            raise EmpowerError(f"Failed to fetch transactions from dashboard: {e}") from e

        sp_data = result.get("spData", {})
        resp_start = sp_data.get("startDate") or start_date or ""
        resp_end = sp_data.get("endDate") or end_date or ""
        money_in = float(sp_data.get("moneyIn") or 0.0)
        money_out = float(sp_data.get("moneyOut") or 0.0)
        net_cashflow = float(sp_data.get("netCashflow") or 0.0)

        raw_txs = sp_data.get("transactions", [])
        # Sort in reverse chronological order (newest first)
        raw_txs.sort(key=lambda x: str(x.get("transactionDate", "")), reverse=True)
        if limit and limit > 0:
            raw_txs = raw_txs[:limit]

        normalized_txs = []
        for tx in raw_txs:
            price_val = float(tx.get("price")) if tx.get("price") is not None else None
            qty_val = float(tx.get("quantity")) if tx.get("quantity") is not None else None

            normalized_txs.append({
                "user_transaction_id": str(tx.get("userTransactionId", "")),
                "account_id": str(tx.get("accountId", "")),
                "user_account_id": tx.get("userAccountId"),
                "account_name": clean_api_text(tx.get("accountName")),
                "transaction_date": str(tx.get("transactionDate", "")),
                "description": clean_api_text(tx.get("description") or tx.get("originalDescription")),
                "original_description": clean_api_text(tx.get("originalDescription")),
                "amount": float(tx.get("amount") or 0.0),
                "is_credit": bool(tx.get("isCredit", False)),
                "is_cash_in": bool(tx.get("isCashIn", False)),
                "is_cash_out": bool(tx.get("isCashOut", False)),
                "is_income": bool(tx.get("isIncome", False)),
                "is_spending": bool(tx.get("isSpending", False)),
                "transaction_type": clean_api_text(tx.get("transactionType", "")),
                "investment_type": clean_api_text(tx.get("investmentType")) if tx.get("investmentType") else None,
                "symbol": clean_api_text(tx.get("symbol")) if tx.get("symbol") else None,
                "price": price_val,
                "quantity": qty_val,
                "status": clean_api_text(tx.get("status", "posted")),
                "category_id": tx.get("categoryId"),
                "category": clean_api_text(tx.get("categoryName") or tx.get("category") or ""),
                "category_name": clean_api_text(tx.get("categoryName") or tx.get("category") or ""),
            })

        return DashboardTransactions(
            start_date=resp_start,
            end_date=resp_end,
            total_transactions=len(normalized_txs),
            money_in=money_in,
            money_out=money_out,
            net_cashflow=net_cashflow,
            transactions=normalized_txs,
            mode="live",
            raw_response=result,
        )

    def fetch_histories(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        user_account_ids: Optional[Union[str, List[Union[str, int]], Tuple[Union[str, int], ...], Set[Union[str, int]]]] = None,
    ) -> DashboardHistories:
        """Fetch historical daily balance and net worth curve directly from Empower."""
        if self.mock_mode:
            return self._generate_mock_histories(
                start_date=start_date,
                end_date=end_date,
                user_account_ids=user_account_ids,
            )

        payload: Dict[str, Union[str, int, List[Union[str, int]], Tuple[Union[str, int], ...], Set[Union[str, int]]]] = {}
        if start_date:
            payload["startDate"] = start_date
        if end_date:
            payload["endDate"] = end_date
        if user_account_ids is not None:
            payload["userAccountIds"] = user_account_ids

        try:
            result = self.fetch("/account/getHistories", data=payload)
        except SessionExpiredError:
            raise
        except Exception as e:
            raise EmpowerError(f"Failed to fetch account histories from dashboard: {e}") from e

        # Response normalization is part of this public API's guarded path: a
        # malformed numeric field (e.g. "N/A" in totalAssets) must surface as a
        # domain EmpowerError rather than a raw ValueError, while a genuine
        # SessionExpiredError keeps propagating untouched.
        try:
            sp_data = result.get("spData", {})
            resp_start = clean_api_text(sp_data.get("startDate") or start_date or "")
            resp_end = clean_api_text(sp_data.get("endDate") or end_date or "")
            raw_hist = sp_data.get("histories", [])

            normalized_histories = []
            for entry in raw_hist:
                # Textual history fields may carry upstream mojibake; sanitize the
                # date and account-balance keys before they enter the payload.
                date_str = clean_api_text(entry.get("date"))
                if not date_str:
                    continue

                total_assets = entry.get("totalAssets")
                if total_assets is None:
                    total_assets = entry.get("aggregateBalance")

                raw_balances = entry.get("balances", {})
                clean_balances: Dict[str, float] = {}
                if isinstance(raw_balances, dict):
                    for k, v in raw_balances.items():
                        if not str(k).endswith("Annotation") and isinstance(v, (int, float)):
                            clean_balances[clean_api_text(k)] = float(v)

                if total_assets is None and clean_balances:
                    # Sum only positive balances: negative entries are liability
                    # accounts, and including them would yield net worth rather than
                    # total assets (and double-count liabilities in net_worth below).
                    total_assets = sum(v for v in clean_balances.values() if v > 0)

                total_assets_val = float(total_assets or 0.0)
                total_liab_val = abs(float(entry.get("totalLiabilities") or 0.0))
                if entry.get("totalLiabilities") is None and clean_balances:
                    total_liab_val = sum(-v for v in clean_balances.values() if v < 0)
                net_worth_val = (
                    float(entry.get("netWorth"))
                    if entry.get("netWorth") is not None
                    else (total_assets_val - total_liab_val)
                )

                normalized_histories.append({
                    "date": date_str,
                    "net_worth": net_worth_val,
                    "total_assets": total_assets_val,
                    "total_liabilities": total_liab_val,
                    "balances": clean_balances,
                })
        except SessionExpiredError:
            raise
        except Exception as e:
            raise EmpowerError(f"Failed to normalize account histories from dashboard: {e}") from e

        return DashboardHistories(
            start_date=resp_start,
            end_date=resp_end,
            total_points=len(normalized_histories),
            histories=normalized_histories,
            mode="live",
            raw_response=result,
        )

    # --------------------------------------------------------------------------
    # Synthetic Benchmark Mock Generators (100% PII-Free)
    # --------------------------------------------------------------------------

    def _generate_mock_balances(self) -> DashboardBalances:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        accounts = [
            {
                "account_id": "ACC-INV-001",
                "account_name": "Horizon 401(k) Retirement Plan",
                "firm_name": "Horizon Investments",
                "account_type": "INVESTMENT",
                "balance": 350000.00,
                "is_asset": True,
                "currency": "USD",
                "last_refreshed": f"{today_str}T08:00:00Z",
                "user_account_id": 1001,
            },
            {
                "account_id": "ACC-IRA-002",
                "account_name": "Pinnacle Traditional IRA",
                "firm_name": "Pinnacle Brokerage",
                "account_type": "INVESTMENT",
                "balance": 125000.00,
                "is_asset": True,
                "currency": "USD",
                "last_refreshed": f"{today_str}T08:00:00Z",
                "user_account_id": 1002,
            },
            {
                "account_id": "ACC-CHK-003",
                "account_name": "Apex Premier Checking",
                "firm_name": "Apex Bank",
                "account_type": "BANK",
                "balance": 24500.00,
                "is_asset": True,
                "currency": "USD",
                "last_refreshed": f"{today_str}T08:00:00Z",
                "user_account_id": 1003,
            },
            {
                "account_id": "ACC-SAV-004",
                "account_name": "Summit High Yield Savings",
                "firm_name": "Summit Federal",
                "account_type": "BANK",
                "balance": 55000.00,
                "is_asset": True,
                "currency": "USD",
                "last_refreshed": f"{today_str}T08:00:00Z",
                "user_account_id": 1004,
            },
            {
                "account_id": "ACC-CRD-005",
                "account_name": "Beacon Preferred Credit Card",
                "firm_name": "Beacon Card Services",
                "account_type": "CREDIT_CARD",
                "balance": 2450.00,
                "is_asset": False,
                "currency": "USD",
                "last_refreshed": f"{today_str}T08:00:00Z",
                "user_account_id": 1005,
            },
        ]

        total_inv = 350000.00 + 125000.00
        total_cash = 24500.00 + 55000.00
        total_cc = 2450.00
        net_worth = (total_inv + total_cash) - total_cc

        return DashboardBalances(
            as_of_date=today_str,
            net_worth=net_worth,
            total_cash=total_cash,
            total_investment=total_inv,
            total_credit_card=total_cc,
            total_loan=0.00,
            total_mortgage=0.00,
            accounts=accounts,
            mode="sandbox_mock",
        )

    def _generate_mock_holdings(self) -> DashboardHoldings:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        mock_holdings = [
            {
                "user_account_id": 1001,
                "account_name": "Horizon 401(k) Retirement Plan",
                "ticker": "VTI",
                "cusip": "922908769",
                "description": "Vanguard Total Stock Market ETF",
                "holding_type": "ETF",
                "quantity": 1000.0,
                "price": 275.50,
                "value": 275500.00,
                "cost_basis": 200000.00,
                "holding_percentage": 58.0,
                "one_day_percent_change": 0.45,
                "one_day_value_change": 1239.75,
            },
            {
                "user_account_id": 1001,
                "account_name": "Horizon 401(k) Retirement Plan",
                "ticker": "BND",
                "cusip": "921937835",
                "description": "Vanguard Total Bond Market ETF",
                "holding_type": "ETF",
                "quantity": 1020.0,
                "price": 73.00,
                "value": 74500.00,
                "cost_basis": 75000.00,
                "holding_percentage": 15.7,
                "one_day_percent_change": 0.05,
                "one_day_value_change": 37.25,
            },
            {
                "user_account_id": 1002,
                "account_name": "Pinnacle Traditional IRA",
                "ticker": "SPY",
                "cusip": "78462F103",
                "description": "SPDR S&P 500 ETF Trust",
                "holding_type": "ETF",
                "quantity": 150.0,
                "price": 575.20,
                "value": 86280.00,
                "cost_basis": 65000.00,
                "holding_percentage": 18.2,
                "one_day_percent_change": 0.52,
                "one_day_value_change": 448.65,
            },
            {
                "user_account_id": 1002,
                "account_name": "Pinnacle Traditional IRA",
                "ticker": "AAPL",
                "cusip": "037833100",
                "description": "Apple Inc.",
                "holding_type": "Stock",
                "quantity": 172.0,
                "price": 225.40,
                "value": 38768.80,
                "cost_basis": 25000.00,
                "holding_percentage": 8.1,
                "one_day_percent_change": 1.12,
                "one_day_value_change": 434.21,
            },
        ]
        total_val = sum(h["value"] for h in mock_holdings)
        return DashboardHoldings(
            as_of_date=today_str,
            total_value=total_val,
            holdings=mock_holdings,
            mode="sandbox_mock",
        )

    def _generate_mock_transactions(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> DashboardTransactions:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        end = end_date or today_str
        mock_txs = [
            {
                "user_transaction_id": "TXN-001",
                "account_id": "ACC-CHK-003",
                "user_account_id": 1003,
                "account_name": "Apex Premier Checking",
                "transaction_date": f"{today_str[:7]}-15",
                "description": "Payroll Direct Deposit",
                "original_description": "ACH DIRECT DEPOSIT PAYROLL",
                "amount": 4500.00,
                "is_credit": True,
                "is_cash_in": True,
                "is_cash_out": False,
                "is_income": True,
                "is_spending": False,
                "transaction_type": "Deposit",
                "investment_type": None,
                "symbol": None,
                "price": None,
                "quantity": None,
                "status": "posted",
                "category_id": 1,
                "category": "Paycheck",
                "category_name": "Paycheck",
            },
            {
                "user_transaction_id": "TXN-002",
                "account_id": "ACC-INV-001",
                "user_account_id": 1001,
                "account_name": "Horizon 401(k) Retirement Plan",
                "transaction_date": f"{today_str[:7]}-15",
                "description": "Elective Payroll Contribution",
                "original_description": "PLAN ELECTIVE CONTRIB",
                "amount": 750.00,
                "is_credit": True,
                "is_cash_in": True,
                "is_cash_out": False,
                "is_income": False,
                "is_spending": False,
                "transaction_type": "Contribution",
                "investment_type": "Contribution",
                "symbol": None,
                "price": None,
                "quantity": None,
                "status": "posted",
                "category_id": 80,
                "category": "Retirement Contribution",
                "category_name": "Retirement Contribution",
            },
            {
                "user_transaction_id": "TXN-003",
                "account_id": "ACC-IRA-002",
                "user_account_id": 1002,
                "account_name": "Pinnacle Traditional IRA",
                "transaction_date": f"{today_str[:7]}-10",
                "description": "Dividend Received — SPDR S&P 500 ETF (SPY)",
                "original_description": "DIVIDEND ON SPY",
                "amount": 125.50,
                "is_credit": True,
                "is_cash_in": True,
                "is_cash_out": False,
                "is_income": True,
                "is_spending": False,
                "transaction_type": "Dividend Received",
                "investment_type": "Dividend",
                "symbol": "SPY",
                "price": 0.0,
                "quantity": 125.50,
                "status": "posted",
                "category_id": 69,
                "category": "Dividends",
                "category_name": "Dividends",
            },
            {
                "user_transaction_id": "TXN-004",
                "account_id": "ACC-CRD-005",
                "user_account_id": 1005,
                "account_name": "Beacon Preferred Credit Card",
                "transaction_date": f"{today_str[:7]}-05",
                "description": "Neighborhood Grocery Market",
                "original_description": "GROCERY MARKET MAIN ST",
                "amount": 182.40,
                "is_credit": False,
                "is_cash_in": False,
                "is_cash_out": True,
                "is_income": False,
                "is_spending": True,
                "transaction_type": "Purchase",
                "investment_type": None,
                "symbol": None,
                "price": None,
                "quantity": None,
                "status": "posted",
                "category_id": 12,
                "category": "Groceries",
                "category_name": "Groceries",
            },
        ]
        dates = [t.get("transaction_date") for t in mock_txs if t.get("transaction_date")]
        start = start_date or (min(dates) if dates else f"{datetime.now(timezone.utc).year}-01-01")

        if limit and limit > 0:
            mock_txs = mock_txs[:limit]

        money_in = sum(t["amount"] for t in mock_txs if t["is_cash_in"])
        money_out = sum(t["amount"] for t in mock_txs if t["is_cash_out"])
        net = money_in - money_out

        return DashboardTransactions(
            start_date=start,
            end_date=end,
            total_transactions=len(mock_txs),
            money_in=money_in,
            money_out=money_out,
            net_cashflow=net,
            transactions=mock_txs,
            mode="sandbox_mock",
        )

    def _generate_mock_histories(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        user_account_ids: Optional[Union[str, List[Union[str, int]], Tuple[Union[str, int], ...], Set[Union[str, int]]]] = None,
    ) -> DashboardHistories:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = start_date or "2024-01-01"
        end = end_date or today_str

        # Emit points at the start, the midpoint, and the end of the requested
        # range so offline callers observe the same [start, end] range semantics
        # as live retrieval (rather than fixed calendar dates in the start year).
        def _midpoint(s: str, e: str) -> str:
            try:
                ds = datetime.strptime(s, "%Y-%m-%d").date()
                de = datetime.strptime(e, "%Y-%m-%d").date()
                if de < ds:
                    de = ds
                return (ds + (de - ds) / 2).strftime("%Y-%m-%d")
            except ValueError:
                return s

        mid = _midpoint(start, end)

        mock_histories = [
            {
                "date": start,
                "net_worth": 485000.0,
                "total_assets": 487500.0,
                "total_liabilities": 2500.0,
                "balances": {
                    "ACC-INV-001": 310000.0,
                    "ACC-IRA-002": 115000.0,
                    "ACC-CHK-003": 20000.0,
                    "ACC-SAV-004": 42500.0,
                    "ACC-CRD-005": -2500.0,
                },
            },
            {
                "date": mid,
                "net_worth": 512000.0,
                "total_assets": 514200.0,
                "total_liabilities": 2200.0,
                "balances": {
                    "ACC-INV-001": 330000.0,
                    "ACC-IRA-002": 120000.0,
                    "ACC-CHK-003": 22000.0,
                    "ACC-SAV-004": 42200.0,
                    "ACC-CRD-005": -2200.0,
                },
            },
            {
                "date": end,
                "net_worth": 552050.0,
                "total_assets": 554500.0,
                "total_liabilities": 2450.0,
                "balances": {
                    "ACC-INV-001": 350000.0,
                    "ACC-IRA-002": 125000.0,
                    "ACC-CHK-003": 24500.0,
                    "ACC-SAV-004": 55000.0,
                    "ACC-CRD-005": -2450.0,
                },
            },
        ]

        # Collapse points that resolve to the same calendar date so a short
        # requested range yields distinct, internally consistent daily
        # snapshots instead of several contradictory values on one date: a
        # one-day request makes start == mid == end, and a two-day request can
        # make start == mid. The last candidate for a date wins, keeping the
        # freshest snapshot for that day.
        collapsed: Dict[str, Dict[str, Union[str, float, Dict[str, float]]]] = {}
        for point in mock_histories:
            collapsed[point["date"]] = point
        mock_histories = list(collapsed.values())

        # Honour an account filter so a scoped history request does not leak
        # every account's balance; recompute aggregates from the kept balances.
        requested_user_ids = self._normalize_account_filter(user_account_ids)
        if requested_user_ids is not None:
            account_id_map = {
                "1001": "ACC-INV-001",
                "1002": "ACC-IRA-002",
                "1003": "ACC-CHK-003",
                "1004": "ACC-SAV-004",
                "1005": "ACC-CRD-005",
            }
            requested_account_ids = {account_id_map.get(uid, uid) for uid in requested_user_ids}
            for point in mock_histories:
                kept = {k: v for k, v in point["balances"].items() if k in requested_account_ids}
                point["balances"] = kept
                point["total_assets"] = sum(v for v in kept.values() if v > 0)
                point["total_liabilities"] = sum(-v for v in kept.values() if v < 0)
                point["net_worth"] = point["total_assets"] - point["total_liabilities"]

        return DashboardHistories(
            start_date=start,
            end_date=end,
            total_points=len(mock_histories),
            histories=mock_histories,
            mode="sandbox_mock",
        )

    @staticmethod
    def _normalize_account_filter(user_account_ids: Optional[Union[str, List[Union[str, int]], Tuple[Union[str, int], ...], Set[Union[str, int]]]]) -> Optional[Set[str]]:
        """Normalize a userAccountIds filter (list, tuple, or CSV string) to a set of ids.

        Returns None if filter is omitted, empty set if filter is explicitly empty.
        """
        if user_account_ids is None:
            return None
        if isinstance(user_account_ids, str):
            normalized = {part.strip() for part in user_account_ids.split(",") if part.strip()}
            return normalized if normalized else set()
        if isinstance(user_account_ids, (list, tuple, set)):
            normalized = {str(x).strip() for x in user_account_ids if str(x).strip()}
            return normalized if normalized else set()
        return {str(user_account_ids).strip()}

