"""
beancount.py — Pure-Python Beancount Plain-Text Accounting (PTA) export engine.

Provides zero-heavy-dependency formatting for Beancount directives:
- Account taxonomy with Direct Firm Naming (Assets:Firm:Account, Liabilities:Firm:Account)
- Double-entry transaction balancing with category mapping and suspense accounts
- Ground-truth balance assertions for cash, liabilities, and investment commodities
- Real-time commodity price directives from portfolio holdings
- Modular and single-file export generation adhering to Beancount 2.x and 3.x standards
"""

import datetime
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from empower_personal_dashboard.models import (
    DashboardBalances,
    DashboardHoldings,
    DashboardTransactions,
)
from empower_personal_dashboard.sanitizers import clean_api_text

logger = logging.getLogger(__name__)

# Strict Beancount account regex:
# Must start with one of 5 root types, each segment starts with capital letter or digit
BEANCOUNT_ACCOUNT_REGEX = re.compile(
    r"^(Assets|Liabilities|Equity|Income|Expenses):[A-Z0-9][A-Za-z0-9\-]*"
    r"(:[A-Z0-9][A-Za-z0-9\-]*)*$"
)


def _clean_segment(text: str) -> str:
    """Normalize and format a text string into a valid Beancount account subsegment."""
    cleaned = clean_api_text(text)
    raw_tokens = re.findall(r"[A-Za-z0-9]+", cleaned)
    if not raw_tokens:
        return "Unknown"
    parts = []
    for token in raw_tokens:
        if token[0].islower():
            parts.append(token[0].upper() + token[1:])
        else:
            parts.append(token)
    segment = "".join(parts)
    # If the segment contains numbers preceded by words, preserve clean hyphenation (e.g. Checking-1234)
    m = re.match(r"^([A-Za-z]+)(\d+)$", segment)
    if m:
        segment = f"{m.group(1)}-{m.group(2)}"
    # Ensure segment starts with an uppercase letter or number
    if not segment[0].isalnum() or segment[0].islower():
        segment = "A" + segment
    return segment


def slugify_account_name(
    firm_name: str,
    account_name: str,
    account_type: str = "bank",
    is_asset: Optional[bool] = None,
) -> str:
    """Derive a compliant Beancount account name using Direct Firm Naming.

    Examples:
        - Ally Bank, Interest Checking - 1234, bank -> Assets:AllyBank:InterestChecking-1234
        - Chase, Freedom Unlimited (...5678), credit -> Liabilities:Chase:FreedomUnlimited-5678
        - Vanguard, Taxable Brokerage, investment -> Assets:Vanguard:TaxableBrokerage
        - Rocket Mortgage, Primary Loan, mortgage -> Liabilities:RocketMortgage:PrimaryHomeLoan

    Args:
        firm_name: Name of the financial firm / institution.
        account_name: Name of the individual account.
        account_type: Empower account category (bank, credit, investment, mortgage, etc.).
        is_asset: Explicit asset/liability flag if known from account balance metadata.

    Returns:
        A strictly valid Beancount account name string.
    """
    type_lower = (account_type or "").strip().lower()
    if is_asset is False:
        root = "Liabilities"
    elif is_asset is True:
        root = "Assets"
    elif type_lower in (
        "credit",
        "credit_card",
        "creditcard",
        "credit_cards",
        "loan",
        "mortgage",
        "other_liabilities",
        "other_liability",
        "liability",
        "liabilities",
    ):
        root = "Liabilities"
    else:
        root = "Assets"

    firm_seg = _clean_segment(firm_name or "Institution")
    acct_seg = _clean_segment(account_name or "Account")

    candidate = f"{root}:{firm_seg}:{acct_seg}"
    if not BEANCOUNT_ACCOUNT_REGEX.match(candidate):
        # Fallback to safe alphanumeric characters
        firm_safe = re.sub(r"[^A-Za-z0-9]", "", firm_seg) or "Firm"
        acct_safe = re.sub(r"[^A-Za-z0-9]", "", acct_seg) or "Account"
        candidate = f"{root}:{firm_safe}:{acct_safe}"

    return candidate


derive_beancount_account = slugify_account_name


def _escape_beancount_string(val: Any) -> str:
    """Escape and sanitize values for Beancount double-quoted strings.

    Replaces newlines, carriage returns, and tabs with spaces, sanitizes unicode mojibake,
    and escapes backslashes and double quotes.
    """
    if val is None:
        return ""
    text = str(val)
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    text = clean_api_text(text)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return text


def _clean_ticker(ticker: Optional[str]) -> str:
    """Clean and sanitize ticker to a strictly valid Beancount commodity symbol.

    Replaces spaces and slashes (e.g. 'BRK B' or 'BRK/B') with dots, and strips
    non-conforming characters.
    """
    if not ticker:
        return ""
    t = re.sub(r"[\s/]+", ".", str(ticker).strip().upper())
    t = re.sub(r"[^A-Z0-9\'\._\-]", "", t)
    return t


def _format_quantity(qty: float) -> str:
    """Format commodity quantity with high precision preserving fractional shares."""
    return f"{qty:.6f}"


def _format_cost(cost: float) -> str:
    """Format per-share cost basis or unit price with high precision."""
    return f"{cost:.6f}"


def _format_price(price: float) -> str:
    """Format commodity unit price."""
    return f"{price:.4f}"


def _parse_simple_yaml(content: str) -> Dict[str, Any]:
    """Lightweight fallback parser for basic YAML mapping files when PyYAML is not installed."""
    result: Dict[str, Any] = {"accounts": {}, "categories": {}, "regex_rules": []}
    current_section = None
    current_rule: Dict[str, str] = {}

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Check section header
        if line.endswith(":") and not line.startswith("-"):
            if current_rule and "pattern" in current_rule and "account" in current_rule:
                result["regex_rules"].append(current_rule)
                current_rule = {}
            current_section = line[:-1].strip().lower()
            continue

        if current_section in ("accounts", "categories"):
            if line.startswith('"'):
                m = re.match(r'^"([^"]+)"\s*:\s*(.*)$', line)
                if m:
                    k, v = m.group(1).strip(), m.group(2).strip().strip('"').strip("'")
                    if k and v:
                        result[current_section][k] = v
                    continue
            elif line.startswith("'"):
                m = re.match(r"^'([^']+)'\s*:\s*(.*)$", line)
                if m:
                    k, v = m.group(1).strip(), m.group(2).strip().strip('"').strip("'")
                    if k and v:
                        result[current_section][k] = v
                    continue

            parts = line.split(":", 1)
            if len(parts) == 2:
                k = parts[0].strip().strip('"').strip("'")
                v = parts[1].strip().strip('"').strip("'")
                if k and v:
                    result[current_section][k] = v

        elif current_section in ("regex_rules", "regex_payee_rules"):
            if line.startswith("-"):
                if current_rule and "pattern" in current_rule and "account" in current_rule:
                    result["regex_rules"].append(current_rule)
                current_rule = {}
                line = line[1:].strip()

            parts = line.split(":", 1)
            if len(parts) == 2:
                k = parts[0].strip().lower()
                v = parts[1].strip().strip('"').strip("'")
                if k in ("pattern", "account") and v:
                    current_rule[k] = v

    if current_rule and "pattern" in current_rule and "account" in current_rule:
        result["regex_rules"].append(current_rule)

    return result


def _build_account_lookup(balances: Optional[DashboardBalances]) -> Dict[str, Dict[str, Any]]:
    """Build a lookup dictionary from balances keyed by account_id, user_account_id, and account_name."""
    lookup: Dict[str, Dict[str, Any]] = {}
    if not balances or not balances.accounts:
        return lookup
    for acct in balances.accounts:
        aid = str(acct.get("account_id") or "")
        uaid = str(acct.get("user_account_id") or "")
        aname = str(acct.get("account_name") or "")
        if aid:
            lookup[aid] = acct
        if uaid:
            lookup[uaid] = acct
        if aname:
            lookup[aname] = acct
    return lookup


def _is_investment_tx(transaction: Dict[str, Any]) -> bool:
    """True when a transaction represents a security trade or investment dividend.

    Requires a ticker symbol plus a buy/sell/reinvest/disposal or dividend
    transaction/investment type, matching the investment detection used when
    emitting postings.
    """
    if not _clean_ticker(transaction.get("symbol")):
        return False
    tt = clean_api_text(transaction.get("transaction_type") or "").strip().lower()
    it = clean_api_text(transaction.get("investment_type") or "").strip().lower()
    return (
        tt in ("buy", "sell", "reinvest", "disposal")
        or it in ("buy", "sell", "reinvest", "disposal")
        or "dividend" in tt
        or "dividend" in it
    )


def _resolve_account_from_tx(
    transaction: Dict[str, Any],
    acct_lookup: Dict[str, Dict[str, Any]],
    default_firm: str = "Institution",
    default_type: str = "bank",
    default_name: str = "Account",
) -> Tuple[str, str, str, str]:
    """Resolve account details from transaction and lookup table.

    Returns: (firm, account_name, account_type, resolved_id)

    Investment transactions fall back to the same ``Brokerage``/``investment``
    identity that holdings reconciliation and balance assertions use, so an
    unresolved security trade, its reconstructed opening lot, and the final
    quantity assertion all land in one ``Assets:Brokerage:...`` account instead
    of splitting between ``Assets:Institution`` and ``Assets:Brokerage``.
    """
    aid = str(transaction.get("account_id") or "")
    uaid = str(transaction.get("user_account_id") or "")
    t_name = transaction.get("account_name") or ""

    if _is_investment_tx(transaction):
        default_firm = "Brokerage"
        default_type = "investment"
        default_name = "Brokerage"

    acct_info = acct_lookup.get(aid) or acct_lookup.get(uaid) or acct_lookup.get(t_name)
    if acct_info:
        firm = acct_info.get("firm_name") or default_firm
        acct_name = acct_info.get("account_name") or t_name or default_name
        acct_type = acct_info.get("account_type") or default_type
        resolved_id = str(acct_info.get("account_id") or aid)
    else:
        firm = transaction.get("firm_name") or default_firm
        acct_name = t_name or default_name
        acct_type = transaction.get("account_type") or default_type
        resolved_id = aid

    return firm, acct_name, acct_type, resolved_id


class BeancountMapper:
    """Handles mapping of Empower accounts, categories, and payee rules to Beancount accounts."""

    def __init__(self, mapping_path: Optional[Union[str, Path]] = None):
        self.accounts: Dict[str, str] = {}
        self.categories: Dict[str, str] = {}
        self.regex_rules: List[Dict[str, str]] = []

        if mapping_path:
            self.load_mapping(mapping_path)

    def load_mapping(self, mapping_path: Union[str, Path]) -> None:
        """Load mapping rules from a YAML or JSON configuration file.

        Gracefully catches YAML parse errors and rejects non-mapping structures (e.g. scalars or lists).
        """
        p = Path(mapping_path).expanduser().resolve()
        if not p.exists():
            logger.warning("Beancount mapping file not found: %s", p)
            return

        try:
            content = p.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Could not read Beancount mapping file %s: %s", p, e)
            return

        parsed: Any = None
        try:
            import yaml  # type: ignore

            parsed = yaml.safe_load(content)
        except ImportError:
            try:
                parsed = json.loads(content)
            except Exception:
                parsed = _parse_simple_yaml(content)
        except Exception as e:
            logger.warning("Failed to parse Beancount mapping configuration (%s): %s", p, e)
            parsed = {}

        if not isinstance(parsed, dict):
            logger.warning(
                "Beancount mapping configuration must be a mapping/dict, got %s. Ignoring rules.",
                type(parsed).__name__,
            )
            return

        raw_accounts = parsed.get("accounts")
        if isinstance(raw_accounts, dict):
            self.accounts = {str(k): str(v) for k, v in raw_accounts.items()}
        else:
            self.accounts = {}

        raw_categories = parsed.get("categories")
        if isinstance(raw_categories, dict):
            self.categories = {str(k): str(v) for k, v in raw_categories.items()}
        else:
            self.categories = {}

        raw_rules = parsed.get("regex_rules")
        if isinstance(raw_rules, list):
            self.regex_rules = [
                {str(k): str(v) for k, v in r.items()}
                for r in raw_rules
                if isinstance(r, dict)
            ]
        else:
            self.regex_rules = []

    def resolve_account(
        self,
        firm_name: str,
        account_name: str,
        account_id: Optional[str] = None,
        account_type: str = "bank",
        **kwargs: Any,
    ) -> str:
        """Resolve Beancount account name using overrides or slugification."""
        actual_type = kwargs.get("acct_type", account_type)
        # Check explicit overrides by account_id, composite firm + account_name, then account_name
        if account_id and str(account_id) in self.accounts:
            return self.accounts[str(account_id)]
        composite_key = f"{firm_name}: {account_name}"
        if composite_key in self.accounts:
            return self.accounts[composite_key]
        if account_name and account_name in self.accounts:
            return self.accounts[account_name]

        return slugify_account_name(firm_name, account_name, actual_type, is_asset=kwargs.get("is_asset"))

    def resolve_category_or_payee(
        self,
        category: Optional[str] = None,
        description: Optional[str] = None,
        is_spending: bool = True,
        is_income: bool = False,
        category_id: Optional[Union[str, int]] = None,
        transaction_type: Optional[str] = None,
        memo: Optional[str] = None,
    ) -> str:
        """Resolve the offsetting balancing leg for a transaction.

        Order of precedence:
        1. Regex payee rules (matching description and memo).
        2. Category name overrides from mapping.
        3. Category ID overrides from mapping.
        4. Transfer detection (non-income and non-spending, or transfer keywords) -> Equity:Transfers.
        5. Income transactions -> Income:Uncategorized.
        6. Spending transactions -> Expenses:Uncategorized.
        7. Default fallback -> Expenses:Uncategorized.
        """
        combined = f"{description or ''} {memo or ''}".strip()
        desc_clean = clean_api_text(combined)

        # 1. Match regex payee rules first
        for rule in self.regex_rules:
            pattern = rule.get("pattern", "")
            target_acct = rule.get("account", "")
            if pattern and target_acct:
                try:
                    if re.search(pattern, desc_clean):
                        return target_acct
                except re.error as e:
                    logger.warning("Invalid regex rule pattern '%s': %s", pattern, e)

        # 2. Match category name if available
        cat_clean = clean_api_text(category or "")
        if cat_clean and cat_clean in self.categories:
            return self.categories[cat_clean]

        # 3. Match category ID if available
        if category_id is not None and str(category_id) in self.categories:
            return self.categories[str(category_id)]

        # 4. Detect transfers (non-income and non-spending, or transfer indicators in type/category/memo)
        type_clean = (transaction_type or "").lower()
        desc_lower = desc_clean.lower()
        cat_lower = cat_clean.lower()
        is_transfer = (
            (not is_income and not is_spending)
            or "transfer" in type_clean
            or "transfer" in cat_lower
            or "transfer" in desc_lower
            or "payment" in type_clean
        )
        if is_transfer:
            return "Equity:Transfers"

        # 5. Fallbacks based on transaction type
        if is_income:
            return "Income:Uncategorized"
        if is_spending:
            return "Expenses:Uncategorized"

        return "Equity:Transfers"


def _calculate_account_transaction_totals(
    transactions: Optional[DashboardTransactions],
    mapper: BeancountMapper,
    acct_lookup: Dict[str, Dict[str, Any]],
) -> Dict[str, float]:
    """Calculate accumulated net posting amounts for each primary account across transactions."""
    totals: Dict[str, float] = {}
    if not transactions or not transactions.transactions:
        return totals

    for tx in transactions.transactions:
        aid = str(tx.get("account_id") or "")
        uaid = str(tx.get("user_account_id") or "")
        t_name = tx.get("account_name") or ""

        acct_info = acct_lookup.get(aid) or acct_lookup.get(uaid) or acct_lookup.get(t_name)
        if acct_info:
            firm = acct_info.get("firm_name") or "Institution"
            name = acct_info.get("account_name") or t_name or "Account"
            acct_type = acct_info.get("account_type") or "bank"
            resolved_id = str(acct_info.get("account_id") or aid)
            is_asset = acct_info.get("is_asset", True)
        else:
            firm = tx.get("firm_name") or "Institution"
            name = t_name or "Account"
            acct_type = tx.get("account_type") or "bank"
            resolved_id = aid
            is_asset = True

        primary_account = mapper.resolve_account(firm, name, resolved_id, acct_type)
        amount = float(tx.get("amount") or 0.0)
        is_credit = bool(tx.get("is_credit"))
        is_cash_in = bool(tx.get("is_cash_in"))
        is_income = bool(tx.get("is_income"))
        is_liability = primary_account.startswith("Liabilities") or (not is_asset)

        if is_liability:
            acct_amount = amount if (is_credit or is_cash_in) else -amount
        else:
            acct_amount = amount if (is_credit or is_cash_in or is_income) else -amount

        totals[primary_account] = totals.get(primary_account, 0.0) + acct_amount

    return totals


def _determine_opening_date(
    transactions: Optional[DashboardTransactions] = None,
    balances: Optional[DashboardBalances] = None,
    opening_date: Optional[str] = None,
) -> Optional[str]:
    """Determine initial opening date for baseline holdings lots.

    If an explicit opening_date is provided, use it. Otherwise, look for the earliest
    transaction date and use min("2020-01-01", earliest_date). If no transactions exist
    but balances exist, fallback to "2020-01-01".
    """
    if opening_date:
        return opening_date

    if transactions and transactions.transactions:
        tx_dates = [
            t.get("transaction_date") or t.get("date")
            for t in transactions.transactions
            if (t.get("transaction_date") or t.get("date"))
        ]
        if tx_dates:
            earliest_tx = min(tx_dates)
            return min("2020-01-01", str(earliest_tx))
        return "2020-01-01"

    if balances and balances.accounts:
        return "2020-01-01"

    return None


def _ensure_fifo_booking_method(content: str) -> str:
    """Inject a FIFO ``booking_method`` option into an existing ledger header lacking one.

    Appending the newly supported sale transactions (whose reductions carry an
    empty ``{}`` cost spec) to a ledger generated by an earlier release — one
    written before FIFO was emitted — would otherwise fail: Beancount's strict
    default booking rejects an ambiguous reduction when a symbol has multiple
    open lots. The option is placed next to any existing ``option`` directives so
    the header stays readable; Beancount applies booking globally regardless of
    position. Content already declaring a booking method is returned unchanged.
    """
    if 'option "booking_method"' in content:
        return content
    option_line = 'option "booking_method" "FIFO"\n'
    lines = content.splitlines(keepends=True)
    insert_at: Optional[int] = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith('option "'):
            insert_at = i + 1
    if insert_at is not None:
        lines.insert(insert_at, option_line)
        return "".join(lines)
    # No existing option directives — prepend so the option precedes any usage.
    return option_line + content


class BeancountGenerator:
    """Generates Beancount directives and ledger files from Empower data models."""

    def __init__(self, mapper: Optional[BeancountMapper] = None):
        self.mapper = mapper or BeancountMapper()

    def generate_main_bean(self) -> str:
        """Generate the root main.bean linking modular components."""
        return (
            ";; ==============================================================================\n"
            ";; Empower Personal Dashboard - Root Beancount Ledger\n"
            ";; ==============================================================================\n\n"
            'option "title" "Empower Personal Dashboard Ledger"\n'
            'option "operating_currency" "USD"\n'
            'option "booking_method" "FIFO"\n\n'
            'plugin "beancount.plugins.auto_accounts"\n\n'
            ';; Fava Web Dashboard Display Configuration\n'
            '1970-01-01 custom "fava-option" "invert-income-liabilities-equity" "true"\n\n'
            'include "accounts.bean"\n'
            'include "balances.bean"\n'
            'include "holdings.bean"\n'
            'include "prices.bean"\n'
            'include "transactions.bean"\n'
        )

    def generate_accounts_bean(
        self,
        balances: Optional[DashboardBalances] = None,
        holdings: Optional[DashboardHoldings] = None,
        transactions: Optional[DashboardTransactions] = None,
        include_pads: bool = True,
    ) -> str:
        """Generate account open and pad directives across all provided datasets."""
        lines = [
            ";; ==============================================================================\n"
            ";; Empower Personal Dashboard - Account Declarations\n"
            ";; ==============================================================================\n\n"
            ";; Standard Equity & Suspense Accounts\n"
            "2000-01-01 open Equity:Opening-Balances USD\n"
            "2000-01-01 open Equity:Transfers USD\n"
            "2000-01-01 open Expenses:Uncategorized USD\n"
            "2000-01-01 open Expenses:Fees USD\n"
            "2000-01-01 open Income:Uncategorized USD\n"
            "2000-01-01 open Income:CapitalGains\n"
            "2000-01-01 open Income:Dividends USD\n\n"
            ";; Linked Institution Accounts & Pads\n"
        ]

        seen_accounts: Set[str] = {
            "Equity:Opening-Balances",
            "Equity:Transfers",
            "Expenses:Uncategorized",
            "Expenses:Fees",
            "Income:Uncategorized",
            "Income:CapitalGains",
            "Income:Dividends",
        }
        acct_lookup = _build_account_lookup(balances)

        # Collect accounts that hold commodities from holdings
        commodity_accounts: Set[str] = set()
        if holdings and holdings.holdings:
            for h in holdings.holdings:
                uaid = str(h.get("user_account_id") or "")
                h_name = h.get("account_name") or ""
                acct_info = acct_lookup.get(uaid) or acct_lookup.get(h_name)
                if acct_info:
                    h_firm = acct_info.get("firm_name") or "Brokerage"
                    acct_name = acct_info.get("account_name") or h_name or "Brokerage"
                    h_type = acct_info.get("account_type") or "investment"
                    h_id = str(acct_info.get("account_id") or uaid)
                else:
                    h_firm = h.get("firm_name") or "Brokerage"
                    acct_name = h_name or "Brokerage"
                    h_type = "investment"
                    h_id = uaid
                b_acct = self.mapper.resolve_account(h_firm, acct_name, h_id, h_type, is_asset=True)
                commodity_accounts.add(b_acct)

        # 1. Accounts from balances
        if balances and balances.accounts:
            for acct in balances.accounts:
                firm = acct.get("firm_name") or "Institution"
                name = acct.get("account_name") or "Account"
                acct_id = str(acct.get("account_id") or "")
                acct_type = acct.get("account_type") or "bank"
                curr = acct.get("currency") or "USD"
                raw_bal = float(acct.get("balance", 0.0))

                b_account = self.mapper.resolve_account(firm, name, acct_id, acct_type)
                if b_account not in seen_accounts:
                    seen_accounts.add(b_account)
                    if b_account in commodity_accounts or any(
                        inv_word in str(acct_type).lower()
                        for inv_word in ("invest", "broker", "ira", "401k", "roth", "rollover", "stock", "portfolio", "529", "other")
                    ):
                        lines.append(f"2000-01-01 open {b_account}\n")
                    else:
                        lines.append(f"2000-01-01 open {b_account} {curr}\n")
                    if abs(raw_bal) > 0.001 and include_pads:
                        lines.append(f"2000-01-01 pad {b_account} Equity:Opening-Balances\n")

        # 2. Accounts from holdings
        if holdings and holdings.holdings:
            for h in holdings.holdings:
                uaid = str(h.get("user_account_id") or "")
                h_name = h.get("account_name") or ""
                acct_info = acct_lookup.get(uaid) or acct_lookup.get(h_name)
                if acct_info:
                    firm = acct_info.get("firm_name") or "Brokerage"
                    name = acct_info.get("account_name") or h_name or "Brokerage"
                    acct_type = acct_info.get("account_type") or "investment"
                    acct_id = str(acct_info.get("account_id") or uaid)
                else:
                    firm = h.get("firm_name") or "Brokerage"
                    name = h_name or "Brokerage"
                    acct_type = "investment"
                    acct_id = uaid

                b_account = self.mapper.resolve_account(firm, name, acct_id, acct_type)
                if b_account not in seen_accounts:
                    seen_accounts.add(b_account)
                    lines.append(f"2000-01-01 open {b_account}\n")
                    if include_pads:
                        lines.append(f"2000-01-01 pad {b_account} Equity:Opening-Balances\n")

        # 3. Accounts from transactions
        if transactions and transactions.transactions:
            # Pre-pass: identify transaction accounts that receive commodity
            # (ticker) postings, so their `open` is left currency-unconstrained.
            commodity_tx_accounts: Set[str] = set()
            for tx in transactions.transactions:
                if not _clean_ticker(tx.get("symbol")):
                    continue
                tt = clean_api_text(tx.get("transaction_type") or "").strip().lower()
                it = clean_api_text(tx.get("investment_type") or "").strip().lower()
                if not (
                    tt in ("buy", "sell", "reinvest", "disposal")
                    or it in ("buy", "sell", "reinvest", "disposal")
                ):
                    continue
                firm, name, acct_type, resolved_id = _resolve_account_from_tx(tx, acct_lookup)
                commodity_tx_accounts.add(
                    self.mapper.resolve_account(firm, name, resolved_id, acct_type)
                )

            for tx in transactions.transactions:
                firm, name, acct_type, resolved_id = _resolve_account_from_tx(tx, acct_lookup)

                b_account = self.mapper.resolve_account(firm, name, resolved_id, acct_type)
                if b_account not in seen_accounts:
                    seen_accounts.add(b_account)
                    # Investment accounts may hold ticker commodities, so leave
                    # them currency-unconstrained; cash-only accounts stay USD.
                    if b_account in commodity_accounts or b_account in commodity_tx_accounts or any(
                        inv_word in str(acct_type).lower()
                        for inv_word in ("invest", "broker", "ira", "401k", "roth", "rollover", "stock", "portfolio", "529", "other")
                    ):
                        lines.append(f"2000-01-01 open {b_account}\n")
                    else:
                        lines.append(f"2000-01-01 open {b_account} USD\n")

        # 4. Open any custom mapped accounts, categories, and regex rule accounts
        for custom_acct in self.mapper.accounts.values():
            if custom_acct not in seen_accounts:
                seen_accounts.add(custom_acct)
                lines.append(f"2000-01-01 open {custom_acct}\n")

        for cat_acct in self.mapper.categories.values():
            if cat_acct not in seen_accounts:
                seen_accounts.add(cat_acct)
                lines.append(f"2000-01-01 open {cat_acct}\n")

        for r in self.mapper.regex_rules:
            r_acct = r.get("account")
            if r_acct and r_acct not in seen_accounts:
                seen_accounts.add(r_acct)
                lines.append(f"2000-01-01 open {r_acct}\n")

        # 5. Open any category / balancing accounts resolved from transactions
        if transactions and transactions.transactions:
            for tx in transactions.transactions:
                cat_acct = self.mapper.resolve_category_or_payee(
                    category=tx.get("category_name"),
                    description=tx.get("description"),
                    is_spending=bool(tx.get("is_spending", False)),
                    is_income=bool(tx.get("is_income", False)),
                    category_id=tx.get("category_id"),
                    transaction_type=tx.get("transaction_type"),
                    memo=tx.get("original_description"),
                )
                if cat_acct and cat_acct not in seen_accounts:
                    seen_accounts.add(cat_acct)
                    lines.append(f"2000-01-01 open {cat_acct}\n")

        return "".join(lines)

    def generate_balances_bean(
        self,
        balances: Optional[DashboardBalances] = None,
        holdings: Optional[DashboardHoldings] = None,
        transactions: Optional[DashboardTransactions] = None,
    ) -> str:
        """Generate balance assertions for cash, liabilities, and investment commodities."""
        lines = [
            ";; ==============================================================================\n"
            ";; Empower Personal Dashboard - Balance Assertions\n"
            ";; ==============================================================================\n\n"
        ]

        acct_lookup = _build_account_lookup(balances)

        # Collect accounts that hold commodities from holdings
        commodity_accounts: Set[str] = set()
        if holdings and holdings.holdings:
            for h in holdings.holdings:
                uaid = str(h.get("user_account_id") or "")
                h_name = h.get("account_name") or ""
                acct_info = acct_lookup.get(uaid) or acct_lookup.get(h_name)
                if acct_info:
                    h_firm = acct_info.get("firm_name") or "Brokerage"
                    acct_name = acct_info.get("account_name") or h_name or "Brokerage"
                    h_type = acct_info.get("account_type") or "investment"
                    h_id = str(acct_info.get("account_id") or uaid)
                else:
                    h_firm = h.get("firm_name") or "Brokerage"
                    acct_name = h_name or "Brokerage"
                    h_type = "investment"
                    h_id = uaid
                b_acct = self.mapper.resolve_account(h_firm, acct_name, h_id, h_type, is_asset=True)
                commodity_accounts.add(b_acct)

        tx_totals = _calculate_account_transaction_totals(transactions, self.mapper, acct_lookup) if transactions else {}

        if balances and balances.accounts:
            as_of = balances.as_of_date
            lines.append(f";; Cash & Liability Balances (as of {as_of})\n")
            for acct in balances.accounts:
                firm = acct.get("firm_name") or "Institution"
                name = acct.get("account_name") or "Account"
                acct_id = str(acct.get("account_id") or "")
                acct_type = acct.get("account_type") or "bank"
                is_asset = acct.get("is_asset", True)
                curr = acct.get("currency") or "USD"
                raw_bal = float(acct.get("balance", 0.0))

                b_account = self.mapper.resolve_account(firm, name, acct_id, acct_type)
                # If account holds commodities, its total balance is portfolio value rather than USD cash.
                # Commodity positions are asserted separately below to avoid double-counting.
                if b_account in commodity_accounts:
                    continue

                # In Beancount, liabilities (credit cards, loans, mortgages) are represented as negative balances
                bal_amt = raw_bal if is_asset else -abs(raw_bal)
                accumulated = tx_totals.get(b_account, 0.0) if transactions else 0.0
                diff = bal_amt - accumulated
                if abs(diff) > 0.005:
                    lines.append(f"2020-01-01 pad {b_account} Equity:Opening-Balances\n")
                lines.append(f"{as_of} balance {b_account} {bal_amt:.2f} {curr}\n")

        if holdings and holdings.holdings:
            as_of = holdings.as_of_date
            # Beancount evaluates balance directives at the beginning of the day,
            # so a trade dated on as_of has not yet posted when an assertion dated
            # as_of is checked. Asserting the snapshot on the following day counts
            # all same-day activity and keeps the reconstructed inventory exact.
            try:
                assert_date = (
                    datetime.date.fromisoformat(as_of) + datetime.timedelta(days=1)
                ).isoformat()
            except Exception:
                assert_date = as_of
            lines.append(f"\n;; Investment Commodity Unit Balances (snapshot as of {as_of}, asserted {assert_date})\n")
            # Aggregate positions by (b_account, ticker) to avoid duplicate conflicting balance assertions
            holding_units: Dict[Tuple[str, str], float] = {}
            for h in holdings.holdings:
                ticker = _clean_ticker(h.get("ticker"))
                qty = float(h.get("quantity") or 0.0)
                uaid = str(h.get("user_account_id") or "")
                h_name = h.get("account_name") or ""

                acct_info = acct_lookup.get(uaid) or acct_lookup.get(h_name)
                if acct_info:
                    firm = acct_info.get("firm_name") or "Brokerage"
                    acct_name = acct_info.get("account_name") or h_name or "Brokerage"
                    acct_type = acct_info.get("account_type") or "investment"
                    acct_id = str(acct_info.get("account_id") or uaid)
                else:
                    firm = h.get("firm_name") or "Brokerage"
                    acct_name = h_name or "Brokerage"
                    acct_type = "investment"
                    acct_id = uaid

                if ticker and qty > 0:
                    b_account = self.mapper.resolve_account(firm, acct_name, acct_id, account_type=acct_type)
                    holding_units[(b_account, ticker)] = holding_units.get((b_account, ticker), 0.0) + qty

            for (b_account, ticker), total_qty in sorted(holding_units.items()):
                lines.append(f"{assert_date} balance {b_account} {_format_quantity(total_qty)} {ticker}\n")

        return "".join(lines)

    def generate_prices_bean(
        self,
        holdings: Optional[DashboardHoldings] = None,
    ) -> str:
        """Generate price directives for investment commodities."""
        lines = [
            ";; ==============================================================================\n"
            ";; Empower Personal Dashboard - Commodity Price Points\n"
            ";; ==============================================================================\n\n"
        ]

        if holdings and holdings.holdings:
            as_of = holdings.as_of_date
            seen_tickers: Set[str] = set()
            for h in holdings.holdings:
                ticker = _clean_ticker(h.get("ticker"))
                price = float(h.get("price") or 0.0)
                if ticker and price > 0 and ticker not in seen_tickers:
                    seen_tickers.add(ticker)
                    lines.append(f"{as_of} price {ticker} {_format_price(price)} USD\n")

        return "".join(lines)

    def generate_holdings_bean(
        self,
        holdings: Optional[DashboardHoldings] = None,
        balances: Optional[DashboardBalances] = None,
        existing_content: Optional[str] = None,
        opening_date: Optional[str] = None,
        *,
        transactions: Optional[DashboardTransactions] = None,
    ) -> str:
        """Generate investment positions with lot cost-basis and price tracking."""
        lines = [
            ";; ==============================================================================\n"
            ";; Empower Personal Dashboard - Portfolio Holdings & Lots\n"
            ";; ==============================================================================\n\n"
        ]

        # Must reconstruct opening lots for fully-sold positions even if holdings snapshot is empty,
        # or sells will reduce an empty inventory and the ledger will fail to load.
        # Early return moved to end of function, after reconstruction setup.

        as_of = holdings.as_of_date if holdings else None
        effective_opening = opening_date or _determine_opening_date(transactions, balances, None)
        if effective_opening:
            lot_date = effective_opening
        else:
            # Date holdings snapshot transaction before the balance assertion date so Beancount's
            # beginning-of-day balance assertion on as_of passes cleanly.
            if as_of:
                try:
                    d = datetime.date.fromisoformat(as_of)
                    lot_date = (d - datetime.timedelta(days=1)).isoformat()
                except Exception:
                    lot_date = as_of
            else:
                lot_date = "2020-01-01"

        acct_lookup = _build_account_lookup(balances)

        # Existing holding tags to skip on append
        existing_keys: Set[str] = set()
        if existing_content:
            existing_keys = set(re.findall(r'empower_holding:\s*"([^"]+)"', existing_content))

        # Calculate net transaction buys per (b_account, ticker) across the transaction history
        net_buys: Dict[Tuple[str, str], float] = {}
        # Per-key metadata (firm + a representative price) used to reconstruct
        # synthetic opening lots for securities fully sold within the window.
        net_buy_meta: Dict[Tuple[str, str], Dict[str, Any]] = {}
        if transactions and transactions.transactions:
            for tx in transactions.transactions:
                sym = tx.get("symbol")
                if not sym:
                    continue
                t_ticker = _clean_ticker(sym)
                if not t_ticker:
                    continue

                tx_raw_qty = tx.get("quantity")
                tx_qty = abs(float(tx_raw_qty)) if tx_raw_qty is not None else 0.0
                if tx_qty <= 0:
                    continue

                tx_raw_price = tx.get("price")
                tx_price = abs(float(tx_raw_price)) if tx_raw_price is not None else 0.0

                aid = str(tx.get("account_id") or "")
                uaid = str(tx.get("user_account_id") or "")
                t_name = tx.get("account_name") or ""
                acct_info = acct_lookup.get(aid) or acct_lookup.get(uaid) or acct_lookup.get(t_name)
                if acct_info:
                    tx_firm = acct_info.get("firm_name") or "Brokerage"
                    tx_acct_name = acct_info.get("account_name") or t_name or "Brokerage"
                    tx_acct_type = acct_info.get("account_type") or "investment"
                    tx_aid = str(acct_info.get("account_id") or aid)
                else:
                    tx_firm = tx.get("firm_name") or "Brokerage"
                    tx_acct_name = t_name or "Brokerage"
                    tx_acct_type = tx.get("account_type") or "investment"
                    tx_aid = aid

                tx_b_account = self.mapper.resolve_account(tx_firm, tx_acct_name, tx_aid, account_type=tx_acct_type)
                tx_type = clean_api_text(tx.get("transaction_type") or "").strip().lower()
                inv_type = clean_api_text(tx.get("investment_type") or "").strip().lower()

                key = (tx_b_account, t_ticker)
                is_tx_buy = tx_type in ("buy", "reinvest") or inv_type in ("buy", "reinvest")
                is_tx_sell = tx_type in ("sell", "disposal") or inv_type in ("sell", "disposal")
                if is_tx_buy:
                    net_buys[key] = net_buys.get(key, 0.0) + tx_qty
                elif is_tx_sell:
                    net_buys[key] = net_buys.get(key, 0.0) - tx_qty

                if is_tx_buy or is_tx_sell:
                    meta = net_buy_meta.setdefault(
                        key, {"firm": tx_firm, "buy_price": 0.0, "any_price": 0.0}
                    )
                    if tx_price > 0:
                        if meta["any_price"] <= 0:
                            meta["any_price"] = tx_price
                        if is_tx_buy and meta["buy_price"] <= 0:
                            meta["buy_price"] = tx_price

        # Aggregate positions by (b_account, ticker) to emit each snapshot position only once
        aggregated_holdings: Dict[Tuple[str, str], Dict[str, Any]] = {}

        if holdings and holdings.holdings:
            for h in holdings.holdings:
                ticker = _clean_ticker(h.get("ticker"))
                qty = float(h.get("quantity") or 0.0)
                price = float(h.get("price") or 0.0)
                cost_basis = h.get("cost_basis")
                uaid = str(h.get("user_account_id") or "")
                h_name = h.get("account_name") or ""

                acct_info = acct_lookup.get(uaid) or acct_lookup.get(h_name)
                if acct_info:
                    firm = acct_info.get("firm_name") or "Brokerage"
                    acct_name = acct_info.get("account_name") or h_name or "Brokerage"
                    acct_type = acct_info.get("account_type") or "investment"
                    acct_id = str(acct_info.get("account_id") or uaid)
                else:
                    firm = h.get("firm_name") or "Brokerage"
                    acct_name = h_name or "Brokerage"
                    acct_type = "investment"
                    acct_id = uaid

                if ticker and qty > 0:
                    b_account = self.mapper.resolve_account(firm, acct_name, acct_id, account_type=acct_type)
                    key = (b_account, ticker)
                    if key not in aggregated_holdings:
                        aggregated_holdings[key] = {
                            "firm": firm,
                            "b_account": b_account,
                            "ticker": ticker,
                            "quantity": qty,
                            "price": price,
                            "cost_basis": float(cost_basis) if cost_basis is not None else None,
                        }
                    else:
                        agg = aggregated_holdings[key]
                        agg["quantity"] += qty
                        if cost_basis is not None:
                            curr_cb = agg["cost_basis"] or 0.0
                            agg["cost_basis"] = curr_cb + float(cost_basis)
                        if price > 0:
                            agg["price"] = price

        for pos in sorted(aggregated_holdings.values(), key=lambda p: (p["b_account"], p["ticker"])):
            b_account = pos["b_account"]
            ticker = pos["ticker"]
            firm = pos["firm"]
            snapshot_qty = pos["quantity"]
            price = pos["price"]
            cost_basis = pos["cost_basis"]

            key = (b_account, ticker)
            if transactions is not None and transactions.transactions:
                nb = net_buys.get(key, 0.0)
                baseline_qty = snapshot_qty - nb
            else:
                baseline_qty = snapshot_qty

            # If Baseline Opening Qty <= 0: position was acquired entirely within transaction window
            if baseline_qty <= 0.000001:
                continue

            holding_tag = f"{b_account}:{ticker}"
            if holding_tag in existing_keys:
                continue

            payee_esc = _escape_beancount_string(f"{firm} Portfolio Snapshot")
            narration_esc = _escape_beancount_string(f"{ticker} Position")
            lines.append(f'{lot_date} * "{payee_esc}" "{narration_esc}"\n')
            lines.append(f'  empower_holding: "{holding_tag}"\n')

            qty = baseline_qty
            if cost_basis is not None and float(cost_basis) > 0 and snapshot_qty > 0:
                scaled_cost_basis = float(cost_basis) * qty / snapshot_qty
                lines.append(
                    f"  {b_account:<36} {_format_quantity(qty):>10} {ticker} "
                    f"{{{{{_format_cost(scaled_cost_basis)} USD}}}}\n"
                )
            else:
                lines.append(
                    f"  {b_account:<36} {_format_quantity(qty):>10} {ticker} @ {_format_price(price)} USD\n"
                )
            lines.append(f"  {'Equity:Opening-Balances':<36}\n\n")

        # Reconstruct opening lots for securities that existed before the window
        # and were fully sold within it. Such a position is absent from the
        # current holdings snapshot, yet its net buys are negative; without a
        # synthetic opening lot the reconstructed sales would reduce an empty
        # inventory and the historical ledger would fail to load.
        if transactions is not None and transactions.transactions:
            for key in sorted(net_buys):
                if key in aggregated_holdings:
                    continue
                nb = net_buys.get(key, 0.0)
                if nb >= -0.000001:
                    continue

                b_account, ticker = key
                holding_tag = f"{b_account}:{ticker}"
                if holding_tag in existing_keys:
                    continue

                baseline_qty = -nb
                meta = net_buy_meta.get(key, {})
                firm = meta.get("firm") or "Brokerage"
                opening_price = meta.get("buy_price") or meta.get("any_price") or 0.0

                payee_esc = _escape_beancount_string(f"{firm} Portfolio Snapshot")
                narration_esc = _escape_beancount_string(f"{ticker} Opening Position")
                lines.append(f'{lot_date} * "{payee_esc}" "{narration_esc}"\n')
                lines.append(f'  empower_holding: "{holding_tag}"\n')
                if opening_price > 0:
                    lines.append(
                        f"  {b_account:<36} {_format_quantity(baseline_qty):>10} {ticker} "
                        f"{{{_format_cost(opening_price)} USD}}\n"
                    )
                else:
                    lines.append(
                        f"  {b_account:<36} {_format_quantity(baseline_qty):>10} {ticker}\n"
                    )
                lines.append(f"  {'Equity:Opening-Balances':<36}\n\n")

        return "".join(lines)

    def generate_transactions_bean(
        self,
        transactions: DashboardTransactions,
        balances: Optional[DashboardBalances] = None,
    ) -> str:
        """Generate balanced double-entry transaction directives."""
        lines = [
            ";; ==============================================================================\n"
            ";; Empower Personal Dashboard - Double-Entry Transactions\n"
            ";; ==============================================================================\n\n"
        ]

        if not transactions or not transactions.transactions:
            return "".join(lines)

        acct_lookup = _build_account_lookup(balances)

        for tx in transactions.transactions:
            tx_id = str(tx.get("user_transaction_id") or "")
            acct_id = str(tx.get("account_id") or "")
            uaid = str(tx.get("user_account_id") or "")
            t_name = tx.get("account_name") or ""

            # Resolve true account identity and type against balances lookup if available.
            # Use _resolve_account_from_tx to ensure investment transactions consistently
            # fall back to the brokerage identity (matching holdings reconciliation and
            # assertions) so their postings reconcile in one account.
            firm, acct_name, acct_type, resolved_id = _resolve_account_from_tx(tx, acct_lookup)
            acct_info = acct_lookup.get(acct_id) or acct_lookup.get(uaid) or acct_lookup.get(t_name)
            if acct_info:
                is_asset = acct_info.get("is_asset", True)
            else:
                type_lower = acct_type.lower()
                is_asset = not (
                    "credit" in type_lower
                    or "loan" in type_lower
                    or "mortgage" in type_lower
                    or "liabilit" in type_lower
                )

            date = tx.get("transaction_date") or "2000-01-01"
            cat_name = tx.get("category") or tx.get("category_name")
            amount = abs(float(tx.get("amount") or 0.0))

            is_credit = bool(tx.get("is_credit"))
            is_cash_in = bool(tx.get("is_cash_in"))
            is_income = bool(tx.get("is_income"))
            is_spending = bool(tx.get("is_spending", False))

            primary_account = self.mapper.resolve_account(firm, acct_name, resolved_id, acct_type)

            # Investment transaction detection
            raw_tx_type = tx.get("transaction_type") or ""
            tx_type_clean = clean_api_text(raw_tx_type).strip().lower()
            raw_inv_type = tx.get("investment_type") or ""
            inv_type_clean = clean_api_text(raw_inv_type).strip().lower()
            cat_clean = clean_api_text(cat_name or "").lower()
            symbol = tx.get("symbol")
            ticker = _clean_ticker(symbol)
            raw_qty = tx.get("quantity")
            raw_price = tx.get("price")

            qty = abs(float(raw_qty)) if raw_qty is not None else 0.0
            price = abs(float(raw_price)) if raw_price is not None else 0.0

            link_id = tx_id[3:] if tx_id.startswith("tx-") else tx_id
            link_id_clean = re.sub(r"[^A-Za-z0-9\-]", "", link_id)
            tag_str = f" ^empower-tx-{link_id_clean}" if link_id_clean else ""

            is_buy = (
                (tx_type_clean in ("buy", "reinvest") or inv_type_clean in ("buy", "reinvest"))
                and bool(ticker)
                and qty > 0
            )
            is_sell = (
                (tx_type_clean in ("sell", "disposal") or inv_type_clean in ("sell", "disposal"))
                and bool(ticker)
                and qty > 0
            )
            is_dividend = (
                "dividend" in tx_type_clean
                or "dividend" in inv_type_clean
                # A bare "dividend" category needs investment evidence (a ticker
                # plus a transaction/investment type) before it is treated as an
                # investment dividend; otherwise ordinary banking transactions
                # labelled "dividend" would skip category resolution.
                or ("dividend" in cat_clean and bool(ticker) and bool(tx_type_clean or inv_type_clean))
            )

            # Resolve payee and narration for investment transactions
            if is_buy or is_sell or is_dividend:
                if firm and firm != "Institution":
                    inv_payee = _escape_beancount_string(firm)
                else:
                    inv_payee = _escape_beancount_string(tx.get("description") or "Brokerage")
                inv_narration = _escape_beancount_string(tx.get("description") or "")

                if is_buy:
                    reported_amount = amount
                    if price == 0 and amount > 0 and qty > 0:
                        price = amount / qty
                    # Calculate lot cost from qty × price; preserve full reported amount
                    # as the cash outflow. If reported amount exceeds lot cost, the
                    # difference is recorded as an Expenses:Fees posting.
                    calculated_cost = round(qty * price, 2) if price > 0 and qty > 0 else 0.0
                    fee = round(reported_amount - calculated_cost, 2) if calculated_cost > 0 else 0.0
                    # Reinvested dividends are funded by dividend income, not by
                    # brokerage cash; routing them through the cash-outflow leg
                    # would wrongly drain USD and omit the dividend income.
                    is_reinvest = tx_type_clean == "reinvest" or inv_type_clean == "reinvest"
                    funding_account = (
                        (self.mapper.categories.get("Dividends") or "Income:Dividends")
                        if is_reinvest
                        else primary_account
                    )
                    lines.append(f'{date} * "{inv_payee}" "{inv_narration}"{tag_str}\n')
                    if tx_id:
                        lines.append(f'  empower_id: "{_escape_beancount_string(tx_id)}"\n')
                    if acct_id:
                        lines.append(f'  empower_account_id: "{_escape_beancount_string(acct_id)}"\n')
                    lines.append(
                        f"  {primary_account:<36} {_format_quantity(qty):>10} {ticker} "
                        f"{{{_format_price(price)} USD}}\n"
                    )
                    lines.append(f"  {funding_account:<36} {-reported_amount:>8.2f} USD\n")
                    if fee > 0:
                        fees_acct = self.mapper.categories.get("Fees") or "Expenses:Fees"
                        lines.append(f"  {fees_acct:<36} {fee:>8.2f} USD\n")
                    lines.append("\n")
                    continue

                if is_sell:
                    if price == 0 and amount > 0 and qty > 0:
                        price = amount / qty
                    if amount == 0 and price > 0 and qty > 0:
                        amount = round(qty * price, 2)
                    cap_gains_acct = self.mapper.categories.get("Capital Gains") or "Income:CapitalGains"
                    lines.append(f'{date} * "{inv_payee}" "{inv_narration}"{tag_str}\n')
                    if tx_id:
                        lines.append(f'  empower_id: "{_escape_beancount_string(tx_id)}"\n')
                    if acct_id:
                        lines.append(f'  empower_account_id: "{_escape_beancount_string(acct_id)}"\n')
                    lines.append(
                        f"  {primary_account:<36} -{_format_quantity(qty)} {ticker} {{}} "
                        f"@ {_format_price(price)} USD\n"
                    )
                    lines.append(f"  {primary_account:<36} {amount:>8.2f} USD\n")
                    lines.append(f"  {cap_gains_acct}\n\n")
                    continue

                if is_dividend:
                    dividend_acct = self.mapper.categories.get("Dividends") or "Income:Dividends"
                    lines.append(f'{date} * "{inv_payee}" "{inv_narration}"{tag_str}\n')
                    if tx_id:
                        lines.append(f'  empower_id: "{_escape_beancount_string(tx_id)}"\n')
                    if acct_id:
                        lines.append(f'  empower_account_id: "{_escape_beancount_string(acct_id)}"\n')
                    lines.append(f"  {primary_account:<36} {amount:>8.2f} USD\n")
                    lines.append(f"  {dividend_acct:<36} {-amount:>8.2f} USD\n\n")
                    continue

            # Standard banking / spending / income transaction
            payee = _escape_beancount_string(tx.get("description") or "Unknown Payee")
            narration = _escape_beancount_string(cat_name or tx.get("original_description") or "")

            category_account = self.mapper.resolve_category_or_payee(
                category=cat_name,
                description=tx.get("description"),
                is_spending=is_spending,
                is_income=is_income,
                category_id=tx.get("category_id"),
                transaction_type=tx.get("transaction_type"),
                memo=tx.get("original_description"),
            )

            # Determine double-entry posting signs
            is_liability = primary_account.startswith("Liabilities") or (not is_asset)

            if is_liability:
                if is_credit or is_cash_in:
                    acct_amount = amount
                    bal_amount = -amount
                else:
                    acct_amount = -amount
                    bal_amount = amount
            elif is_credit or is_cash_in or is_income:
                acct_amount = amount
                bal_amount = -amount
            else:
                acct_amount = -amount
                bal_amount = amount

            lines.append(f'{date} * "{payee}" "{narration}"{tag_str}\n')
            if tx_id:
                lines.append(f'  empower_id: "{_escape_beancount_string(tx_id)}"\n')
            if acct_id:
                lines.append(f'  empower_account_id: "{_escape_beancount_string(acct_id)}"\n')
            lines.append(f"  {primary_account:<36} {acct_amount:>8.2f} USD\n")
            lines.append(f"  {category_account:<36} {bal_amount:>8.2f} USD\n\n")

        return "".join(lines)

    def export_modular_ledger(
        self,
        destination_dir: Union[str, Path],
        balances: Optional[DashboardBalances] = None,
        holdings: Optional[DashboardHoldings] = None,
        transactions: Optional[DashboardTransactions] = None,
        append: bool = True,
        opening_date: Optional[str] = None,
    ) -> List[Path]:
        """Export complete modular ledger directory (main.bean, accounts.bean, etc.).

        Safely avoids overwriting existing ledgers when append=True by preserving existing
        transactions and accounts.
        """
        dest = Path(destination_dir).expanduser().resolve()
        if dest.is_symlink():
            raise ValueError(f"Destination directory cannot be a symlink: {dest}")
        dest.mkdir(parents=True, exist_ok=True)
        created_files: List[Path] = []

        def _verify_file_symlink(p: Path) -> None:
            if p.is_symlink():
                raise ValueError(f"Refusing to write to symlinked ledger target: {p}")

        effective_opening_date = _determine_opening_date(transactions, balances, opening_date)

        # 1. main.bean
        main_path = dest / "main.bean"
        _verify_file_symlink(main_path)
        if not main_path.exists() or not append:
            main_path.write_text(self.generate_main_bean(), encoding="utf-8")
        else:
            current_main = main_path.read_text(encoding="utf-8")
            updated_main = current_main
            # Ensure holdings.bean include is present if missing
            if 'include "holdings.bean"' not in updated_main:
                updated_main += '\ninclude "holdings.bean"\n'
            # Ensure a FIFO booking method is declared so appended sale
            # transactions resolve against the oldest lot even when the ledger
            # was created by an earlier release that omitted the option.
            updated_main = _ensure_fifo_booking_method(updated_main)
            if updated_main != current_main:
                main_path.write_text(updated_main, encoding="utf-8")
        created_files.append(main_path)

        # 2. accounts.bean
        accounts_path = dest / "accounts.bean"
        _verify_file_symlink(accounts_path)
        accounts_content = self.generate_accounts_bean(balances, holdings, transactions, include_pads=False)
        accounts_path.write_text(accounts_content, encoding="utf-8")
        created_files.append(accounts_path)

        # 3. balances.bean
        balances_path = dest / "balances.bean"
        _verify_file_symlink(balances_path)
        balances_content = self.generate_balances_bean(balances, holdings, transactions=transactions)
        balances_path.write_text(balances_content, encoding="utf-8")
        created_files.append(balances_path)

        # 4. holdings.bean
        holdings_path = dest / "holdings.bean"
        _verify_file_symlink(holdings_path)
        existing_h_text = holdings_path.read_text(encoding="utf-8") if (holdings_path.exists() and append) else None
        holdings_content = (
            self.generate_holdings_bean(
                holdings,
                balances=balances,
                transactions=transactions,
                existing_content=existing_h_text,
                opening_date=effective_opening_date,
            )
            if holdings
            else ""
        )
        if holdings_path.exists() and append:
            if holdings_content:
                body_start = holdings_content.find("\n\n")
                to_append = holdings_content[body_start + 2:] if body_start != -1 else holdings_content
                if to_append.strip():
                    with open(holdings_path, "a", encoding="utf-8") as f:
                        f.write(to_append)
        else:
            holdings_path.write_text(holdings_content, encoding="utf-8")
        created_files.append(holdings_path)

        # 5. prices.bean
        prices_path = dest / "prices.bean"
        _verify_file_symlink(prices_path)
        prices_content = self.generate_prices_bean(holdings)
        prices_path.write_text(prices_content, encoding="utf-8")
        created_files.append(prices_path)

        # 6. transactions.bean
        tx_path = dest / "transactions.bean"
        _verify_file_symlink(tx_path)
        if transactions and transactions.transactions:
            if tx_path.exists() and append:
                existing_tx_text = tx_path.read_text(encoding="utf-8")
                # Identify already exported transaction IDs
                existing_ids = set(re.findall(r'empower_id:\s*"([^"]+)"', existing_tx_text))
                new_txs = [
                    t for t in transactions.transactions
                    if str(t.get("user_transaction_id") or "") not in existing_ids
                ]
                if new_txs:
                    delta_container = DashboardTransactions(
                        start_date=transactions.start_date,
                        end_date=transactions.end_date,
                        total_transactions=len(new_txs),
                        money_in=transactions.money_in,
                        money_out=transactions.money_out,
                        net_cashflow=transactions.net_cashflow,
                        transactions=new_txs,
                    )
                    delta_text = self.generate_transactions_bean(delta_container, balances=balances)
                    # Strip the header from delta_text when appending
                    body_start = delta_text.find("\n\n")
                    to_append = delta_text[body_start + 2:] if body_start != -1 else delta_text
                    with open(tx_path, "a", encoding="utf-8") as f:
                        f.write(to_append)
            else:
                tx_content = self.generate_transactions_bean(transactions, balances=balances)
                tx_path.write_text(tx_content, encoding="utf-8")
        elif not tx_path.exists():
            tx_path.write_text("", encoding="utf-8")
        created_files.append(tx_path)

        return created_files

    def export_single_file(
        self,
        filepath: Union[str, Path],
        balances: Optional[DashboardBalances] = None,
        holdings: Optional[DashboardHoldings] = None,
        transactions: Optional[DashboardTransactions] = None,
        append: bool = True,
        opening_date: Optional[str] = None,
    ) -> Path:
        """Export directives into a single comprehensive .bean ledger file.

        When append=True and the file exists, preserves prior accounting entries and appends
        new balance assertions and non-duplicate transactions.
        """
        target = Path(filepath).expanduser().resolve()
        if target.is_symlink():
            raise ValueError(f"Refusing to write to symlinked target file: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)

        effective_opening_date = _determine_opening_date(transactions, balances, opening_date)

        if target.exists() and append:
            existing_content = target.read_text(encoding="utf-8")
            existing_ids = set(re.findall(r'empower_id:\s*"([^"]+)"', existing_content))

            # Ensure a FIFO booking method is declared so appended sale
            # transactions resolve against the oldest lot even when the file
            # was created by an earlier release that omitted the option.
            patched_content = _ensure_fifo_booking_method(existing_content)
            if patched_content != existing_content:
                target.write_text(patched_content, encoding="utf-8")

            delta_chunks: List[str] = [
                f"\n\n;; ------------------------------------------------------------------------------\n"
                f";; Empower Personal Dashboard Append Export\n"
                f";; ------------------------------------------------------------------------------\n\n"
            ]

            # Append balance assertions if available
            if balances or holdings:
                delta_chunks.append(self.generate_balances_bean(balances, holdings, transactions=transactions))
                delta_chunks.append("\n")

            # Append commodity holdings lots if available
            if holdings:
                delta_chunks.append(
                    self.generate_holdings_bean(
                        holdings,
                        balances=balances,
                        transactions=transactions,
                        existing_content=existing_content,
                        opening_date=effective_opening_date,
                    )
                )
                delta_chunks.append("\n")

            # Append price points
            if holdings:
                delta_chunks.append(self.generate_prices_bean(holdings))
                delta_chunks.append("\n")

            # Append non-duplicate transactions
            if transactions and transactions.transactions:
                new_txs = [
                    t for t in transactions.transactions
                    if str(t.get("user_transaction_id") or "") not in existing_ids
                ]
                if new_txs:
                    delta_container = DashboardTransactions(
                        start_date=transactions.start_date,
                        end_date=transactions.end_date,
                        total_transactions=len(new_txs),
                        money_in=transactions.money_in,
                        money_out=transactions.money_out,
                        net_cashflow=transactions.net_cashflow,
                        transactions=new_txs,
                    )
                    delta_tx_text = self.generate_transactions_bean(delta_container, balances=balances)
                    delta_chunks.append(delta_tx_text)

            with open(target, "a", encoding="utf-8") as f:
                f.write("".join(delta_chunks))
            return target

        # Clean new single-file ledger
        chunks = [
            ";; ==============================================================================\n"
            ";; Empower Personal Dashboard - Single Ledger Export\n"
            ";; ==============================================================================\n\n"
            'option "title" "Empower Personal Dashboard Ledger"\n'
            'option "operating_currency" "USD"\n'
            'option "booking_method" "FIFO"\n\n',
            'plugin "beancount.plugins.auto_accounts"\n\n',
            ';; Fava Web Dashboard Display Configuration\n',
            '1970-01-01 custom "fava-option" "invert-income-liabilities-equity" "true"\n\n',
            self.generate_accounts_bean(balances, holdings, transactions, include_pads=False),
            "\n",
            self.generate_holdings_bean(
                holdings,
                balances=balances,
                transactions=transactions,
                opening_date=effective_opening_date,
            ),
            "\n",
            self.generate_balances_bean(balances, holdings, transactions=transactions),
            "\n",
            self.generate_prices_bean(holdings),
            "\n",
        ]
        if transactions:
            chunks.append(self.generate_transactions_bean(transactions, balances=balances))

        target.write_text("".join(chunks), encoding="utf-8")
        return target
