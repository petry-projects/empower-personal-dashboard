"""Model Context Protocol (MCP) server for Empower Personal Dashboard.

Exposes aggregated financial data (net worth, balances, holdings, transactions)
to AI agents (Claude Desktop, Antigravity CLI, Cursor, Windsurf) over stdio or SSE.

Security Model:
    Out-of-band authentication: The user authenticates once via `empower --login`
    in their local terminal. The MCP server runs headlessly using the persisted
    session token (~/.empower_personal_dashboard_session.json, mode 0600).
    Passwords and 2FA SMS/Email codes are NEVER accepted or routed through the LLM context.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from empower_personal_dashboard.cli import (
    export_balances_csv,
    export_holdings_csv,
    export_transactions_csv,
    format_currency,
    render_balances_markdown,
    render_balances_table,
    render_holdings_markdown,
    render_holdings_table,
    render_transactions_markdown,
    render_transactions_table,
)
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

logger = logging.getLogger("empower_personal_dashboard.mcp")

# Compatibility import across MCP SDK versions (mcp 2.x MCPServer vs mcp 1.x FastMCP)
try:
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        FastMCP = None  # type: ignore[assignment,misc]


def _mask_account_identifier(ident: Any) -> str:
    """Mask sensitive account numbers or identifiers to only show last 4 characters."""
    if not ident:
        return ""
    s = str(ident).strip()
    if len(s) <= 4:
        return s
    return f"****{s[-4:]}"


def _sanitize_account_dict(acct: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize account record for safe presentation to LLMs."""
    clean = dict(acct)
    if "account_number" in clean and clean["account_number"]:
        clean["account_number"] = _mask_account_identifier(clean["account_number"])
    return clean


class _DataCache:
    """Thread-safe, lightweight in-memory TTL cache to prevent aggressive upstream polling."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl = ttl_seconds
        self.balances: Optional[tuple[float, DashboardBalances]] = None
        self.holdings: Optional[tuple[float, DashboardHoldings]] = None
        self.transactions: Dict[str, tuple[float, DashboardTransactions]] = {}

    def get_balances(self) -> Optional[DashboardBalances]:
        if self.balances and (time.time() - self.balances[0] < self.ttl):
            return self.balances[1]
        return None

    def set_balances(self, data: DashboardBalances) -> None:
        self.balances = (time.time(), data)

    def get_holdings(self) -> Optional[DashboardHoldings]:
        if self.holdings and (time.time() - self.holdings[0] < self.ttl):
            return self.holdings[1]
        return None

    def set_holdings(self, data: DashboardHoldings) -> None:
        self.holdings = (time.time(), data)

    def get_transactions(self, key: str) -> Optional[DashboardTransactions]:
        if key in self.transactions:
            ts, data = self.transactions[key]
            if time.time() - ts < self.ttl:
                return data
        return None

    def set_transactions(self, key: str, data: DashboardTransactions) -> None:
        self.transactions[key] = (time.time(), data)


def create_mcp_server(
    client: Optional[EmpowerDashboardClient] = None,
    cache_ttl_seconds: int = 300,
) -> Any:
    """Create and configure the FastMCP server instance.

    Args:
        client: Optional pre-configured EmpowerDashboardClient (useful for testing).
        cache_ttl_seconds: Time-to-live for in-memory balance/holding caches (default: 5 min).

    Returns:
        Configured FastMCP / MCPServer instance.
    """
    if FastMCP is None:
        raise ImportError(
            "The 'mcp' package is required to create the MCP server. "
            "Install it via: pip install 'empower-personal-dashboard[mcp]'"
        )

    server = FastMCP(
        "Empower Personal Dashboard",
        instructions=(
            "You have access to the user's aggregated financial data from Empower Personal Dashboard "
            "(formerly Personal Capital). Use these tools to query balances, net worth, investment holdings, "
            "and transactions. Never reveal or request master account passwords."
        ),
    )

    # Initialize client and cache
    empower_client = client or EmpowerDashboardClient()
    cache = _DataCache(ttl_seconds=cache_ttl_seconds)

    last_session_mtime: float = 0.0
    if not empower_client.mock_mode and empower_client.session_file.exists():
        try:
            last_session_mtime = empower_client.session_file.stat().st_mtime
        except Exception:
            pass

    def _ensure_session_fresh() -> None:
        nonlocal last_session_mtime
        if empower_client.mock_mode:
            return
        session_file = empower_client.session_file
        if session_file.exists():
            try:
                mtime = session_file.stat().st_mtime
                if mtime > last_session_mtime:
                    empower_client.load_session(session_file)
                    last_session_mtime = mtime
                    # Invalidate in-memory caches since session re-authenticated
                    cache.balances = None
                    cache.holdings = None
                    cache.transactions.clear()
            except Exception as e:
                logger.warning(f"Failed to refresh session from {session_file}: {e}")

    def _fetch_balances_cached() -> DashboardBalances:
        _ensure_session_fresh()
        cached = cache.get_balances()
        if cached:
            return cached
        try:
            fresh = empower_client.fetch_balances()
        except (SessionExpiredError, RequireTwoFactorException):
            _ensure_session_fresh()
            cache.balances = None
            raise
        cache.set_balances(fresh)
        return fresh

    def _fetch_holdings_cached() -> DashboardHoldings:
        _ensure_session_fresh()
        cached = cache.get_holdings()
        if cached:
            return cached
        try:
            fresh = empower_client.fetch_holdings()
        except (SessionExpiredError, RequireTwoFactorException):
            _ensure_session_fresh()
            cache.holdings = None
            raise
        cache.set_holdings(fresh)
        return fresh

    def _fetch_transactions_cached(
        start_date: Optional[str],
        end_date: Optional[str],
        limit: Optional[int] = None,
    ) -> DashboardTransactions:
        _ensure_session_fresh()
        key = f"{start_date}_{end_date}_{limit}"
        cached = cache.get_transactions(key)
        if cached:
            return cached
        try:
            fresh = empower_client.fetch_transactions(start_date=start_date, end_date=end_date, limit=limit)
        except (SessionExpiredError, RequireTwoFactorException):
            _ensure_session_fresh()
            raise
        cache.set_transactions(key, fresh)
        return fresh

    # -------------------------------------------------------------------------
    # Tools
    # -------------------------------------------------------------------------

    @server.tool()
    def get_net_worth_summary(format: str = "json") -> Dict[str, Any]:
        """Get an ultra-lightweight summary of current net worth and totals by asset class.

        Designed to minimize context window consumption (~100 tokens).

        Args:
            format: Output format ('json', 'markdown', or 'table'). Default: 'json'.

        Returns:
            Dict containing net_worth, total_cash, total_investment, total_credit,
            total_mortgage, total_loan, total_other_assets, total_other_liabilities,
            and total linked account count. If format is 'markdown' or 'table',
            includes a formatted string under 'formatted_output'.
        """
        try:
            balances = _fetch_balances_cached()
            res: Dict[str, Any] = {
                "status": "success",
                "net_worth": balances.net_worth,
                "total_cash": balances.total_cash,
                "total_investment": balances.total_investment,
                "total_credit": balances.total_card_liabilities,
                "total_credit_card": balances.total_card_liabilities,
                "total_mortgage": balances.total_mortgage,
                "total_loan": balances.total_loan,
                "total_other_assets": balances.total_other_assets,
                "total_other_liabilities": balances.total_other_liabilities,
                "accounts_count": len(balances.accounts),
            }
            fmt = format.lower()
            if fmt == "markdown":
                res["formatted_output"] = (
                    f"### Empower Net Worth Summary ({balances.as_of_date})\n\n"
                    f"- **Net Worth**: **{format_currency(balances.net_worth)}**\n"
                    f"- **Total Investments**: {format_currency(balances.total_investment)}\n"
                    f"- **Total Cash / Banking**: {format_currency(balances.total_cash)}\n"
                    f"- **Real Estate & Physical Assets**: {format_currency(balances.total_other_assets)}\n"
                    f"- **Credit Card Liabilities**: -{format_currency(balances.total_card_liabilities)}\n"
                    f"- **Mortgages & Loans**: -{format_currency(balances.total_mortgage + balances.total_loan)}\n"
                    f"- **Linked Accounts**: {len(balances.accounts)}"
                )
            elif fmt == "table":
                lines = [
                    f"### Empower Net Worth Summary ({balances.as_of_date})",
                    "",
                    "| Category | Amount |",
                    "| :--- | ---: |",
                    f"| Net Worth | **{format_currency(balances.net_worth)}** |",
                    f"| Total Investments | {format_currency(balances.total_investment)} |",
                    f"| Total Cash / Banking | {format_currency(balances.total_cash)} |",
                    f"| Real Estate & Physical Assets | {format_currency(balances.total_other_assets)} |",
                    f"| Credit Card Liabilities | -{format_currency(balances.total_card_liabilities)} |",
                    f"| Mortgages & Loans | -{format_currency(balances.total_mortgage + balances.total_loan)} |",
                    f"| Linked Accounts | {len(balances.accounts)} |",
                ]
                res["formatted_output"] = "\n".join(lines)
            return res
        except (SessionExpiredError, RequireTwoFactorException, FileNotFoundError):
            return {
                "status": "error",
                "error_code": "AUTH_REQUIRED",
                "message": (
                    "Empower session has expired or is not initialized. "
                    "Please run 'empower --login' in your local terminal to re-authenticate with 2FA."
                ),
            }
        except EmpowerError as e:
            return {"status": "error", "error_code": "API_ERROR", "message": str(e)}
        except Exception as e:
            logger.exception("Unexpected error in get_net_worth_summary")
            return {"status": "error", "error_code": "INTERNAL_ERROR", "message": str(e)}

    @server.tool()
    def get_balances(
        account_types: Optional[List[str]] = None,
        include_inactive: bool = False,
        format: str = "json",
    ) -> Dict[str, Any]:
        """Get account balances grouped by type (CASH, INVESTMENT, CREDIT, MORTGAGE, LOAN).

        Account numbers are masked by default to protect personal identifiers.

        Args:
            account_types: Optional list of account types to filter by (e.g. ['CASH', 'INVESTMENT']).
            include_inactive: Whether to include closed or zero-balance inactive accounts (default: False).
            format: Output format ('json', 'markdown', or 'table'). Default: 'json'.

        Returns:
            Dict containing net worth, totals by type, accounts count, and sanitized accounts list.
            If format is 'markdown' or 'table', includes formatted table string under 'formatted_output'.
        """
        try:
            balances = _fetch_balances_cached()

            acct_filter: set[str] = set()
            if account_types:
                for t in account_types:
                    t_up = t.strip().upper()
                    if t_up in ("CASH", "BANK", "CHECKING", "SAVINGS"):
                        acct_filter.update(["BANK", "CASH", "CHECKING", "SAVINGS"])
                    elif t_up in ("CREDIT", "CREDIT_CARD", "CREDITCARD"):
                        acct_filter.update(["CREDIT", "CREDIT_CARD"])
                    elif t_up in ("INVESTMENT", "INVESTMENTS", "BROKERAGE", "IRA", "401K"):
                        acct_filter.update(["INVESTMENT", "INVESTMENTS"])
                    elif t_up in ("MORTGAGE", "MORTGAGES"):
                        acct_filter.update(["MORTGAGE", "MORTGAGES"])
                    elif t_up in ("LOAN", "LOANS"):
                        acct_filter.update(["LOAN", "LOANS"])
                    else:
                        acct_filter.add(t_up)

            filtered: List[Dict[str, Any]] = []
            for a in balances.accounts:
                acct_type = str(a.get("account_type", "")).upper()
                if acct_filter and acct_type not in acct_filter:
                    continue
                if not include_inactive and a.get("is_closed"):
                    continue
                filtered.append(_sanitize_account_dict(a))

            if account_types is not None:
                cash_total = sum(a.get("balance", 0.0) for a in filtered if str(a.get("account_type", "")).upper() in ("BANK", "CASH", "CHECKING", "SAVINGS"))
                inv_total = sum(a.get("balance", 0.0) for a in filtered if str(a.get("account_type", "")).upper() in ("INVESTMENT", "INVESTMENTS"))
                cc_total = sum(a.get("balance", 0.0) for a in filtered if str(a.get("account_type", "")).upper() in ("CREDIT", "CREDIT_CARD"))
                mort_total = sum(a.get("balance", 0.0) for a in filtered if str(a.get("account_type", "")).upper() in ("MORTGAGE", "MORTGAGES"))
                loan_total = sum(a.get("balance", 0.0) for a in filtered if str(a.get("account_type", "")).upper() in ("LOAN", "LOANS"))
                other_assets_total = sum(a.get("balance", 0.0) for a in filtered if a.get("is_asset", True) and str(a.get("account_type", "")).upper() not in ("BANK", "CASH", "CHECKING", "SAVINGS", "INVESTMENT", "INVESTMENTS"))
                other_liab_total = sum(a.get("balance", 0.0) for a in filtered if not a.get("is_asset", True) and str(a.get("account_type", "")).upper() not in ("CREDIT", "CREDIT_CARD", "MORTGAGE", "MORTGAGES", "LOAN", "LOANS"))
                totals_by_type = {
                    "cash": round(cash_total, 2),
                    "investment": round(inv_total, 2),
                    "credit": round(cc_total, 2),
                    "credit_card": round(cc_total, 2),
                    "mortgage": round(mort_total, 2),
                    "loan": round(loan_total, 2),
                    "other_assets": round(other_assets_total, 2),
                    "other_liabilities": round(other_liab_total, 2),
                }
            else:
                totals_by_type = {
                    "cash": balances.total_cash,
                    "investment": balances.total_investment,
                    "credit": balances.total_card_liabilities,
                    "credit_card": balances.total_card_liabilities,
                    "mortgage": balances.total_mortgage,
                    "loan": balances.total_loan,
                    "other_assets": balances.total_other_assets,
                    "other_liabilities": balances.total_other_liabilities,
                }

            filtered_balances = DashboardBalances(
                as_of_date=balances.as_of_date,
                net_worth=balances.net_worth,
                total_cash=totals_by_type["cash"],
                total_investment=totals_by_type["investment"],
                total_card_liabilities=totals_by_type["credit_card"],
                total_loan=totals_by_type["loan"],
                total_mortgage=totals_by_type["mortgage"],
                total_other_assets=totals_by_type["other_assets"],
                total_other_liabilities=totals_by_type["other_liabilities"],
                accounts=filtered,
                mode=balances.mode,
            )

            res = {
                "status": "success",
                "net_worth": balances.net_worth,
                "totals_by_type": totals_by_type,
                "accounts_count": len(filtered),
                "accounts": filtered,
            }
            if format.lower() == "markdown":
                res["formatted_output"] = render_balances_markdown(filtered_balances)
            elif format.lower() == "table":
                res["formatted_output"] = render_balances_table(filtered_balances)
            return res
        except (SessionExpiredError, RequireTwoFactorException, FileNotFoundError):
            return {
                "status": "error",
                "error_code": "AUTH_REQUIRED",
                "message": (
                    "Empower session has expired or is not initialized. "
                    "Please run 'empower --login' in your local terminal to re-authenticate with 2FA."
                ),
            }
        except EmpowerError as e:
            return {"status": "error", "error_code": "API_ERROR", "message": str(e)}
        except Exception as e:
            logger.exception("Unexpected error in get_balances")
            return {"status": "error", "error_code": "INTERNAL_ERROR", "message": str(e)}

    @server.tool()
    def get_holdings(
        ticker: Optional[str] = None,
        account_id: Optional[int] = None,
        aggregate_by_ticker: bool = False,
        sort_by: str = "value",
        min_value: float = 0.0,
        limit: int = 50,
        format: str = "json",
    ) -> Dict[str, Any]:
        """Query investment portfolio holdings, top positions, allocations, and quantities across all accounts.

        Use this tool when asked:
        - 'What are our top investment holdings?'
        - 'How much Apple (AAPL) or Vanguard (VTI) do we own?'
        - 'Show my investment portfolio breakdown or asset allocation.'
        - 'What positions are held in our 401(k), IRA, 529, or brokerage accounts?'

        Args:
            ticker: Optional ticker symbol to filter by (e.g. 'VTI', 'AAPL').
            account_id: Optional account ID to inspect a specific investment account.
            aggregate_by_ticker: Whether to consolidate the same ticker held across multiple accounts into a single total position (default: False). Set True when looking for top household holdings.
            sort_by: Field to sort by ('value', 'ticker', 'weight', 'shares'). Default: 'value'.
            min_value: Minimum position dollar value to include (default: 0.0). Useful to filter out fractional dust lots.
            limit: Maximum number of holdings to return (1-200, default: 50).
            format: Output format ('json', 'markdown', or 'table'). Default: 'json'.

        Returns:
            Dict containing total portfolio value, matching positions count, holdings list,
            and optionally 'formatted_output' if format is 'markdown' or 'table'.
        """
        try:
            clamped_limit = min(max(1, limit), 200)
            holdings = _fetch_holdings_cached()

            filtered: List[Dict[str, Any]] = []
            ticker_upper = ticker.strip().upper() if ticker else None

            for pos in holdings.holdings:
                if ticker_upper:
                    pos_ticker = str(pos.get("ticker") or "").upper()
                    if pos_ticker != ticker_upper:
                        continue
                if account_id is not None:
                    pos_acct_id = pos.get("user_account_id") if pos.get("user_account_id") is not None else pos.get("account_id")
                    if pos_acct_id != account_id and str(pos_acct_id) != str(account_id):
                        continue
                filtered.append(dict(pos))

            if aggregate_by_ticker:
                grouped: Dict[str, Dict[str, Any]] = {}
                for pos in filtered:
                    sym = (pos.get("ticker") or pos.get("description") or "UNKNOWN").strip().upper()
                    if sym not in grouped:
                        grouped[sym] = {
                            "ticker": pos.get("ticker") or "",
                            "cusip": pos.get("cusip") or "",
                            "description": pos.get("description") or "",
                            "holding_type": pos.get("holding_type") or "OTHER",
                            "quantity": 0.0,
                            "price": pos.get("price") or 0.0,
                            "value": 0.0,
                            "cost_basis": 0.0,
                            "accounts": [],
                        }
                    g = grouped[sym]
                    qty = pos.get("quantity") or 0.0
                    val = pos.get("value") or 0.0
                    cb = pos.get("cost_basis") or 0.0
                    g["quantity"] += qty
                    g["value"] += val
                    g["cost_basis"] += cb
                    acct_id_val = pos.get("user_account_id") if pos.get("user_account_id") is not None else pos.get("account_id")
                    g["accounts"].append({
                        "account_name": pos.get("account_name"),
                        "account_id": acct_id_val,
                        "user_account_id": acct_id_val,
                        "quantity": qty,
                        "value": val,
                    })
                aggregated_list = list(grouped.values())
                for item in aggregated_list:
                    if holdings.total_value > 0:
                        item["holding_percentage"] = round((item["value"] / holdings.total_value) * 100, 2)
                    else:
                        item["holding_percentage"] = 0.0
                    item["accounts_count"] = len(item["accounts"])
                    item["account_name"] = f"Held in {len(item['accounts'])} account(s)"
                filtered = aggregated_list

            if min_value > 0.0:
                filtered = [h for h in filtered if (h.get("value") or 0.0) >= min_value]

            sort_key = sort_by.strip().lower()
            if sort_key == "value":
                filtered.sort(key=lambda x: x.get("value") or 0.0, reverse=True)
            elif sort_key == "ticker":
                filtered.sort(key=lambda x: str(x.get("ticker") or ""))
            elif sort_key == "weight":
                filtered.sort(key=lambda x: x.get("holding_percentage") or 0.0, reverse=True)
            elif sort_key in ("shares", "quantity"):
                filtered.sort(key=lambda x: x.get("quantity") or 0.0, reverse=True)

            returned_holdings = filtered[:clamped_limit]
            res: Dict[str, Any] = {
                "status": "success",
                "total_portfolio_value": holdings.total_value,
                "total_positions_available": len(holdings.holdings),
                "matching_count": len(filtered),
                "returned_count": len(returned_holdings),
                "holdings": returned_holdings,
            }

            if format.lower() in ("markdown", "table"):
                temp_holdings = DashboardHoldings(
                    total_value=holdings.total_value,
                    holdings=returned_holdings,
                    as_of_date=holdings.as_of_date,
                    mode=holdings.mode,
                )
                if format.lower() == "markdown":
                    res["formatted_output"] = render_holdings_markdown(temp_holdings, limit=clamped_limit, preserve_order=True)
                else:
                    res["formatted_output"] = render_holdings_table(temp_holdings, limit=clamped_limit, preserve_order=True)

            return res
        except (SessionExpiredError, RequireTwoFactorException, FileNotFoundError):
            return {
                "status": "error",
                "error_code": "AUTH_REQUIRED",
                "message": (
                    "Empower session has expired or is not initialized. "
                    "Please run 'empower --login' in your local terminal to re-authenticate with 2FA."
                ),
            }
        except EmpowerError as e:
            return {"status": "error", "error_code": "API_ERROR", "message": str(e)}
        except Exception as e:
            logger.exception("Unexpected error in get_holdings")
            return {"status": "error", "error_code": "INTERNAL_ERROR", "message": str(e)}

    @server.tool()
    def get_transactions(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: Optional[int] = None,
        account_id: Optional[int] = None,
        category: Optional[str] = None,
        spending_only: bool = False,
        income_only: bool = False,
        transaction_type: Optional[str] = None,
        min_amount: Optional[float] = None,
        max_amount: Optional[float] = None,
        limit: int = 50,
        format: str = "json",
    ) -> Dict[str, Any]:
        """Query account activity, purchases, spending, deposits, payroll, dividends, and transfers.

        Use this tool when asked:
        - 'What did we purchase in the last 24 hours / last 7 days?'
        - 'Show recent credit card charges or grocery spending.'
        - 'Did our payroll deposit arrive?'
        - 'List all transactions for [Account].'

        Args:
            start_date: Earliest transaction date (YYYY-MM-DD). If omitted and 'days' is provided, computed automatically.
            end_date: Latest transaction date (YYYY-MM-DD). Defaults to today.
            days: Optional relative lookback in days from today (e.g. 1 for last 24 hours, 7 for last week, 30 for last month).
            account_id: Optional account ID filter.
            category: Optional category name filter (e.g. 'Groceries', 'Utilities').
            spending_only: Filter strictly for purchases/spending (excludes internal transfers, deposits, dividends). Default: False.
            income_only: Filter strictly for income/deposits. Default: False.
            transaction_type: Optional transaction type filter (e.g. 'Purchase', 'Payment', 'Deposit', 'Transfer', 'Dividend Received').
            min_amount: Minimum transaction dollar amount.
            max_amount: Maximum transaction dollar amount.
            limit: Maximum number of transactions to return (1-200, default: 50).
            format: Output format ('json', 'markdown', or 'table'). Default: 'json'.

        Returns:
            Dict containing total transaction count, net cashflow, matching count, transactions list,
            and optionally 'formatted_output' if format is 'markdown' or 'table'.
        """
        try:
            clamped_limit = min(max(1, limit), 200)

            if days is not None:
                if days < 0:
                    return {
                        "status": "error",
                        "error_code": "INVALID_ARGUMENT",
                        "message": "days must be non-negative",
                    }
                now_utc = datetime.now(timezone.utc)
                start_date = start_date or (now_utc - timedelta(days=days)).strftime("%Y-%m-%d")
                end_date = end_date or now_utc.strftime("%Y-%m-%d")

            has_post_filters = bool(
                account_id is not None
                or category
                or spending_only
                or income_only
                or transaction_type
                or min_amount is not None
                or max_amount is not None
            )

            fetch_limit = None if has_post_filters else clamped_limit
            tx_data = _fetch_transactions_cached(
                start_date=start_date,
                end_date=end_date,
                limit=fetch_limit,
            )

            filtered: List[Dict[str, Any]] = []
            category_lower = category.strip().lower() if category else None
            type_lower = transaction_type.strip().lower() if transaction_type else None

            for tx in tx_data.transactions:
                if account_id is not None:
                    tx_acct_id = tx.get("account_id")
                    tx_user_acct_id = tx.get("user_account_id")
                    if str(account_id) != str(tx_acct_id) and account_id != tx_user_acct_id:
                        continue
                if category_lower:
                    tx_cat = str(tx.get("category") or "").lower()
                    if category_lower not in tx_cat:
                        continue
                if spending_only and not tx.get("is_spending"):
                    continue
                if income_only and not tx.get("is_income"):
                    continue
                if type_lower:
                    tx_type = str(tx.get("transaction_type") or "").lower()
                    if type_lower not in tx_type:
                        continue
                amt = abs(tx.get("amount") or 0.0)
                if min_amount is not None and amt < min_amount:
                    continue
                if max_amount is not None and amt > max_amount:
                    continue

                filtered.append(tx)

            returned_tx = filtered[:clamped_limit]

            if has_post_filters:
                filtered_money_in = sum(
                    abs(t.get("amount", 0.0)) for t in filtered if t.get("is_cash_in") or t.get("is_credit") or t.get("is_income")
                )
                filtered_money_out = sum(
                    abs(t.get("amount", 0.0)) for t in filtered if t.get("is_cash_out") or t.get("is_spending")
                )
                filtered_net_cashflow = round(filtered_money_in - filtered_money_out, 2)
            else:
                filtered_money_in = tx_data.money_in
                filtered_money_out = tx_data.money_out
                filtered_net_cashflow = tx_data.net_cashflow

            res: Dict[str, Any] = {
                "status": "success",
                "total_transactions": len(filtered) if has_post_filters else tx_data.total_transactions,
                "net_cashflow": filtered_net_cashflow,
                "money_in": round(filtered_money_in, 2),
                "money_out": round(filtered_money_out, 2),
                "matching_count": len(filtered),
                "returned_count": len(returned_tx),
                "transactions": returned_tx,
            }
            if has_post_filters:
                res["period_net_cashflow"] = tx_data.net_cashflow
                res["period_total_transactions"] = tx_data.total_transactions

            if format.lower() in ("markdown", "table"):
                temp_tx = DashboardTransactions(
                    start_date=tx_data.start_date,
                    end_date=tx_data.end_date,
                    total_transactions=len(filtered) if has_post_filters else tx_data.total_transactions,
                    money_in=filtered_money_in,
                    money_out=filtered_money_out,
                    net_cashflow=filtered_net_cashflow,
                    transactions=returned_tx,
                    mode=tx_data.mode,
                )
                if format.lower() == "markdown":
                    res["formatted_output"] = render_transactions_markdown(temp_tx, limit=clamped_limit)
                else:
                    res["formatted_output"] = render_transactions_table(temp_tx, limit=clamped_limit)

            return res
        except (SessionExpiredError, RequireTwoFactorException, FileNotFoundError):
            return {
                "status": "error",
                "error_code": "AUTH_REQUIRED",
                "message": (
                    "Empower session has expired or is not initialized. "
                    "Please run 'empower --login' in your local terminal to re-authenticate with 2FA."
                ),
            }
        except EmpowerError as e:
            return {"status": "error", "error_code": "API_ERROR", "message": str(e)}
        except Exception as e:
            logger.exception("Unexpected error in get_transactions")
            return {"status": "error", "error_code": "INTERNAL_ERROR", "message": str(e)}

    @server.tool()
    def check_auth_status() -> Dict[str, Any]:
        """Check whether a valid local session exists for Empower Personal Dashboard.

        Returns:
            Dict containing authenticated boolean status, session path, and guidance.
        """
        _ensure_session_fresh()
        session_file = empower_client.session_file
        exists = session_file.exists()
        if not exists and not empower_client.mock_mode:
            return {
                "status": "unauthenticated",
                "authenticated": False,
                "session_path": str(session_file),
                "message": "No session file found. Run 'empower --login' in terminal to authenticate.",
            }

        try:
            # Quick validation check: try to fetch balances
            balances = _fetch_balances_cached()
            return {
                "status": "authenticated",
                "authenticated": True,
                "session_path": str(session_file),
                "net_worth": balances.net_worth,
                "accounts_count": len(balances.accounts),
                "message": "Session is active and valid.",
            }
        except (SessionExpiredError, RequireTwoFactorException):
            return {
                "status": "expired",
                "authenticated": False,
                "session_path": str(session_file),
                "message": "Session token has expired. Run 'empower --login' in terminal to re-authenticate.",
            }
        except Exception as e:
            return {
                "status": "error",
                "authenticated": False,
                "session_path": str(session_file),
                "message": f"Session verification error: {e}",
            }

    @server.tool()
    def export_data(
        destination_dir: str = "./data",
        scope: str = "all",
        export_csv: bool = True,
        export_beancount: bool = False,
        beancount_map_path: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Export Empower Personal Dashboard data to disk in JSON, JSONL, CSV, and Beancount formats.

        Directly mirrors the CLI bulk export functionality (`empower --all --csv --output ...`).

        Args:
            destination_dir: Directory where export files will be written (default: './data').
            scope: Data domain to export ('all', 'balances', 'holdings', 'transactions'). Default: 'all'.
            export_csv: Whether to write companion .csv files alongside JSON (default: True).
            export_beancount: Whether to generate Beancount plain-text ledger files (.bean) (default: False).
            beancount_map_path: Optional path to YAML/JSON Beancount account/category mapping configuration.
            start_date: Optional earliest date for transactions export (YYYY-MM-DD). If omitted, exports all historical transactions.
            end_date: Optional latest date for transactions export (YYYY-MM-DD). Defaults to today.
            limit: Optional maximum record limit for transactions.

        Returns:
            Dict containing export status, destination path, list of files created with record counts, and summary.
        """
        try:
            VALID_SCOPES = ("all", "balances", "holdings", "transactions")
            scope_clean = scope.strip().lower()
            if scope_clean not in VALID_SCOPES:
                return {
                    "status": "error",
                    "error_code": "INVALID_ARGUMENT",
                    "message": f"Invalid scope '{scope}'. Supported scopes: {', '.join(VALID_SCOPES)}.",
                }

            raw_dest = Path(destination_dir).expanduser()
            if raw_dest.is_symlink():
                return {
                    "status": "error",
                    "error_code": "ACCESS_DENIED",
                    "message": f"destination_dir '{destination_dir}' cannot be a symlink.",
                }

            export_root = Path(os.environ.get("EMPOWER_EXPORT_ROOT", "./data")).expanduser().resolve()
            dest_path = raw_dest.resolve()
            try:
                dest_path.relative_to(export_root)
            except ValueError:
                return {
                    "status": "error",
                    "error_code": "ACCESS_DENIED",
                    "message": (
                        f"destination_dir '{destination_dir}' resolves to '{dest_path}' which is outside "
                        f"the authorized export root '{export_root}'. Configure EMPOWER_EXPORT_ROOT if needed."
                    ),
                }

            if dest_path.is_symlink():
                return {
                    "status": "error",
                    "error_code": "ACCESS_DENIED",
                    "message": f"destination_dir '{destination_dir}' cannot be a symlink.",
                }

            dest_path.mkdir(parents=True, exist_ok=True)
            files_created: List[Dict[str, Any]] = []

            def _check_symlink(target_file: Path) -> Optional[Dict[str, Any]]:
                if target_file.is_symlink():
                    return {
                        "status": "error",
                        "error_code": "ACCESS_DENIED",
                        "message": f"Refusing to write to symlinked destination file '{target_file}'.",
                    }
                return None

            do_balances = scope_clean in ("all", "balances")
            do_holdings = scope_clean in ("all", "holdings")
            do_transactions = scope_clean in ("all", "transactions")

            balances: Optional[DashboardBalances] = None
            holdings: Optional[DashboardHoldings] = None
            tx_data: Optional[DashboardTransactions] = None
            beancount_warnings: List[str] = []

            if do_balances:
                balances = _fetch_balances_cached()
                b_json_path = dest_path / "empower_balances.json"
                sym_err = _check_symlink(b_json_path)
                if sym_err:
                    return sym_err
                with open(b_json_path, "w", encoding="utf-8") as f:
                    json.dump(balances.to_dict(), f, indent=2, ensure_ascii=False)
                files_created.append({"file": str(b_json_path), "format": "json", "records": len(balances.accounts)})

                if export_csv:
                    b_csv_path = dest_path / "empower_balances.csv"
                    sym_err = _check_symlink(b_csv_path)
                    if sym_err:
                        return sym_err
                    export_balances_csv(balances, b_csv_path)
                    files_created.append({"file": str(b_csv_path), "format": "csv", "records": len(balances.accounts)})

            if do_holdings:
                holdings = _fetch_holdings_cached()
                h_json_path = dest_path / "empower_holdings.json"
                sym_err = _check_symlink(h_json_path)
                if sym_err:
                    return sym_err
                with open(h_json_path, "w", encoding="utf-8") as f:
                    json.dump(holdings.to_dict(), f, indent=2, ensure_ascii=False)
                files_created.append({"file": str(h_json_path), "format": "json", "records": len(holdings.holdings)})

                if export_csv:
                    h_csv_path = dest_path / "empower_holdings.csv"
                    sym_err = _check_symlink(h_csv_path)
                    if sym_err:
                        return sym_err
                    export_holdings_csv(holdings, h_csv_path)
                    files_created.append({"file": str(h_csv_path), "format": "csv", "records": len(holdings.holdings)})

            if do_transactions:
                s_date = start_date
                e_date = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
                tx_data = _fetch_transactions_cached(start_date=s_date, end_date=e_date, limit=limit)

                t_jsonl_path = dest_path / "empower_transactions.jsonl"
                sym_err = _check_symlink(t_jsonl_path)
                if sym_err:
                    return sym_err
                with open(t_jsonl_path, "w", encoding="utf-8") as f:
                    for t in tx_data.transactions:
                        f.write(json.dumps(t, ensure_ascii=False) + "\n")
                files_created.append({"file": str(t_jsonl_path), "format": "jsonl", "records": len(tx_data.transactions)})

                if export_csv:
                    t_csv_path = dest_path / "empower_transactions.csv"
                    sym_err = _check_symlink(t_csv_path)
                    if sym_err:
                        return sym_err
                    export_transactions_csv(tx_data, t_csv_path)
                    files_created.append({"file": str(t_csv_path), "format": "csv", "records": len(tx_data.transactions)})

            beancount_warnings: List[str] = []
            if export_beancount:
                from empower_personal_dashboard.beancount import BeancountGenerator, BeancountMapper

                if beancount_map_path:
                    raw_map = Path(beancount_map_path).expanduser()
                    if raw_map.is_symlink():
                        return {
                            "status": "error",
                            "error_code": "ACCESS_DENIED",
                            "message": f"beancount_map_path '{beancount_map_path}' cannot be a symlink.",
                        }
                    resolved_map = raw_map.resolve()
                    if resolved_map.suffix.lower() not in (".yaml", ".yml", ".json"):
                        return {
                            "status": "error",
                            "error_code": "INVALID_ARGUMENT",
                            "message": f"beancount_map_path '{beancount_map_path}' must have a .yaml, .yml, or .json extension.",
                        }
                    allowed_roots = [
                        export_root,
                        Path.cwd().resolve(),
                        Path.home().resolve(),
                    ]
                    is_allowed = any(resolved_map.is_relative_to(root) for root in allowed_roots)
                    if not is_allowed:
                        return {
                            "status": "error",
                            "error_code": "ACCESS_DENIED",
                            "message": (
                                f"beancount_map_path '{beancount_map_path}' resolves to '{resolved_map}' "
                                f"which is outside approved configuration directories."
                            ),
                        }

                if balances is None:
                    try:
                        balances = _fetch_balances_cached()
                    except Exception as e:
                        logger.warning("Beancount export: balances unavailable: %s", e)
                        beancount_warnings.append(f"Balances unavailable; ledger omits balance data: {e}")

                b_mapper = BeancountMapper(mapping_path=beancount_map_path)
                b_gen = BeancountGenerator(mapper=b_mapper)
                ledger_dir = dest_path / "ledger"
                sym_err = _check_symlink(ledger_dir)
                if sym_err:
                    return sym_err

                for bean_file_name in ("main.bean", "accounts.bean", "balances.bean", "holdings.bean", "prices.bean", "transactions.bean"):
                    bean_target = ledger_dir / bean_file_name
                    bean_err = _check_symlink(bean_target)
                    if bean_err:
                        return bean_err

                b_files = b_gen.export_modular_ledger(
                    destination_dir=ledger_dir,
                    balances=balances,
                    holdings=holdings,
                    transactions=tx_data,
                )
                for bf in b_files:
                    files_created.append({"file": str(bf), "format": "beancount", "records": None})

            resp: Dict[str, Any] = {
                "status": "success",
                "destination_dir": str(dest_path),
                "scope": scope,
                "files_count": len(files_created),
                "files": files_created,
                "summary": f"Successfully exported {len(files_created)} file(s) to {dest_path}.",
            }
            if beancount_warnings:
                resp["warnings"] = beancount_warnings
            return resp
        except (SessionExpiredError, RequireTwoFactorException, FileNotFoundError):
            return {
                "status": "error",
                "error_code": "AUTH_REQUIRED",
                "message": (
                    "Empower session has expired or is not initialized. "
                    "Please run 'empower --login' in your local terminal to re-authenticate with 2FA."
                ),
            }
        except Exception as e:
            logger.exception("Unexpected error in export_data")
            return {"status": "error", "error_code": "EXPORT_ERROR", "message": str(e)}

    @server.tool()
    def get_export_options() -> Dict[str, Any]:
        """Discover available export formats, CLI commands, and destination configurations.

        Returns:
            Dict outlining supported formats (JSON, JSONL, CSV, Markdown, Table, Beancount), scopes, CLI flags, and usage examples.
        """
        return {
            "status": "success",
            "supported_formats": ["json", "jsonl", "csv", "markdown", "table", "beancount"],
            "data_scopes": ["all", "balances", "holdings", "transactions"],
            "cli_examples": {
                "bulk_export_csv": "empower --all --csv",
                "beancount_modular_export": "empower --all --beancount --output-beancount ./ledger",
                "beancount_single_ledger": "empower --all --beancount --output-beancount data/empower.bean",
                "balances_markdown": "empower --balances --format markdown",
                "holdings_table": "empower --holdings --format table --limit 50",
                "filtered_transactions_csv": "empower --transactions --start-date 2026-01-01 --csv --output-transactions data/tx.json",
                "silent_cron_export": "empower --all --csv --quiet",
            },
            "mcp_export_tool": "Call export_data(destination_dir='./data', scope='all', export_csv=True, export_beancount=True) to trigger programmatic exports directly.",
        }

    # -------------------------------------------------------------------------
    # Resources
    # -------------------------------------------------------------------------

    @server.resource("empower://balances/summary")
    def get_balances_resource() -> str:
        """Resource exposing real-time net worth and asset class totals as JSON."""
        res = get_net_worth_summary()
        return json.dumps(res, indent=2)

    @server.resource("empower://holdings/portfolio")
    def get_holdings_resource() -> str:
        """Resource exposing investment portfolio positions as JSON."""
        res = get_holdings(limit=100)
        return json.dumps(res, indent=2)

    @server.resource("empower://accounts/list")
    def get_accounts_resource() -> str:
        """Resource exposing linked financial accounts list as JSON."""
        res = get_balances(include_inactive=False)
        return json.dumps(res, indent=2)

    @server.resource("empower://beancount/prices")
    def get_beancount_prices_resource() -> str:
        """Resource exposing investment commodity prices as Beancount price directives."""
        try:
            holdings = _fetch_holdings_cached()
            from empower_personal_dashboard.beancount import BeancountGenerator

            gen = BeancountGenerator()
            return gen.generate_prices_bean(holdings)
        except (SessionExpiredError, RequireTwoFactorException, FileNotFoundError):
            return (
                ";; ERROR [AUTH_REQUIRED]: Empower session has expired or is not initialized.\n"
                ";; Please run 'empower --login' in your local terminal to re-authenticate with 2FA.\n"
            )
        except EmpowerError as e:
            return f";; ERROR [API_ERROR]: Empower API error: {e}\n"
        except Exception as e:
            logger.exception("Unexpected error in get_beancount_prices_resource")
            return f";; ERROR [INTERNAL_ERROR]: {e}\n"

    @server.resource("empower://beancount/balances")
    def get_beancount_balances_resource() -> str:
        """Resource exposing accounts and commodity balances as Beancount directives."""
        try:
            balances = _fetch_balances_cached()
            holdings = _fetch_holdings_cached()
            from empower_personal_dashboard.beancount import BeancountGenerator

            gen = BeancountGenerator()
            return gen.generate_accounts_bean(balances, holdings) + "\n" + gen.generate_balances_bean(balances, holdings)
        except (SessionExpiredError, RequireTwoFactorException, FileNotFoundError):
            return (
                ";; ERROR [AUTH_REQUIRED]: Empower session has expired or is not initialized.\n"
                ";; Please run 'empower --login' in your local terminal to re-authenticate with 2FA.\n"
            )
        except EmpowerError as e:
            return f";; ERROR [API_ERROR]: Empower API error: {e}\n"
        except Exception as e:
            logger.exception("Unexpected error in get_beancount_balances_resource")
            return f";; ERROR [INTERNAL_ERROR]: {e}\n"

    @server.resource("empower://beancount/transactions")
    def get_beancount_transactions_resource() -> str:
        """Resource exposing recent transactions as balanced Beancount double-entry directives."""
        try:
            now = datetime.now(timezone.utc)
            start_of_year = f"{now.year}-01-01"
            today = now.strftime("%Y-%m-%d")
            tx_data = _fetch_transactions_cached(start_date=start_of_year, end_date=today, limit=100)
            balances = None
            try:
                balances = _fetch_balances_cached()
            except Exception as e:
                logger.warning("Could not fetch balances to enrich Beancount transactions resource: %s", e)
            from empower_personal_dashboard.beancount import BeancountGenerator

            gen = BeancountGenerator()
            return gen.generate_transactions_bean(tx_data, balances=balances)
        except (SessionExpiredError, RequireTwoFactorException, FileNotFoundError):
            return (
                ";; ERROR [AUTH_REQUIRED]: Empower session has expired or is not initialized.\n"
                ";; Please run 'empower --login' in your local terminal to re-authenticate with 2FA.\n"
            )
        except EmpowerError as e:
            return f";; ERROR [API_ERROR]: Empower API error: {e}\n"
        except Exception as e:
            logger.exception("Unexpected error in get_beancount_transactions_resource")
            return f";; ERROR [INTERNAL_ERROR]: {e}\n"

    @server.resource("empower://export/options")
    def get_export_options_resource() -> str:
        """Resource exposing export options, formats, and CLI flags as markdown."""
        return (
            "# Empower Personal Dashboard Export Options\n\n"
            "## CLI Export Commands\n"
            "- Bulk export JSON + CSV: `empower --all --csv`\n"
            "- Beancount modular ledger: `empower --all --beancount --output-beancount ./ledger`\n"
            "- Balances to Markdown: `empower --balances --format markdown`\n"
            "- Holdings to Table: `empower --holdings --format table --limit 50`\n"
            "- Date-filtered transactions: `empower --transactions --start-date YYYY-01-01 --csv`\n\n"
            "## Supported Formats\n"
            "1. **JSON**: Full hierarchical state dictionaries.\n"
            "2. **JSONL**: Newline-delimited JSON stream for transaction ledgers.\n"
            "3. **CSV**: Flat tabular companion files for Excel and spreadsheets.\n"
            "4. **Markdown**: Formatted tables for direct note embedding.\n"
            "5. **Table**: ASCII console tables for terminal audits.\n"
            "6. **Beancount**: Double-entry plain-text accounting directives (.bean) with Direct Firm Naming and lot tracking.\n"
        )

    # -------------------------------------------------------------------------
    # Prompts
    # -------------------------------------------------------------------------

    @server.prompt()
    def portfolio_review(risk_profile: str = "moderate") -> str:
        """Prompt template for auditing portfolio asset allocation and cash drag."""
        return (
            f"Please conduct an in-depth portfolio review with a {risk_profile} risk tolerance. "
            "Follow these steps:\n"
            "1. Call `get_net_worth_summary` to understand total net worth and asset breakdown.\n"
            "2. Call `get_holdings` to inspect security positions and allocations.\n"
            "3. Analyze cash drag, equity vs fixed-income balance, and single-stock concentration.\n"
            "4. Provide a structured summary with actionable recommendations."
        )

    @server.prompt()
    def spending_audit(days: int = 90) -> str:
        """Prompt template for analyzing recent transactions, recurring expenses, and burn rate."""
        return (
            f"Please perform a cashflow and spending audit over the past {days} days. "
            "Follow these steps:\n"
            "1. Call `get_transactions` for the specified timeframe.\n"
            "2. Group expenses by category and identify the highest expenditure buckets.\n"
            "3. Identify recurring subscriptions and check for unexpected spikes or fee charges.\n"
            "4. Summarize net cashflow (income vs expenses) and highlight potential budget savings."
        )

    @server.prompt()
    def recent_purchases_audit(days: int = 7) -> str:
        """Prompt template for inspecting recent spending, card charges, and retail purchases."""
        return (
            f"Please review retail purchases and spending over the last {days} days. "
            "Follow these steps:\n"
            f"1. Call `get_transactions(days={days}, spending_only=True)`.\n"
            "2. Identify all posted charges, amounts, and merchants.\n"
            "3. Highlight any unusual charges or recurring subscription renewals.\n"
            "4. Provide a clear spending breakdown table."
        )

    @server.prompt()
    def top_holdings_review(limit: int = 10) -> str:
        """Prompt template for summarizing top investment holdings across all accounts."""
        return (
            f"Please provide an analysis of our top {limit} investment holdings. "
            "Follow these steps:\n"
            f"1. Call `get_holdings(aggregate_by_ticker=True, sort_by='value', limit={limit})`.\n"
            "2. Present the top positions in a table with value, asset class, and portfolio weight.\n"
            "3. Identify single-stock concentration risks and equity/fixed-income balance.\n"
            "4. Highlight any positions held across multiple accounts."
        )

    return server


def main() -> None:
    """CLI entrypoint for running the empower-mcp server."""
    parser = argparse.ArgumentParser(
        description="Empower Personal Dashboard Model Context Protocol (MCP) Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run in standard stdio mode (Claude Desktop, Antigravity CLI, Cursor)
  empower-mcp

  # Run in SSE HTTP mode for networked or containerized deployment
  empower-mcp --transport sse --port 8000 --host 0.0.0.0

  # Run in offline sandbox mode (synthetic mock data, no network required)
  empower-mcp --sandbox
        """,
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport mode (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on when using SSE transport (default: 8000)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host address to bind when using SSE transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--session-path",
        default=None,
        help="Path to saved session JSON file (default: ~/.empower_personal_dashboard_session.json)",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Run in offline sandbox mode using synthetic financial mock data",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging to stderr",
    )

    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="[%(asctime)s] [%(levelname)s] %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    if FastMCP is None:
        sys.stderr.write(
            "Error: The 'mcp' package is required to run the MCP server.\n"
            "Please install it with:\n"
            "    pip install 'empower-personal-dashboard[mcp]'\n"
        )
        sys.exit(1)

    # Initialize client
    client = EmpowerDashboardClient(
        session_file=args.session_path,
        mock_mode=args.sandbox,
        debug=args.debug,
    )

    server = create_mcp_server(client=client)

    if args.transport == "stdio":
        server.run(transport="stdio")
    elif args.transport == "sse":
        server.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
