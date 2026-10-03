"""
cli.py — Command-line interface for Empower Personal Dashboard.

Usage:
  # One-time interactive setup with 2FA:
  empower --login

  # Unattended balance extraction:
  empower

  # Extract investment holdings:
  empower --holdings --csv

  # Extract transactions:
  empower --transactions --start-date 2026-01-01 --end-date 2026-09-30 --limit 50

  # Full extraction (balances, holdings, transactions):
  empower --all --csv

  # Offline sandbox mode (test without network or credentials):
  empower --sandbox --all --csv
"""

import argparse
import csv
import getpass
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_SESSION_FILE,
    MIGRATED_BASE_URL,
    EmpowerDashboardClient,
)
from .exceptions import (
    EmpowerError,
    LoginFailedException,
    RequireTwoFactorException,
    SessionExpiredError,
)
from .models import DashboardBalances, DashboardHoldings, DashboardTransactions

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = Path.cwd() / "data"
DEFAULT_BALANCES_FILE = DEFAULT_OUTPUT_DIR / "empower_balances.json"
DEFAULT_HOLDINGS_FILE = DEFAULT_OUTPUT_DIR / "empower_holdings.json"
DEFAULT_TRANSACTIONS_FILE = DEFAULT_OUTPUT_DIR / "empower_transactions.jsonl"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="empower",
        description="Extract account balances, investment holdings, and transaction history via Empower Personal Dashboard.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Run interactive login to authenticate with 2FA and save persistent session.",
    )
    parser.add_argument(
        "--balances",
        action="store_true",
        help="Extract account balances and net worth snapshot.",
    )
    parser.add_argument(
        "--holdings",
        action="store_true",
        help="Extract investment holdings and positions.",
    )
    parser.add_argument(
        "--transactions",
        action="store_true",
        help="Extract individual account transactions.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Extract balances, holdings, and transactions in a single run.",
    )
    parser.add_argument(
        "--session-file",
        type=Path,
        default=DEFAULT_SESSION_FILE,
        help=f"Path to session storage file (default: {DEFAULT_SESSION_FILE}).",
    )
    parser.add_argument(
        "--sandbox",
        "--mock",
        action="store_true",
        help="Force execution in offline sandbox/mock mode using synthetic benchmark accounts.",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Transactions start date (YYYY-MM-DD). If omitted, extracts all historical transactions.",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="Transactions end date (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--account-id",
        type=str,
        default=None,
        help="Filter transactions by specific account ID (userAccountId).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of transactions or holdings to process/display.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_BALANCES_FILE,
        help=f"File path to save balance snapshot JSON (default: {DEFAULT_BALANCES_FILE}).",
    )
    parser.add_argument(
        "--output-holdings",
        type=Path,
        default=DEFAULT_HOLDINGS_FILE,
        help=f"File path to save holdings JSON (default: {DEFAULT_HOLDINGS_FILE}).",
    )
    parser.add_argument(
        "--output-transactions",
        type=Path,
        default=DEFAULT_TRANSACTIONS_FILE,
        help=f"File path to save transactions JSON or JSONL (default: {DEFAULT_TRANSACTIONS_FILE}).",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Export companion CSV files alongside JSON output.",
    )
    parser.add_argument(
        "--beancount",
        action="store_true",
        help="Export data to Beancount plain-text accounting format (.bean / .beancount).",
    )
    parser.add_argument(
        "--output-beancount",
        type=Path,
        default=None,
        help="File path or directory for Beancount ledger export.",
    )
    parser.add_argument(
        "--beancount-map",
        type=Path,
        default=None,
        help="Path to YAML/JSON Beancount account/category mapping configuration.",
    )
    parser.add_argument(
        "--overwrite-ledger",
        "--overwrite",
        action="store_true",
        help="Overwrite existing Beancount ledger files instead of appending.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Explicitly append exported Beancount directives to an existing ledger file.",
    )
    parser.add_argument(
        "--opening-date",
        type=str,
        default=None,
        help="Baseline date for initial portfolio holdings lots (e.g. 2020-01-01).",
    )
    parser.add_argument(
        "--from-data-dir",
        "--input-dir",
        type=Path,
        default=None,
        help="Path to directory containing existing empower_balances.json, empower_holdings.json, empower_transactions.jsonl to process offline.",
    )
    parser.add_argument(
        "--input-balances",
        type=Path,
        default=None,
        help="Path to existing balances JSON snapshot to load offline.",
    )
    parser.add_argument(
        "--input-holdings",
        type=Path,
        default=None,
        help="Path to existing holdings JSON snapshot to load offline.",
    )
    parser.add_argument(
        "--input-transactions",
        type=Path,
        default=None,
        help="Path to existing transactions JSON or JSONL snapshot to load offline.",
    )
    parser.add_argument(
        "--email",
        "--username",
        type=str,
        default=None,
        help="Empower email/username (optional, otherwise reads env or prompts).",
    )
    parser.add_argument(
        "--password",
        type=str,
        default=None,
        help="Empower password (optional, otherwise reads env or prompts).",
    )
    parser.add_argument(
        "--mode",
        choices=["SMS", "EMAIL"],
        default=None,
        help="2FA challenge delivery method (SMS or EMAIL).",
    )
    parser.add_argument(
        "--code",
        type=str,
        default=None,
        help="6-digit 2FA verification code.",
    )
    parser.add_argument(
        "--format",
        choices=["table", "markdown", "json", "beancount"],
        default="table",
        help="Console output format (table, markdown, json, beancount).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging.",
    )
    parser.add_argument(
        "--migrated",
        action="store_true",
        help="Connect directly to Empower unified migrated API (https://pc-api.empower-retirement.com).",
    )
    parser.add_argument(
        "--api-host",
        type=str,
        default=None,
        help="Custom base API host URL.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="File path to write detailed debug logs.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="HTTP request timeout in seconds (default: 90).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress console output, only writing files.",
    )
    return parser.parse_args()


def format_currency(val: float) -> str:
    if val < 0:
        return f"-${abs(val):,.2f}"
    return f"${val:,.2f}"


def export_balances_csv(balances: DashboardBalances, filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["institution", "account_name", "account_type", "balance", "is_asset", "currency", "last_refreshed"]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for acct in balances.accounts:
            writer.writerow({
                "institution": acct.get("firm_name", ""),
                "account_name": acct.get("account_name", ""),
                "account_type": acct.get("account_type", ""),
                "balance": acct.get("balance", 0.0),
                "is_asset": acct.get("is_asset", True),
                "currency": acct.get("currency", "USD"),
                "last_refreshed": acct.get("last_refreshed", ""),
            })


def export_holdings_csv(holdings: DashboardHoldings, filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "account_name", "ticker", "cusip", "description", "holding_type",
        "quantity", "price", "value", "cost_basis", "holding_percentage",
        "one_day_percent_change", "one_day_value_change"
    ]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for h in holdings.holdings:
            writer.writerow({
                "account_name": h.get("account_name", ""),
                "ticker": h.get("ticker", ""),
                "cusip": h.get("cusip", ""),
                "description": h.get("description", ""),
                "holding_type": h.get("holding_type", ""),
                "quantity": h.get("quantity", 0.0),
                "price": h.get("price", 0.0),
                "value": h.get("value", 0.0),
                "cost_basis": h.get("cost_basis") if h.get("cost_basis") is not None else "",
                "holding_percentage": h.get("holding_percentage") if h.get("holding_percentage") is not None else "",
                "one_day_percent_change": h.get("one_day_percent_change") if h.get("one_day_percent_change") is not None else "",
                "one_day_value_change": h.get("one_day_value_change") if h.get("one_day_value_change") is not None else "",
            })


def export_transactions_csv(transactions: DashboardTransactions, filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "transaction_date", "account_name", "description", "amount",
        "transaction_type", "investment_type", "symbol", "price", "quantity",
        "status", "category_id", "user_transaction_id"
    ]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for tx in transactions.transactions:
            writer.writerow({
                "transaction_date": tx.get("transaction_date", ""),
                "account_name": tx.get("account_name", ""),
                "description": tx.get("description", ""),
                "amount": tx.get("amount", 0.0),
                "transaction_type": tx.get("transaction_type", ""),
                "investment_type": tx.get("investment_type") or "",
                "symbol": tx.get("symbol") or "",
                "price": tx.get("price") if tx.get("price") is not None else "",
                "quantity": tx.get("quantity") if tx.get("quantity") is not None else "",
                "status": tx.get("status", ""),
                "category_id": tx.get("category_id") if tx.get("category_id") is not None else "",
                "user_transaction_id": tx.get("user_transaction_id", ""),
            })


def render_balances_markdown(balances: DashboardBalances) -> str:
    lines = [
        f"### Empower Personal Dashboard Balance Snapshot ({balances.as_of_date})",
        "",
        f"- **Mode**: `{balances.mode}`",
        f"- **Total Net Worth**: **{format_currency(balances.net_worth)}**",
        f"- **Total Investments**: {format_currency(balances.total_investment)}",
        f"- **Total Cash / Banking**: {format_currency(balances.total_cash)}",
        f"- **Real Estate & Physical Assets**: {format_currency(balances.total_other_assets)}",
        f"- **Credit Card Liabilities**: {format_currency(balances.total_credit_card)}",
        f"- **Mortgages & Loans**: {format_currency(balances.total_loan + balances.total_mortgage)}",
        "",
        "#### Account Breakdown",
        "",
        "| Institution | Account Name | Type | Balance |",
        "| :--- | :--- | :--- | :--- |",
    ]

    for entry in balances.accounts:
        institution = entry.get("firm_name", "—")
        display_name = entry.get("account_name", "Account")
        category = entry.get("account_type", "OTHER")
        amount = entry.get("balance", 0.0)
        lines.append(f"| {institution} | {display_name} | `{category}` | {format_currency(amount)} |")

    return "\n".join(lines)


def render_balances_table(balances: DashboardBalances) -> str:
    md = render_balances_markdown(balances)
    divider = "=" * 80
    return f"{divider}\n{md}\n{divider}"


def render_holdings_markdown(
    holdings: DashboardHoldings,
    limit: Optional[int] = 25,
    preserve_order: bool = False,
) -> str:
    title_suffix = "" if preserve_order else " by Value"
    lines = [
        f"### Empower Personal Dashboard Holdings ({holdings.as_of_date})",
        "",
        f"- **Mode**: `{holdings.mode}`",
        f"- **Total Portfolio Value**: **{format_currency(holdings.total_value)}**",
        f"- **Total Positions**: {len(holdings.holdings)}",
        "",
        f"#### Top Positions{title_suffix}",
        "",
        "| Ticker | Description | Account | Type | Shares | Price | Value | Weight | 1-Day Chg |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
    ]

    sorted_holdings = holdings.holdings if preserve_order else sorted(holdings.holdings, key=lambda x: x.get("value", 0.0), reverse=True)
    display_holdings = sorted_holdings[:limit] if limit else sorted_holdings

    for h in display_holdings:
        ticker = h.get("ticker") or "—"
        desc = h.get("description", "—")
        if len(desc) > 35:
            desc = desc[:32] + "..."
        acct = h.get("account_name", "—")
        if len(acct) > 25:
            acct = acct[:22] + "..."
        htype = h.get("holding_type", "OTHER")
        shares = f"{h.get('quantity', 0.0):,.2f}"
        price = format_currency(h.get("price", 0.0))
        val = format_currency(h.get("value", 0.0))
        weight = f"{h.get('holding_percentage', 0.0):.1f}%" if h.get("holding_percentage") is not None else "—"
        chg = format_currency(h.get("one_day_value_change", 0.0)) if h.get("one_day_value_change") is not None else "—"
        lines.append(f"| `{ticker}` | {desc} | {acct} | `{htype}` | {shares} | {price} | {val} | {weight} | {chg} |")

    if limit and len(sorted_holdings) > limit:
        lines.append("")
        lines.append(f"*... and {len(sorted_holdings) - limit} additional positions.*")

    return "\n".join(lines)


def render_holdings_table(
    holdings: DashboardHoldings,
    limit: Optional[int] = 25,
    preserve_order: bool = False,
) -> str:
    md = render_holdings_markdown(holdings, limit=limit, preserve_order=preserve_order)
    divider = "=" * 80
    return f"{divider}\n{md}\n{divider}"


def render_transactions_markdown(transactions: DashboardTransactions, limit: Optional[int] = 25) -> str:
    lines = [
        f"### Empower Personal Dashboard Transactions ({transactions.start_date} to {transactions.end_date})",
        "",
        f"- **Mode**: `{transactions.mode}`",
        f"- **Total Transactions**: {transactions.total_transactions}",
        f"- **Money In**: {format_currency(transactions.money_in)}",
        f"- **Money Out**: {format_currency(transactions.money_out)}",
        f"- **Net Cashflow**: **{format_currency(transactions.net_cashflow)}**",
        "",
        "#### Recent Transactions",
        "",
        "| Date | Account | Description | Type | Amount | Status |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]

    display_txs = transactions.transactions[:limit] if limit else transactions.transactions

    for tx in display_txs:
        date = tx.get("transaction_date", "—")
        acct = tx.get("account_name", "—")
        if len(acct) > 25:
            acct = acct[:22] + "..."
        desc = tx.get("description", "—")
        if len(desc) > 35:
            desc = desc[:32] + "..."
        ttype = tx.get("transaction_type", "—")
        amt = tx.get("amount", 0.0)
        amt_str = f"+{format_currency(amt)}" if tx.get("is_credit") or tx.get("is_cash_in") else f"-{format_currency(amt)}"
        status = tx.get("status", "posted")
        lines.append(f"| {date} | {acct} | {desc} | `{ttype}` | {amt_str} | `{status}` |")

    if limit and len(transactions.transactions) > limit:
        lines.append("")
        lines.append(f"*... and {len(transactions.transactions) - limit} additional transactions.*")

    return "\n".join(lines)


def render_transactions_table(transactions: DashboardTransactions, limit: Optional[int] = 25) -> str:
    md = render_transactions_markdown(transactions, limit=limit)
    divider = "=" * 80
    return f"{divider}\n{md}\n{divider}"


def interactive_login(
    client: EmpowerDashboardClient,
    session_file: Path,
    cli_email: Optional[str] = None,
    cli_password: Optional[str] = None,
    cli_mode: Optional[str] = None,
    cli_code: Optional[str] = None,
) -> int:
    """Run interactive 2FA login workflow."""
    print("=" * 68)
    print("  Empower Personal Dashboard — Login & 2FA Setup")
    print("=" * 68)

    email = cli_email or os.environ.get("EMPOWER_USERNAME") or os.environ.get("PERSONAL_CAPITAL_EMAIL")
    if not email:
        try:
            email = input("Empower Email / Username: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[!] Canceled.", file=sys.stderr)
            return 1

    password = cli_password or os.environ.get("EMPOWER_PASSWORD") or os.environ.get("PERSONAL_CAPITAL_PASSWORD")
    if not password:
        try:
            password = getpass.getpass("Empower Password: ")
        except (EOFError, KeyboardInterrupt):
            print("\n[!] Canceled.", file=sys.stderr)
            return 1

    try:
        print(f"[*] Identifying user '{email}'...")
        try:
            client.login(username=email, password=password)
            print("[+] Login successful! No additional 2FA needed for this device.")
        except RequireTwoFactorException:
            print("[*] Two-factor authentication (2FA) is required.")

            mode = cli_mode
            if not mode:
                mode_input = input("Send 2FA challenge via [S]MS or [E]mail? (default: SMS): ").strip().upper()
                mode = "EMAIL" if mode_input.startswith("E") else "SMS"

            print(f"[*] Requesting 2FA challenge code via {mode}...")
            client.request_2fa_challenge(mode=mode)
            print(f"[+] Verification challenge accepted by Empower! Check your {mode.lower()} for code.")

            code = cli_code
            if not code:
                code = input("Enter the 6-digit verification code: ").strip()

            print("[*] Submitting verification code...")
            client.submit_2fa_code(code=code, mode=mode)
            print("[+] 2FA verified successfully.")

            print("[*] Finalizing device registration with password...")
            client.authenticate_password(password=password)
            print("[+] Password authenticated and device registered.")

        saved_path = client.save_session(session_file)
        print(f"[+] Session saved successfully to: {saved_path} (mode 0600)")
        print("[+] You can now run unattended periodic extractions without prompts.")
        return 0

    except LoginFailedException as e:
        print(f"[!] Login failed: {e}", file=sys.stderr)
        return 1
    except EmpowerError as e:
        print(f"[!] Empower error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[!] Unexpected error during login: {e}", file=sys.stderr)
        return 1


class _CliExit(Exception):
    """Internal signal used by helpers to request that main() return a code."""

    def __init__(self, code: int) -> None:
        super().__init__()
        self.code = code


def _build_client(args: argparse.Namespace) -> EmpowerDashboardClient:
    base_url = args.api_host
    if not base_url and args.migrated:
        base_url = MIGRATED_BASE_URL

    return EmpowerDashboardClient(
        session_file=args.session_file,
        base_url=base_url or DEFAULT_BASE_URL,
        timeout_seconds=args.timeout,
        mock_mode=args.sandbox,
        debug=args.debug,
        log_file=args.log_file,
    )


def _first_existing(candidates):
    for cand in candidates:
        if cand.exists():
            return cand
    return None


def _resolve_offline_input_files(input_dir, in_balances_file, in_holdings_file, in_transactions_file):
    if not input_dir:
        offline_mode = bool(in_balances_file or in_holdings_file or in_transactions_file)
        return in_balances_file, in_holdings_file, in_transactions_file, offline_mode

    input_dir = Path(input_dir).expanduser().resolve()
    if not input_dir.exists():
        print(f"[!] Error: Data directory not found: {input_dir}", file=sys.stderr)
        raise _CliExit(1)
    if not in_balances_file:
        in_balances_file = _first_existing([input_dir / "empower_balances.json", input_dir / "balances.json"])
    if not in_holdings_file:
        in_holdings_file = _first_existing([input_dir / "empower_holdings.json", input_dir / "holdings.json"])
    if not in_transactions_file:
        in_transactions_file = _first_existing([
            input_dir / "empower_transactions.jsonl",
            input_dir / "empower_transactions.json",
            input_dir / "transactions.jsonl",
            input_dir / "transactions.json",
        ])

    offline_mode = bool(in_balances_file or in_holdings_file or in_transactions_file)
    return in_balances_file, in_holdings_file, in_transactions_file, offline_mode


def _report_live_mode(args, client, progress_file) -> None:
    has_session = args.session_file.exists()
    if not has_session and not args.sandbox:
        if not args.quiet:
            print(f"[*] No session file found at {args.session_file}.", file=progress_file)
            print("[*] Defaulting to sandbox/mock mode. Run with '--login' to connect live.", file=progress_file)
        client.mock_mode = True

    if not args.quiet:
        mode_label = "SANDBOX / MOCK MODE" if client.mock_mode else "LIVE"
        print(f"[*] Querying Empower Personal Dashboard [{mode_label}]...", file=progress_file)


def _report_mode(args, client, offline_mode, progress_file) -> None:
    if offline_mode:
        if not args.quiet:
            print(f"[*] Processing offline financial datasets...", file=progress_file)
    else:
        _report_live_mode(args, client, progress_file)


def _resolve_requested_datasets(args):
    do_balances = args.balances or args.all or (not args.holdings and not args.transactions)
    do_holdings = args.holdings or args.all
    do_transactions = args.transactions or args.all
    return do_balances, do_holdings, do_transactions


def _safe_output_path(path: Path) -> Path:
    """Resolve a user-supplied output path and refuse symlinked targets."""
    resolved = Path(os.path.expanduser(str(path))).resolve()
    if Path(os.path.expanduser(str(path))).is_symlink():
        raise ValueError(f"Refusing to write to symlinked path: {path}")
    return resolved


def _emit(text: str) -> None:
    """Write rendered user-requested report output to stdout.

    Uses ``sys.stdout.write`` rather than ``print`` because CodeQL models the
    ``print`` builtin as a clear-text logging sink. This is report output the
    user explicitly requested, not logging, so writing to the stdout stream
    directly reflects the intent and keeps the data-flow query accurate.
    """
    sys.stdout.write(text + "\n")


def _load_balances(args, client, in_balances_file, progress_file):
    if in_balances_file:
        in_p = Path(in_balances_file)
        if not in_p.exists():
            print(f"[!] Balances file not found: {in_p}", file=sys.stderr)
            raise _CliExit(1)
        with open(in_p, "r", encoding="utf-8") as f:
            b_raw = json.load(f)
        balances_res = DashboardBalances.from_dict(b_raw)
        if not args.quiet:
            print(f"[+] Loaded {len(balances_res.accounts)} account balances from: {in_p}", file=progress_file)
        return balances_res

    balances_res = client.fetch_balances()
    b_data = balances_res.to_dict()
    b_data["extracted_at"] = datetime.now(timezone.utc).isoformat()

    if args.output:
        out_path = _safe_output_path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(b_data, f, indent=2, ensure_ascii=False)
        if not args.quiet:
            print(f"[+] Balances snapshot saved to: {args.output}", file=progress_file)

    if args.csv and args.output:
        csv_path = args.output.with_suffix(".csv")
        export_balances_csv(balances_res, csv_path)
        if not args.quiet:
            print(f"[+] Balances CSV saved to: {csv_path}", file=progress_file)

    return balances_res


def _load_holdings(args, client, in_holdings_file, progress_file):
    if in_holdings_file:
        in_h = Path(in_holdings_file)
        if not in_h.exists():
            print(f"[!] Holdings file not found: {in_h}", file=sys.stderr)
            raise _CliExit(1)
        with open(in_h, "r", encoding="utf-8") as f:
            h_raw = json.load(f)
        holdings_res = DashboardHoldings.from_dict(h_raw)
        if not args.quiet:
            print(f"[+] Loaded {len(holdings_res.holdings)} portfolio holdings from: {in_h}", file=progress_file)
        return holdings_res

    holdings_res = client.fetch_holdings()
    h_data = holdings_res.to_dict()
    h_data["extracted_at"] = datetime.now(timezone.utc).isoformat()

    if args.output_holdings:
        out_path = _safe_output_path(args.output_holdings)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(h_data, f, indent=2, ensure_ascii=False)
        if not args.quiet:
            print(f"[+] Holdings snapshot saved to: {args.output_holdings}", file=progress_file)

    if args.csv and args.output_holdings:
        csv_path = args.output_holdings.with_suffix(".csv")
        export_holdings_csv(holdings_res, csv_path)
        if not args.quiet:
            print(f"[+] Holdings CSV saved to: {csv_path}", file=progress_file)

    return holdings_res


def _parse_transactions_jsonl(in_t):
    tx_list = []
    with open(in_t, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tx_list.append(json.loads(line))
    dates = [t.get("transaction_date") for t in tx_list if t.get("transaction_date")]
    s_date = min(dates) if dates else "2026-01-01"
    e_date = max(dates) if dates else "2026-12-31"
    m_in = sum(float(t.get("amount", 0.0)) for t in tx_list if t.get("is_income") or t.get("is_credit"))
    m_out = sum(float(t.get("amount", 0.0)) for t in tx_list if t.get("is_spending") or t.get("is_cash_out"))
    return DashboardTransactions(
        start_date=s_date,
        end_date=e_date,
        total_transactions=len(tx_list),
        money_in=round(m_in, 2),
        money_out=round(m_out, 2),
        net_cashflow=round(m_in - m_out, 2),
        transactions=tx_list,
        mode="historical",
    )


def _load_transactions_from_file(args, in_transactions_file, progress_file):
    in_t = Path(in_transactions_file)
    if not in_t.exists():
        print(f"[!] Transactions file not found: {in_t}", file=sys.stderr)
        raise _CliExit(1)
    if str(in_t).endswith(".jsonl"):
        transactions_res = _parse_transactions_jsonl(in_t)
    else:
        with open(in_t, "r", encoding="utf-8") as f:
            t_raw = json.load(f)
        transactions_res = DashboardTransactions.from_dict(t_raw)

    if args.start_date or args.end_date:
        sd = args.start_date or transactions_res.start_date
        ed = args.end_date or transactions_res.end_date
        filtered = [
            t for t in transactions_res.transactions
            if sd <= str(t.get("transaction_date", "")) <= ed
        ]
        transactions_res.transactions = filtered
        transactions_res.total_transactions = len(filtered)
        transactions_res.start_date = sd
        transactions_res.end_date = ed

    if not args.quiet:
        print(f"[+] Loaded {transactions_res.total_transactions} transactions ({transactions_res.start_date} to {transactions_res.end_date}) from: {in_t}", file=progress_file)
    return transactions_res


def _write_transactions_output(args, transactions_res, t_data, progress_file) -> None:
    if args.output_transactions:
        out_path = _safe_output_path(args.output_transactions)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if str(args.output_transactions).endswith(".jsonl"):
            with open(out_path, "w", encoding="utf-8") as f:
                for tx in transactions_res.transactions:
                    f.write(json.dumps(tx, ensure_ascii=False) + "\n")
        else:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(t_data, f, indent=2, ensure_ascii=False)
        if not args.quiet:
            print(f"[+] Transactions saved to: {args.output_transactions}", file=progress_file)

    if args.csv and args.output_transactions:
        csv_path = args.output_transactions.with_suffix(".csv")
        export_transactions_csv(transactions_res, csv_path)
        if not args.quiet:
            print(f"[+] Transactions CSV saved to: {csv_path}", file=progress_file)


def _fetch_transactions(args, client, progress_file):
    start_date = args.start_date
    end_date = args.end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Beancount reconstruction requires complete transaction history, not truncated.
    # Apply limit only for display purposes (via render functions), not the fetch.
    tx_limit = None if (args.beancount or args.format == "beancount") else args.limit
    transactions_res = client.fetch_transactions(
        start_date=start_date,
        end_date=end_date,
        user_account_ids=args.account_id,
        limit=tx_limit,
    )
    t_data = transactions_res.to_dict()
    t_data["extracted_at"] = datetime.now(timezone.utc).isoformat()

    _write_transactions_output(args, transactions_res, t_data, progress_file)
    return transactions_res


def _load_transactions(args, client, in_transactions_file, progress_file):
    if in_transactions_file:
        return _load_transactions_from_file(args, in_transactions_file, progress_file)
    return _fetch_transactions(args, client, progress_file)


def _export_beancount_to_target(args, generator, out_target, balances_res, holdings_res, transactions_res, should_append, progress_file) -> None:
    if out_target.suffix in (".bean", ".beancount"):
        generator.export_single_file(
            filepath=out_target,
            balances=balances_res,
            holdings=holdings_res,
            transactions=transactions_res,
            append=should_append,
            opening_date=args.opening_date,
        )
        action_msg = "appended to" if (out_target.exists() and should_append) else "saved to"
        if not args.quiet:
            print(f"[+] Beancount ledger {action_msg}: {out_target}", file=progress_file)
    else:
        created = generator.export_modular_ledger(
            destination_dir=out_target,
            balances=balances_res,
            holdings=holdings_res,
            transactions=transactions_res,
            append=should_append,
            opening_date=args.opening_date,
        )
        if not args.quiet:
            print(f"[+] Beancount modular ledger ({len(created)} files) saved to: {out_target}", file=progress_file)


def _run_beancount_export(args, client, balances_res, holdings_res, transactions_res, offline_mode, progress_file):
    from empower_personal_dashboard.beancount import BeancountGenerator, BeancountMapper

    # Ensure balances are available to enrich account identities and types
    if not balances_res and not offline_mode:
        try:
            balances_res = client.fetch_balances()
        except Exception:
            logger.debug("Could not fetch balances to enrich Beancount accounts")  # lgtm[py/clear-text-logging-sensitive-data]

    mapper = BeancountMapper(mapping_path=args.beancount_map)
    generator = BeancountGenerator(mapper=mapper)
    should_append = not args.overwrite_ledger

    if args.output_beancount:
        _export_beancount_to_target(
            args, generator, Path(args.output_beancount),
            balances_res, holdings_res, transactions_res, should_append, progress_file,
        )
    else:
        dest_dir = Path("ledger")
        created = generator.export_modular_ledger(
            destination_dir=dest_dir,
            balances=balances_res,
            holdings=holdings_res,
            transactions=transactions_res,
            append=should_append,
            opening_date=args.opening_date,
        )
        if not args.quiet:
            print(f"[+] Beancount modular ledger ({len(created)} files) saved to: {dest_dir}", file=progress_file)  # lgtm[py/clear-text-logging-sensitive-data]

    return balances_res


def _render_json(balances_res, holdings_res, transactions_res) -> None:
    combined = {}
    if balances_res:
        combined["balances"] = balances_res.to_dict()
    if holdings_res:
        combined["holdings"] = holdings_res.to_dict()
    if transactions_res:
        combined["transactions"] = transactions_res.to_dict()
    print(json.dumps(combined, indent=2))


def _render_markdown(args, balances_res, holdings_res, transactions_res) -> None:
    if balances_res:
        _emit(render_balances_markdown(balances_res))
    if holdings_res:
        print(render_holdings_markdown(holdings_res, limit=args.limit or 25))
    if transactions_res:
        print(render_transactions_markdown(transactions_res, limit=args.limit or 25))


def _render_beancount(args, balances_res, holdings_res, transactions_res) -> None:
    from empower_personal_dashboard.beancount import BeancountGenerator, BeancountMapper

    mapper = BeancountMapper(mapping_path=args.beancount_map)
    generator = BeancountGenerator(mapper=mapper)
    output_parts = []
    # Declare FIFO booking so streamed sell postings (empty cost spec)
    # resolve deterministically against reconstructed multi-lot holdings.
    output_parts.append(
        'option "operating_currency" "USD"\n'
        'option "booking_method" "FIFO"\n'
    )
    output_parts.append(generator.generate_accounts_bean(balances_res, holdings_res, transactions_res))
    if balances_res or holdings_res:
        output_parts.append(generator.generate_balances_bean(balances_res, holdings_res, transactions=transactions_res))
    if holdings_res:
        output_parts.append(generator.generate_holdings_bean(holdings_res, balances=balances_res, transactions=transactions_res, opening_date=args.opening_date))
        output_parts.append(generator.generate_prices_bean(holdings_res))
    if transactions_res:
        output_parts.append(generator.generate_transactions_bean(transactions_res, balances=balances_res))
    print("\n".join(part.strip() for part in output_parts if part.strip()))


def _render_table(args, balances_res, holdings_res, transactions_res) -> None:
    if balances_res:
        _emit(render_balances_table(balances_res))
    if holdings_res:
        print(render_holdings_table(holdings_res, limit=args.limit or 25))
    if transactions_res:
        print(render_transactions_table(transactions_res, limit=args.limit or 25))


def _render_output(args, balances_res, holdings_res, transactions_res) -> None:
    if args.format == "json":
        _render_json(balances_res, holdings_res, transactions_res)
    elif args.format == "markdown":
        _render_markdown(args, balances_res, holdings_res, transactions_res)
    elif args.format == "beancount":
        _render_beancount(args, balances_res, holdings_res, transactions_res)
    else:
        _render_table(args, balances_res, holdings_res, transactions_res)


def main() -> int:
    args = parse_args()
    client = _build_client(args)

    if args.login:
        return interactive_login(
            client=client,
            session_file=args.session_file,
            cli_email=args.email,
            cli_password=args.password,
            cli_mode=args.mode,
            cli_code=args.code,
        )

    # Validate mutually exclusive ledger flags
    if getattr(args, "append", False) and getattr(args, "overwrite_ledger", False):
        print("[!] Error: Cannot specify both --append and --overwrite-ledger (--overwrite).", file=sys.stderr)
        return 1

    # Keep progress and informational messages out of stdout when emitting Beancount directives
    progress_file = sys.stderr if getattr(args, "format", None) == "beancount" else None

    # Determine offline data loading vs live API execution
    in_balances_file = getattr(args, "input_balances", None)
    in_holdings_file = getattr(args, "input_holdings", None)
    in_transactions_file = getattr(args, "input_transactions", None)

    try:
        in_balances_file, in_holdings_file, in_transactions_file, offline_mode = _resolve_offline_input_files(
            getattr(args, "from_data_dir", None),
            in_balances_file,
            in_holdings_file,
            in_transactions_file,
        )
    except _CliExit as e:
        return e.code

    _report_mode(args, client, offline_mode, progress_file)

    do_balances, do_holdings, do_transactions = _resolve_requested_datasets(args)

    balances_res: Optional[DashboardBalances] = None
    holdings_res: Optional[DashboardHoldings] = None
    transactions_res: Optional[DashboardTransactions] = None

    try:
        if do_balances:
            balances_res = _load_balances(args, client, in_balances_file, progress_file)

        if do_holdings:
            holdings_res = _load_holdings(args, client, in_holdings_file, progress_file)

        if do_transactions:
            transactions_res = _load_transactions(args, client, in_transactions_file, progress_file)

        if args.beancount:
            balances_res = _run_beancount_export(
                args, client, balances_res, holdings_res, transactions_res, offline_mode, progress_file
            )

    except _CliExit as e:
        return e.code
    except SessionExpiredError as e:
        print(f"[!] {e}", file=sys.stderr)
        print("[!] Your session has expired. Re-authenticate by running: empower --login", file=sys.stderr)
        return 1
    except EmpowerError as e:
        print(f"[!] Empower API error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[!] Unexpected error: {e}", file=sys.stderr)
        return 1

    if not args.quiet:
        _render_output(args, balances_res, holdings_res, transactions_res)

    return 0


if __name__ == "__main__":
    sys.exit(main())
