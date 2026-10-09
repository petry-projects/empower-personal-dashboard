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

from empower_personal_dashboard.exceptions import LedgerAppendError
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

# Date every generated account ``open`` directive uses. Inferred opening-lot
# dates are clamped to never precede it, so a reconstructed lot cannot post
# against an account Beancount considers not yet open.
_ACCOUNT_OPEN_DATE = "2000-01-01"

# Comment marker persisting the earliest reconstructed window boundary in a
# holdings section, so an append can detect (and reject) a window broadened into
# the past, which would leave the already-exported opening lot stale.
_WINDOW_START_MARKER = "empower_window_start"

# Canonical Beancount account names reused across multiple generators.
ACCT_OPENING_BALANCES = "Equity:Opening-Balances"
ACCT_INCOME_DIVIDENDS = "Income:Dividends"
ACCT_INCOME_CAPITAL_GAINS = "Income:CapitalGains"
ACCT_EXPENSES_FEES = "Expenses:Fees"

# Default firm / account identity fallbacks used when upstream data is missing.
_FIRM_INSTITUTION = "Institution"
_FIRM_BROKERAGE = "Brokerage"
_ACCOUNT_DEFAULT_NAME = "Account"
_ACCOUNT_TYPE_BANK = "bank"
_ACCOUNT_TYPE_INVESTMENT = "investment"

# Account-type keywords that indicate a commodity-bearing investment account,
# whose `open` directive is left currency-unconstrained.
_INVESTMENT_ACCOUNT_WORDS = (
    "invest", "broker", "ira", "401k", "roth", "rollover",
    "stock", "portfolio", "529", "other",
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
    result: Dict[str, Any] = {"accounts": {}, "categories": {}, "regex_rules": [], "transaction_overrides": {}}
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

        if current_section in ("accounts", "categories", "transaction_overrides"):
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

    A security trade (buy/sell/reinvest/disposal) requires a ticker, matching
    the investment detection used when emitting postings. A dividend is
    recognised from its transaction/investment type independently of whether the
    upstream payload supplied a ticker — the canonical transaction schema permits
    a null symbol — so a symbol-less dividend still posts to the brokerage
    account rather than splitting into ``Assets:Institution``.
    """
    tt = clean_api_text(transaction.get("transaction_type") or "").strip().lower()
    it = clean_api_text(transaction.get("investment_type") or "").strip().lower()
    if "dividend" in tt or "dividend" in it:
        return True
    if not _clean_ticker(transaction.get("symbol")):
        return False
    return (
        tt in ("buy", "sell", "reinvest", "disposal")
        or it in ("buy", "sell", "reinvest", "disposal")
    )


def _resolve_account_from_tx(
    transaction: Dict[str, Any],
    acct_lookup: Dict[str, Dict[str, Any]],
    default_firm: str = _FIRM_INSTITUTION,
    default_type: str = _ACCOUNT_TYPE_BANK,
    default_name: str = _ACCOUNT_DEFAULT_NAME,
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
        default_firm = _FIRM_BROKERAGE
        default_type = _ACCOUNT_TYPE_INVESTMENT
        default_name = _FIRM_BROKERAGE

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


def _resolve_account_from_holding(
    holding: Dict[str, Any],
    acct_lookup: Dict[str, Dict[str, Any]],
) -> Tuple[str, str, str, str]:
    """Resolve (firm, account_name, account_type, account_id) for a holding.

    Mirrors the account resolution used across accounts, balances, and holdings
    generation so a single position maps to one consistent brokerage account.
    """
    uaid = str(holding.get("user_account_id") or "")
    h_name = holding.get("account_name") or ""
    acct_info = acct_lookup.get(uaid) or acct_lookup.get(h_name)
    if acct_info:
        firm = acct_info.get("firm_name") or _FIRM_BROKERAGE
        acct_name = acct_info.get("account_name") or h_name or _FIRM_BROKERAGE
        acct_type = acct_info.get("account_type") or _ACCOUNT_TYPE_INVESTMENT
        acct_id = str(acct_info.get("account_id") or uaid)
    else:
        firm = holding.get("firm_name") or _FIRM_BROKERAGE
        acct_name = h_name or _FIRM_BROKERAGE
        acct_type = _ACCOUNT_TYPE_INVESTMENT
        acct_id = uaid
    return firm, acct_name, acct_type, acct_id


def _is_investment_account_type(acct_type: Any) -> bool:
    """True when an account type string denotes a commodity-bearing investment account."""
    type_lower = str(acct_type).lower()
    return any(word in type_lower for word in _INVESTMENT_ACCOUNT_WORDS)


def _balance_account_fields(acct: Dict[str, Any]) -> Tuple[str, str, str, str, str, float]:
    """Extract (firm, name, account_id, account_type, currency, balance) from a balances account."""
    return (
        acct.get("firm_name") or _FIRM_INSTITUTION,
        acct.get("account_name") or _ACCOUNT_DEFAULT_NAME,
        str(acct.get("account_id") or ""),
        acct.get("account_type") or _ACCOUNT_TYPE_BANK,
        acct.get("currency") or "USD",
        float(acct.get("balance", 0.0)),
    )


class BeancountMapper:
    """Handles mapping of Empower accounts, categories, and payee rules to Beancount accounts."""

    def __init__(self, mapping_path: Optional[Union[str, Path]] = None):
        self.accounts: Dict[str, str] = {}
        self.categories: Dict[str, str] = {}
        self.regex_rules: List[Dict[str, str]] = []
        self.transaction_overrides: Dict[str, str] = {}

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

        raw_tx_overrides = parsed.get("transaction_overrides")
        if isinstance(raw_tx_overrides, dict):
            self.transaction_overrides = {str(k): str(v) for k, v in raw_tx_overrides.items()}
        else:
            self.transaction_overrides = {}

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
        tx_id: Optional[Union[str, int]] = None,
    ) -> str:
        """Resolve the offsetting balancing leg for a transaction.

        Order of precedence:
        0. Explicit transaction ID override (tx_id).
        1. Regex payee rules (matching description and memo).
        2. Category name overrides from mapping.
        3. Category ID overrides from mapping.
        4. Transfer detection (non-income and non-spending, or transfer keywords) -> Equity:Transfers.
        5. Income transactions -> Income:Uncategorized.
        6. Spending transactions -> Expenses:Uncategorized.
        7. Default fallback -> Expenses:Uncategorized.
        """
        # 0. Check explicit transaction ID override
        if tx_id is not None and str(tx_id) in self.transaction_overrides:
            return self.transaction_overrides[str(tx_id)]

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
    transaction date and use min("2020-01-01", earliest_date - 1 day) so baseline lots
    strictly precede every trade, clamped to never fall before "2000-01-01" — the date
    every generated account ``open`` directive uses, so an inferred pre-2000 lot would
    otherwise post against an account Beancount considers not yet open. If no
    transactions exist but balances exist, fallback to "2020-01-01".
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
            earliest_tx = str(min(tx_dates))
            try:
                earliest_tx = (datetime.date.fromisoformat(earliest_tx[:10]) - datetime.timedelta(days=1)).isoformat()
            except ValueError:
                pass
            return max(_ACCOUNT_OPEN_DATE, min("2020-01-01", earliest_tx))
        return "2020-01-01"

    if balances and balances.accounts:
        return "2020-01-01"

    return None


def _clamp_before_earliest_trade(opening: str, transactions: Optional[DashboardTransactions]) -> str:
    """Ensure reconstructed opening lots are dated strictly before the earliest trade.

    A caller-supplied ``opening_date`` later than an included investment trade would
    place the baseline inventory after the reduction that needs it, so the ledger
    could not book the sale. Clamp to the day before the earliest trade instead.
    """
    if not transactions or not transactions.transactions:
        return opening
    trade_dates = [
        str(t.get("transaction_date") or t.get("date"))[:10]
        for t in transactions.transactions
        if t.get("symbol") and (t.get("transaction_date") or t.get("date"))
    ]
    if not trade_dates:
        return opening
    try:
        cap = (datetime.date.fromisoformat(min(trade_dates)) - datetime.timedelta(days=1)).isoformat()
    except ValueError:
        return opening
    # Never move the lot before the supported account-open date: postings preceding
    # their account's open directive are rejected by Beancount.
    return max(_ACCOUNT_OPEN_DATE, min(opening, cap))


def _ensure_fifo_booking_method(content: str) -> str:
    """Inject a FIFO ``booking_method`` option into an existing ledger header lacking one.

    Appending the newly supported sale transactions (whose reductions carry an
    empty ``{}`` cost spec) to a ledger generated by an earlier release — one
    written before FIFO was emitted — would otherwise fail: Beancount's strict
    default booking rejects an ambiguous reduction when a symbol has multiple
    open lots. The option is placed next to any existing ``option`` directives so
    the header stays readable; Beancount applies booking globally regardless of
    position. Content already declaring ``FIFO`` is returned unchanged; an
    existing *incompatible* method (e.g. ``STRICT``, under which the appended
    ``{}`` reductions remain ambiguous) is rewritten to ``FIFO`` rather than
    duplicated.
    """
    booking_re = re.compile(r'option\s+"booking_method"\s+"([^"]*)"')
    existing = booking_re.search(content)
    if existing:
        if existing.group(1) == "FIFO":
            return content
        return booking_re.sub('option "booking_method" "FIFO"', content, count=1)
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


def _prepare_ledger_path(filepath: Union[str, Path], *, is_dir: bool) -> Path:
    """Resolve and validate a user-supplied ledger path before any write.

    Guards against path-injection: rejects embedded NUL bytes in the raw input
    and refuses to follow a symlink at the resolved location (which could redirect
    the write outside the intended target). Returns the resolved absolute path.
    """
    raw = str(filepath)
    if "\x00" in raw:
        raise ValueError("Ledger path must not contain NUL bytes.")
    expanded = Path(filepath).expanduser()
    # Inspect the user-supplied location for a symlink *before* resolving it.
    # ``Path.resolve()`` follows symlinks, so a check on the resolved path can
    # never see one — the link has already been dereferenced to its target.
    # A symlinked target could redirect the write outside the intended path,
    # so refuse it up front, then return the resolved absolute path.
    if expanded.is_symlink():
        kind = "directory" if is_dir else "target file"
        raise ValueError(f"Refusing to write to symlinked {kind}: {expanded}")
    # A symlink anywhere in the parent chain can redirect the write outside the
    # intended location just as a symlinked leaf can; ``resolve()`` would follow
    # it silently. Reject every symlinked parent before resolving, matching the
    # CLI's ``_safe_output_path`` guard.
    for parent in expanded.parents:
        if parent.is_symlink():
            raise ValueError(f"Refusing to write to path inside symlinked directory: {expanded}")
    return expanded.resolve()


def _verify_not_symlink(p: Path) -> None:
    """Refuse to write through a symlinked ledger component file."""
    if p.is_symlink():
        raise ValueError(f"Refusing to write to symlinked ledger target: {p}")


def _strip_bean_header(text: str) -> str:
    """Return ``text`` with its leading comment header (up to the first blank line) removed."""
    body_start = text.find("\n\n")
    return text[body_start + 2:] if body_start != -1 else text


def _build_delta_transactions(
    transactions: DashboardTransactions,
    existing_ids: Set[str],
) -> Optional[DashboardTransactions]:
    """Build a container of transactions whose ids are not already present, or None if none are new."""
    new_txs = [
        t for t in transactions.transactions
        if str(t.get("user_transaction_id") or "") not in existing_ids
    ]
    if not new_txs:
        return None
    return DashboardTransactions(
        start_date=transactions.start_date,
        end_date=transactions.end_date,
        total_transactions=len(new_txs),
        money_in=transactions.money_in,
        money_out=transactions.money_out,
        net_cashflow=transactions.net_cashflow,
        transactions=new_txs,
    )


class BeancountGenerator:
    """Generates Beancount directives and ledger files from Empower data models."""

    def __init__(self, mapper: Optional[BeancountMapper] = None):
        self.mapper = mapper or BeancountMapper()

    def generate_main_bean(self, additional_includes: Optional[List[str]] = None) -> str:
        """Generate the root main.bean linking modular components."""
        lines = [
            ";; ==============================================================================\n",
            ";; Empower Personal Dashboard - Root Beancount Ledger\n",
            ";; ==============================================================================\n\n",
            'option "title" "Empower Personal Dashboard Ledger"\n',
            'option "operating_currency" "USD"\n',
            'option "booking_method" "FIFO"\n',
            'option "render_commas" "TRUE"\n\n',
            'plugin "beancount.plugins.auto_accounts"\n\n',
            ';; Fava Web Dashboard Display Configuration\n',
            '1970-01-01 custom "fava-option" "invert-income-liabilities-equity" "true"\n',
            '1970-01-01 custom "fava-option" "locale" "en_US"\n\n',
            'include "accounts.bean"\n',
            'include "balances.bean"\n',
            'include "holdings.bean"\n',
            'include "prices.bean"\n',
            'include "transactions.bean"\n',
        ]
        if additional_includes:
            for inc in sorted(set(additional_includes)):
                lines.append(f'include "{inc}"\n')
        return "".join(lines)

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
        commodity_accounts = self._collect_commodity_accounts(holdings, acct_lookup)

        # 1. Accounts from balances
        self._open_accounts_from_balances(
            balances, commodity_accounts, seen_accounts, lines, include_pads
        )
        # 2. Accounts from holdings
        self._open_accounts_from_holdings(
            holdings, acct_lookup, seen_accounts, lines, include_pads
        )
        # 3. Accounts from transactions
        self._open_accounts_from_transactions(
            transactions, acct_lookup, commodity_accounts, seen_accounts, lines
        )
        # 4 & 5. Custom/regex mapped accounts and transaction-derived categories
        self._open_custom_and_regex_accounts(seen_accounts, lines)
        self._open_category_accounts_from_transactions(transactions, seen_accounts, lines)

        return "".join(lines)

    def _collect_commodity_accounts(
        self,
        holdings: Optional[DashboardHoldings],
        acct_lookup: Dict[str, Dict[str, Any]],
    ) -> Set[str]:
        """Resolve the set of Beancount accounts that hold investment commodities."""
        commodity_accounts: Set[str] = set()
        if holdings and holdings.holdings:
            for h in holdings.holdings:
                firm, acct_name, acct_type, acct_id = _resolve_account_from_holding(h, acct_lookup)
                commodity_accounts.add(
                    self.mapper.resolve_account(firm, acct_name, acct_id, acct_type, is_asset=True)
                )
        return commodity_accounts

    def _open_accounts_from_balances(
        self,
        balances: Optional[DashboardBalances],
        commodity_accounts: Set[str],
        seen_accounts: Set[str],
        lines: List[str],
        include_pads: bool,
    ) -> None:
        if not (balances and balances.accounts):
            return
        for acct in balances.accounts:
            firm, name, acct_id, acct_type, curr, raw_bal = _balance_account_fields(acct)

            b_account = self.mapper.resolve_account(firm, name, acct_id, acct_type)
            if b_account in seen_accounts:
                continue
            seen_accounts.add(b_account)
            if b_account in commodity_accounts or _is_investment_account_type(acct_type):
                lines.append(f"2000-01-01 open {b_account}\n")
            else:
                lines.append(f"2000-01-01 open {b_account} {curr}\n")
            if abs(raw_bal) > 0.001 and include_pads:
                lines.append(f"2000-01-01 pad {b_account} {ACCT_OPENING_BALANCES}\n")

    def _open_accounts_from_holdings(
        self,
        holdings: Optional[DashboardHoldings],
        acct_lookup: Dict[str, Dict[str, Any]],
        seen_accounts: Set[str],
        lines: List[str],
        include_pads: bool,
    ) -> None:
        if not (holdings and holdings.holdings):
            return
        for h in holdings.holdings:
            firm, name, acct_type, acct_id = _resolve_account_from_holding(h, acct_lookup)
            b_account = self.mapper.resolve_account(firm, name, acct_id, acct_type, is_asset=True)
            if b_account in seen_accounts:
                continue
            seen_accounts.add(b_account)
            lines.append(f"2000-01-01 open {b_account}\n")
            if include_pads:
                lines.append(f"2000-01-01 pad {b_account} {ACCT_OPENING_BALANCES}\n")

    def _collect_commodity_tx_accounts(
        self,
        transactions: DashboardTransactions,
        acct_lookup: Dict[str, Dict[str, Any]],
    ) -> Set[str]:
        """Resolve transaction accounts that receive commodity (ticker) postings."""
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
        return commodity_tx_accounts

    def _open_accounts_from_transactions(
        self,
        transactions: Optional[DashboardTransactions],
        acct_lookup: Dict[str, Dict[str, Any]],
        commodity_accounts: Set[str],
        seen_accounts: Set[str],
        lines: List[str],
    ) -> None:
        if not (transactions and transactions.transactions):
            return
        commodity_tx_accounts = self._collect_commodity_tx_accounts(transactions, acct_lookup)
        for tx in transactions.transactions:
            firm, name, acct_type, resolved_id = _resolve_account_from_tx(tx, acct_lookup)
            b_account = self.mapper.resolve_account(firm, name, resolved_id, acct_type)
            if b_account in seen_accounts:
                continue
            seen_accounts.add(b_account)
            # Investment accounts may hold ticker commodities, so leave them
            # currency-unconstrained; cash-only accounts stay USD.
            if (
                b_account in commodity_accounts
                or b_account in commodity_tx_accounts
                or _is_investment_account_type(acct_type)
            ):
                lines.append(f"2000-01-01 open {b_account}\n")
            else:
                lines.append(f"2000-01-01 open {b_account} USD\n")

    def _open_custom_and_regex_accounts(
        self,
        seen_accounts: Set[str],
        lines: List[str],
    ) -> None:
        """Open custom mapped accounts, categories, and regex rule accounts."""
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

    def _open_category_accounts_from_transactions(
        self,
        transactions: Optional[DashboardTransactions],
        seen_accounts: Set[str],
        lines: List[str],
    ) -> None:
        """Open category / balancing accounts resolved from transactions."""
        if not (transactions and transactions.transactions):
            return
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
        commodity_accounts = self._collect_commodity_accounts(holdings, acct_lookup)
        # ``empower --transactions --beancount`` fetches balances but not holdings,
        # so holdings-derived commodity accounts are empty. Investment transactions
        # still create security lots in those accounts, whose reported balance is
        # portfolio value, not USD cash. Fold in the transaction-derived commodity
        # accounts so their value is asserted as units below rather than double-
        # counted as a cash assertion here.
        if transactions and transactions.transactions:
            commodity_accounts |= self._collect_commodity_tx_accounts(transactions, acct_lookup)

        self._emit_cash_liability_assertions(
            balances, transactions, acct_lookup, commodity_accounts, lines
        )
        self._emit_commodity_unit_assertions(holdings, acct_lookup, lines)

        return "".join(lines)

    def _emit_cash_liability_assertions(
        self,
        balances: Optional[DashboardBalances],
        transactions: Optional[DashboardTransactions],
        acct_lookup: Dict[str, Dict[str, Any]],
        commodity_accounts: Set[str],
        lines: List[str],
    ) -> None:
        """Emit cash and liability balance assertions (and corrective pads)."""
        if not (balances and balances.accounts):
            return
        tx_totals = (
            _calculate_account_transaction_totals(transactions, self.mapper, acct_lookup)
            if transactions
            else {}
        )
        as_of = balances.as_of_date
        lines.append(f";; Cash & Liability Balances (as of {as_of})\n")
        for acct in balances.accounts:
            firm, name, acct_id, acct_type, curr, raw_bal = _balance_account_fields(acct)
            is_asset = acct.get("is_asset", True)

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
                lines.append(f"2020-01-01 pad {b_account} {ACCT_OPENING_BALANCES}\n")
            lines.append(f"{as_of} balance {b_account} {bal_amt:.2f} {curr}\n")

    def _emit_commodity_unit_assertions(
        self,
        holdings: Optional[DashboardHoldings],
        acct_lookup: Dict[str, Dict[str, Any]],
        lines: List[str],
    ) -> None:
        """Emit aggregated per-(account, ticker) commodity unit balance assertions."""
        if not (holdings and holdings.holdings):
            return
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
            # A short position reports a negative quantity; assert it as-is so the
            # reconstructed inventory is checked end to end. Only a ticker-less or
            # zero-rounding row is skipped.
            if not ticker or round(qty, 6) == 0:
                continue
            firm, acct_name, acct_type, acct_id = _resolve_account_from_holding(h, acct_lookup)
            b_account = self.mapper.resolve_account(firm, acct_name, acct_id, account_type=acct_type, is_asset=True)
            holding_units[(b_account, ticker)] = holding_units.get((b_account, ticker), 0.0) + qty

        for (b_account, ticker), total_qty in sorted(holding_units.items()):
            # Skip aggregates that cancel to zero at six-decimal precision: the
            # opening-lot emitter uses the same rounding rule, so asserting a
            # commodity whose lot was not created would fail the ledger. Negative
            # (short) aggregates are asserted as negative unit balances.
            if round(total_qty, 6) == 0:
                continue
            lines.append(f"{assert_date} balance {b_account} {_format_quantity(total_qty)} {ticker}\n")

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

    @staticmethod
    def _guard_window_not_broadened(existing_content: Optional[str], lot_date: str) -> None:
        """Reject an append whose opening lot predates the persisted window start.

        The holdings section records its earliest reconstructed window boundary in
        a ``;; empower_window_start: <date>`` marker. When appending, a new opening
        lot dated before that boundary would broaden the window into the past, but
        the already-exported opening lot is never revised — so the historical
        inventory would be inconsistent. Such an append is rejected.

        The marker lives in the header, which ``_strip_bean_header`` removes from
        every appended body, so a ``holdings.bean`` first written by an older
        release (or by a prior append) never carries it. When it is absent we fall
        back to the earliest existing opening-lot date (directives posting to
        ``Equity:Opening-Balances``), so the guard still rejects a
        backward-broadening append instead of silently permitting it.
        """
        if not existing_content:
            return
        m = re.search(rf"{_WINDOW_START_MARKER}:\s*(\S+)", existing_content)
        if m:
            prior_start = m.group(1).strip()
        else:
            # No marker (legacy file, or a body stripped of its header): derive the
            # earliest existing *opening-lot* date instead. Only directives that
            # post to ``Equity:Opening-Balances`` are reconstructed opening lots, so
            # restricting to those avoids mistaking a later snapshot/price directive
            # for the window boundary.
            opening_dates = [
                block_match.group(1)
                for block in re.split(r"\n\s*\n", existing_content)
                if ACCT_OPENING_BALANCES in block
                for block_match in [re.match(r"\s*(\d{4}-\d{2}-\d{2})\b", block)]
                if block_match
            ]
            if not opening_dates:
                return
            # ISO ``YYYY-MM-DD`` dates compare chronologically under a lexical compare.
            prior_start = min(opening_dates)
        # ISO ``YYYY-MM-DD`` dates compare chronologically under a lexical compare.
        if lot_date and prior_start and lot_date < prior_start:
            raise LedgerAppendError(
                f"Refusing to append: opening lot date {lot_date!r} predates the "
                f"previously exported window start {prior_start!r}. Broadening the "
                f"window into the past would leave the existing opening lot stale; "
                f"regenerate the ledger instead of appending."
            )

    def generate_holdings_bean(
        self,
        holdings: Optional[DashboardHoldings] = None,
        balances: Optional[DashboardBalances] = None,
        existing_content: Optional[str] = None,
        opening_date: Optional[str] = None,
        *,
        transactions: Optional[DashboardTransactions] = None,
    ) -> str:
        """Generate investment positions with lot cost-basis and price tracking.

        When appending to an existing holdings section, the previously exported
        window-start boundary is honoured: an append whose opening lot predates it
        broadens the window into the past, which would leave the already-written
        opening lot stale. That is rejected with :class:`LedgerAppendError` rather
        than silently producing an inconsistent historical inventory.
        """
        # Must reconstruct opening lots for fully-sold positions even if the holdings
        # snapshot is empty, or sells will reduce an empty inventory and the ledger
        # will fail to load. The emit helpers therefore run regardless of holdings.
        lot_date = self._resolve_lot_date(holdings, opening_date, transactions, balances)

        self._guard_window_not_broadened(existing_content, lot_date)

        lines = [
            ";; ==============================================================================\n"
            ";; Empower Personal Dashboard - Portfolio Holdings & Lots\n"
            f";; {_WINDOW_START_MARKER}: {lot_date}\n"
            ";; ==============================================================================\n\n"
        ]

        acct_lookup = _build_account_lookup(balances)

        # Existing holding tags to skip on append
        existing_keys: Set[str] = set()
        if existing_content:
            existing_keys = set(re.findall(r'empower_holding:\s*"([^"]+)"', existing_content))

        net_buys, net_buy_meta = self._compute_net_buys(transactions, acct_lookup)
        aggregated_holdings = self._aggregate_snapshot_holdings(holdings, acct_lookup)

        snapshot_date = holdings.as_of_date if holdings else None
        self._emit_snapshot_lots(
            aggregated_holdings, net_buys, net_buy_meta, transactions, existing_keys,
            lot_date, snapshot_date, lines,
        )
        self._emit_reconstructed_opening_lots(
            net_buys, net_buy_meta, aggregated_holdings, existing_keys, lot_date, transactions, lines
        )

        return "".join(lines)

    def _resolve_lot_date(
        self,
        holdings: Optional[DashboardHoldings],
        opening_date: Optional[str],
        transactions: Optional[DashboardTransactions],
        balances: Optional[DashboardBalances],
    ) -> str:
        """Determine the dated day used for reconstructed opening lots."""
        effective_opening = opening_date or _determine_opening_date(transactions, balances, None)
        if effective_opening:
            return _clamp_before_earliest_trade(effective_opening, transactions)
        # Date the holdings snapshot a day before the balance assertion date so
        # Beancount's beginning-of-day balance assertion on as_of passes cleanly.
        as_of = holdings.as_of_date if holdings else None
        if as_of:
            try:
                d = datetime.date.fromisoformat(as_of)
                return (d - datetime.timedelta(days=1)).isoformat()
            except Exception:
                return as_of
        return "2020-01-01"

    def _compute_net_buys(
        self,
        transactions: Optional[DashboardTransactions],
        acct_lookup: Dict[str, Dict[str, Any]],
    ) -> Tuple[Dict[Tuple[str, str], float], Dict[Tuple[str, str], Dict[str, Any]]]:
        """Net buy quantity and representative pricing per (account, ticker).

        Returns ``(net_buys, net_buy_meta)``. ``net_buy_meta`` carries the firm
        plus a representative price used to reconstruct synthetic opening lots
        for securities fully sold within the transaction window.
        """
        net_buys: Dict[Tuple[str, str], float] = {}
        net_buy_meta: Dict[Tuple[str, str], Dict[str, Any]] = {}
        if not (transactions and transactions.transactions):
            return net_buys, net_buy_meta

        for tx in transactions.transactions:
            self._accumulate_net_buy(tx, acct_lookup, net_buys, net_buy_meta)

        # Second, chronological pass: record the deepest intermediate inventory
        # deficit per key so a reconstructed opening lot covers a sell-then-buy
        # sequence whose final net is non-negative (see _emit_reconstructed_opening_lots).
        self._accumulate_running_deficit(transactions, acct_lookup, net_buy_meta)

        return net_buys, net_buy_meta

    def _classify_trade(
        self,
        tx: Dict[str, Any],
        acct_lookup: Dict[str, Dict[str, Any]],
    ) -> Optional[Tuple[Tuple[str, str], float, bool, bool, str, float]]:
        """Resolve a trade to ``(key, qty, is_buy, is_sell, firm, price)`` or ``None``.

        ``None`` for non-trades (no ticker, zero quantity, or neither buy nor sell).
        """
        t_ticker = _clean_ticker(tx.get("symbol"))
        tx_raw_qty = tx.get("quantity")
        tx_qty = abs(float(tx_raw_qty)) if tx_raw_qty is not None else 0.0
        if not t_ticker or tx_qty <= 0:
            return None

        tx_type = clean_api_text(tx.get("transaction_type") or "").strip().lower()
        inv_type = clean_api_text(tx.get("investment_type") or "").strip().lower()
        is_tx_buy = tx_type in ("buy", "reinvest") or inv_type in ("buy", "reinvest")
        is_tx_sell = tx_type in ("sell", "disposal") or inv_type in ("sell", "disposal")
        if not (is_tx_buy or is_tx_sell):
            return None

        tx_firm, tx_acct_name, tx_acct_type, tx_aid = _resolve_account_from_tx(tx, acct_lookup)
        tx_b_account = self.mapper.resolve_account(
            tx_firm, tx_acct_name, tx_aid, account_type=tx_acct_type
        )
        tx_raw_price = tx.get("price")
        tx_price = abs(float(tx_raw_price)) if tx_raw_price is not None else 0.0
        # A valid trade may report quantity and amount but omit price. Derive the
        # per-unit price from amount / quantity — mirroring _emit_investment_sell /
        # _emit_investment_buy — so the reconstructed opening lot is costed and a
        # later ``{}`` sale reduction can book against it instead of hitting a bare
        # (uncosted) opening lot.
        if tx_price <= 0:
            tx_raw_amount = tx.get("amount")
            tx_amount = abs(float(tx_raw_amount)) if tx_raw_amount is not None else 0.0
            if tx_amount > 0 and tx_qty > 0:
                tx_price = tx_amount / tx_qty
        return (tx_b_account, t_ticker), tx_qty, is_tx_buy, is_tx_sell, tx_firm, tx_price

    def _accumulate_net_buy(
        self,
        tx: Dict[str, Any],
        acct_lookup: Dict[str, Dict[str, Any]],
        net_buys: Dict[Tuple[str, str], float],
        net_buy_meta: Dict[Tuple[str, str], Dict[str, Any]],
    ) -> None:
        """Fold a single trade into the net-buys and opening-lot metadata maps."""
        classified = self._classify_trade(tx, acct_lookup)
        if classified is None:
            return
        key, tx_qty, is_tx_buy, is_tx_sell, tx_firm, tx_price = classified
        net_buys[key] = net_buys.get(key, 0.0) + (tx_qty if is_tx_buy else -tx_qty)

        meta = net_buy_meta.setdefault(
            key, {"firm": tx_firm, "buy_price": 0.0, "any_price": 0.0}
        )
        if is_tx_sell:
            # Mark that the baseline opening lot for this key must carry a cost
            # basis so an empty ``{}`` reduction can book against it.
            meta["has_sell"] = True
        self._update_net_buy_meta(meta, tx_price, is_tx_buy)

    def _accumulate_running_deficit(
        self,
        transactions: DashboardTransactions,
        acct_lookup: Dict[str, Dict[str, Any]],
        net_buy_meta: Dict[Tuple[str, str], Dict[str, Any]],
    ) -> None:
        """Record per-key ``max_deficit`` = the most-negative running inventory.

        Processing trades in chronological order yields the minimum running
        cumulative quantity; its negation is the opening quantity needed so the
        inventory never goes negative, even when the final net is non-negative.
        """
        def _tx_date(tx: Dict[str, Any]) -> str:
            return str(tx.get("transaction_date") or tx.get("date") or "")

        cumulative: Dict[Tuple[str, str], float] = {}
        for tx in sorted(transactions.transactions, key=_tx_date):
            classified = self._classify_trade(tx, acct_lookup)
            if classified is None:
                continue
            key, tx_qty, is_tx_buy, _is_sell, tx_firm, _price = classified
            cumulative[key] = cumulative.get(key, 0.0) + (tx_qty if is_tx_buy else -tx_qty)
            meta = net_buy_meta.setdefault(
                key, {"firm": tx_firm, "buy_price": 0.0, "any_price": 0.0}
            )
            deficit = -cumulative[key]
            if deficit > meta.get("max_deficit", 0.0):
                meta["max_deficit"] = deficit

    @staticmethod
    def _update_net_buy_meta(meta: Dict[str, Any], tx_price: float, is_tx_buy: bool) -> None:
        """Record the first positive price seen (and first buy price) for opening-lot reconstruction."""
        if tx_price <= 0:
            return
        if meta.get("any_price", 0.0) <= 0:
            meta["any_price"] = tx_price
        if is_tx_buy and meta.get("buy_price", 0.0) <= 0:
            meta["buy_price"] = tx_price

    def _aggregate_snapshot_holdings(
        self,
        holdings: Optional[DashboardHoldings],
        acct_lookup: Dict[str, Dict[str, Any]],
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Aggregate the holdings snapshot by (account, ticker), summing quantities and cost basis."""
        aggregated: Dict[Tuple[str, str], Dict[str, Any]] = {}
        if not (holdings and holdings.holdings):
            return aggregated

        for h in holdings.holdings:
            ticker = _clean_ticker(h.get("ticker"))
            qty = float(h.get("quantity") or 0.0)
            # Preserve negative (short) quantities: only a ticker-less row or one
            # whose quantity rounds to zero at the emitted precision is dropped.
            if not ticker or round(qty, 6) == 0:
                continue
            price = float(h.get("price") or 0.0)
            cost_basis = h.get("cost_basis")
            firm, acct_name, acct_type, acct_id = _resolve_account_from_holding(h, acct_lookup)
            b_account = self.mapper.resolve_account(firm, acct_name, acct_id, account_type=acct_type)
            key = (b_account, ticker)
            if key not in aggregated:
                aggregated[key] = {
                    "firm": firm,
                    "b_account": b_account,
                    "ticker": ticker,
                    "quantity": qty,
                    "price": price,
                    "cost_basis": float(cost_basis) if cost_basis is not None else None,
                }
            else:
                self._merge_holding(aggregated[key], qty, price, cost_basis)
        return aggregated

    @staticmethod
    def _merge_holding(agg: Dict[str, Any], qty: float, price: float, cost_basis: Any) -> None:
        """Fold an additional holding row of the same (account, ticker) into an aggregate."""
        agg["quantity"] = agg.get("quantity", 0.0) + qty
        if cost_basis is not None:
            curr_cb = agg.get("cost_basis") or 0.0
            agg["cost_basis"] = curr_cb + float(cost_basis)
        if price > 0:
            agg["price"] = price

    def _emit_snapshot_lots(
        self,
        aggregated_holdings: Dict[Tuple[str, str], Dict[str, Any]],
        net_buys: Dict[Tuple[str, str], float],
        net_buy_meta: Dict[Tuple[str, str], Dict[str, Any]],
        transactions: Optional[DashboardTransactions],
        existing_keys: Set[str],
        lot_date: str,
        snapshot_date: Optional[str],
        lines: List[str],
    ) -> None:
        """Emit the baseline opening lot for each snapshot position acquired before the window."""
        has_tx = transactions is not None and bool(transactions.transactions)
        for pos in sorted(
            aggregated_holdings.values(),
            key=lambda p: (p.get("b_account", ""), p.get("ticker", "")),
        ):
            b_account = pos.get("b_account", "")
            ticker = pos.get("ticker", "")
            snapshot_qty = pos.get("quantity", 0.0)
            key = (b_account, ticker)
            # Baseline Opening Qty = Current Snapshot Qty - Net Buys within window.
            naive_baseline = snapshot_qty - net_buys.get(key, 0.0) if has_tx else snapshot_qty
            baseline_qty = naive_baseline
            # A still-held *long* position can dip below its naive opening quantity
            # mid-window (e.g. a sell-then-buy whose net is zero): floor the opening
            # lot at the deepest intermediate deficit, mirroring the reconstructed-lot
            # path, so the earlier sale has inventory to book against instead of
            # failing the ledger before the later purchase. The floor is a long-only
            # construct (a negative/short baseline must stay negative), so apply it
            # only when the naive baseline is non-negative.
            floor_surplus = 0.0
            if has_tx and naive_baseline >= 0:
                max_deficit = net_buy_meta.get(key, {}).get("max_deficit", 0.0)
                baseline_qty = max(naive_baseline, max_deficit)
                # Flooring above ``snapshot - net_buys`` over-provisions the opening
                # lot, so the reconstructed final quantity would exceed the asserted
                # snapshot (unknown same-day sell/buy ordering). Record the surplus
                # so an explicit shortfall reconciliation below brings the final
                # quantity back to the snapshot.
                floor_surplus = baseline_qty - naive_baseline
            # Skip only positions whose baseline rounds to zero at the emitted
            # six-decimal precision — acquired entirely within the window — so a
            # legitimate micro-position (long or short) that still earns a balance
            # assertion (_emit_commodity_unit_assertions) is not dropped from the
            # opening lot.
            if round(baseline_qty, 6) == 0:
                continue
            holding_tag = f"{b_account}:{ticker}"
            if holding_tag in existing_keys:
                continue
            # A sale against this ticker in the window reduces the lot with an
            # empty ``{}`` cost spec, which Beancount matches only against costed
            # lots; emit the baseline lot with a cost basis so the sale can book.
            force_cost = bool(net_buy_meta.get(key, {}).get("has_sell"))
            self._append_snapshot_lot(
                lines, lot_date, pos.get("firm", _FIRM_BROKERAGE), ticker, holding_tag,
                b_account, baseline_qty, snapshot_qty, pos.get("price", 0.0), pos.get("cost_basis"),
                force_cost,
            )
            if round(floor_surplus, 6) > 0 and snapshot_date:
                self._append_shortfall_reconciliation(
                    lines, snapshot_date, pos.get("firm", _FIRM_BROKERAGE), ticker,
                    holding_tag, b_account, floor_surplus,
                )

    def _append_shortfall_reconciliation(
        self,
        lines: List[str],
        snapshot_date: str,
        firm: str,
        ticker: str,
        holding_tag: str,
        b_account: str,
        surplus: float,
    ) -> None:
        """Dispose a deficit-floor surplus on the snapshot date to match the assertion.

        When the running-deficit floor lifts the opening lot above
        ``snapshot - net_buys``, the reconstructed inventory ends the window with
        ``surplus`` extra units that the snapshot assertion does not expect. The
        exact timing is unknowable (same-day sell/buy ordering), so the surplus is
        reconciled explicitly against ``Equity:Opening-Balances`` on the snapshot
        date — before the next-day unit assertion — rather than silently breaking
        the ledger. The ``{}`` reduction books FIFO against the floored opening lot.
        """
        payee_esc = _escape_beancount_string(f"{firm} Snapshot Shortfall Reconciliation")
        narration_esc = _escape_beancount_string(f"{ticker} Deficit Floor Adjustment")
        lines.append(f'{snapshot_date} * "{payee_esc}" "{narration_esc}"\n')
        lines.append(f'  empower_holding: "{holding_tag}"\n')
        lines.append(
            f"  {b_account:<36} -{_format_quantity(surplus)} {ticker} {{}}\n"
        )
        lines.append(f"  {ACCT_OPENING_BALANCES:<36}\n\n")

    def _append_snapshot_lot(
        self,
        lines: List[str],
        lot_date: str,
        firm: str,
        ticker: str,
        holding_tag: str,
        b_account: str,
        qty: float,
        snapshot_qty: float,
        price: float,
        cost_basis: Optional[float],
        force_cost: bool = False,
    ) -> None:
        """Append a single snapshot opening-lot directive, scaling cost basis when present."""
        payee_esc = _escape_beancount_string(f"{firm} Portfolio Snapshot")
        narration_esc = _escape_beancount_string(f"{ticker} Position")
        lines.append(f'{lot_date} * "{payee_esc}" "{narration_esc}"\n')
        lines.append(f'  empower_holding: "{holding_tag}"\n')
        if cost_basis is not None and float(cost_basis) > 0 and snapshot_qty > 0:
            scaled_cost_basis = float(cost_basis) * qty / snapshot_qty
            lines.append(
                f"  {b_account:<36} {_format_quantity(qty):>10} {ticker} "
                f"{{{{{_format_cost(scaled_cost_basis)} USD}}}}\n"
            )
        elif force_cost and price > 0:
            # No reported cost basis, but a sale will reduce this lot: estimate the
            # lot cost from the snapshot price so an empty ``{}`` reduction matches.
            lines.append(
                f"  {b_account:<36} {_format_quantity(qty):>10} {ticker} "
                f"{{{_format_cost(price)} USD}}\n"
            )
        else:
            lines.append(
                f"  {b_account:<36} {_format_quantity(qty):>10} {ticker} @ {_format_price(price)} USD\n"
            )
        lines.append(f"  {ACCT_OPENING_BALANCES:<36}\n\n")

    def _emit_reconstructed_opening_lots(
        self,
        net_buys: Dict[Tuple[str, str], float],
        net_buy_meta: Dict[Tuple[str, str], Dict[str, Any]],
        aggregated_holdings: Dict[Tuple[str, str], Dict[str, Any]],
        existing_keys: Set[str],
        lot_date: str,
        transactions: Optional[DashboardTransactions],
        lines: List[str],
    ) -> None:
        """Reconstruct opening lots for securities traded before the window and absent from the snapshot.

        Such a position is absent from the current holdings snapshot, yet the
        transaction window reduces its inventory below zero at some point; without
        a synthetic opening lot the reconstructed sales would reduce an empty
        inventory and the historical ledger would fail to load. The opening
        quantity must cover the *maximum running deficit*, not merely the final
        net: a sequence that sells 10 then later buys 10 nets to zero yet the
        earlier sale still needs 10 opening units to book.
        """
        if not (transactions is not None and transactions.transactions):
            return
        for key in sorted(net_buy_meta):
            if key in aggregated_holdings:
                continue
            nb = net_buys.get(key, 0.0)
            meta = net_buy_meta.get(key, {})
            # Opening units needed = the larger of the net sold quantity and the
            # deepest intermediate shortfall over the chronological trade sequence.
            baseline_qty = max(-nb, meta.get("max_deficit", 0.0))
            # Use the same six-decimal rounding test as the snapshot-lot path
            # (_emit_snapshot_lots / _emit_commodity_unit_assertions): a reconstructed
            # micro-lot (e.g. a 0.000001-share sale) must still be emitted so the
            # equally sized sale has inventory to reduce and the ledger books.
            if round(baseline_qty, 6) <= 0:
                continue
            b_account, ticker = key
            holding_tag = f"{b_account}:{ticker}"
            if holding_tag in existing_keys:
                continue
            firm = meta.get("firm") or _FIRM_BROKERAGE
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
            lines.append(f"  {ACCT_OPENING_BALANCES:<36}\n\n")

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
            self._emit_transaction(tx, acct_lookup, lines)

        return "".join(lines)

    def _emit_transaction(
        self,
        tx: Dict[str, Any],
        acct_lookup: Dict[str, Dict[str, Any]],
        lines: List[str],
    ) -> None:
        """Emit one transaction, dispatching to investment or standard handling."""
        ctx = self._build_tx_context(tx, acct_lookup)
        if ctx["is_buy"]:
            self._emit_investment_buy(ctx, lines)
        elif ctx["is_sell"]:
            self._emit_investment_sell(ctx, lines)
        elif ctx["is_dividend"]:
            self._emit_investment_dividend(ctx, lines)
        else:
            self._emit_standard_tx(ctx, lines)

    def _resolve_is_asset(
        self,
        tx: Dict[str, Any],
        acct_type: str,
        acct_lookup: Dict[str, Dict[str, Any]],
    ) -> bool:
        """Determine whether a transaction's primary account is an asset (vs liability)."""
        acct_id = str(tx.get("account_id") or "")
        uaid = str(tx.get("user_account_id") or "")
        t_name = tx.get("account_name") or ""
        acct_info = acct_lookup.get(acct_id) or acct_lookup.get(uaid) or acct_lookup.get(t_name)
        if acct_info:
            return acct_info.get("is_asset", True)
        type_lower = acct_type.lower()
        return not (
            "credit" in type_lower
            or "loan" in type_lower
            or "mortgage" in type_lower
            or "liabilit" in type_lower
        )

    @staticmethod
    def _classify_investment_tx(
        tx_type_clean: str,
        inv_type_clean: str,
        cat_clean: str,
        ticker: str,
        qty: float,
    ) -> Tuple[bool, bool, bool]:
        """Classify a transaction as (is_buy, is_sell, is_dividend)."""
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
            # A bare "dividend" category needs investment evidence (a ticker plus a
            # transaction/investment type) before it is treated as an investment
            # dividend; otherwise ordinary banking transactions labelled "dividend"
            # would skip category resolution.
            or ("dividend" in cat_clean and bool(ticker) and bool(tx_type_clean or inv_type_clean))
        )
        return is_buy, is_sell, is_dividend

    @staticmethod
    def _tx_tag_str(tx_id: str) -> str:
        """Build the ``^empower-tx-<id>`` link tag for a transaction, or '' when unavailable."""
        link_id = tx_id[3:] if tx_id.startswith("tx-") else tx_id
        link_id_clean = re.sub(r"[^A-Za-z0-9\-]", "", link_id)
        return f" ^empower-tx-{link_id_clean}" if link_id_clean else ""

    @staticmethod
    def _investment_payee_narration(tx: Dict[str, Any], firm: str) -> Tuple[str, str]:
        """Resolve the payee and narration strings for an investment transaction."""
        if firm and firm != _FIRM_INSTITUTION:
            inv_payee = _escape_beancount_string(firm)
        else:
            inv_payee = _escape_beancount_string(tx.get("description") or _FIRM_BROKERAGE)
        inv_narration = _escape_beancount_string(tx.get("description") or "")
        return inv_payee, inv_narration

    def _build_tx_context(
        self,
        tx: Dict[str, Any],
        acct_lookup: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Compute the shared per-transaction fields used by every emit handler."""
        tx_id = str(tx.get("user_transaction_id") or "")
        acct_id = str(tx.get("account_id") or "")

        # Use _resolve_account_from_tx so investment transactions consistently fall
        # back to the brokerage identity (matching holdings reconciliation and
        # assertions) so their postings reconcile in one account.
        firm, acct_name, acct_type, resolved_id = _resolve_account_from_tx(tx, acct_lookup)
        is_asset = self._resolve_is_asset(tx, acct_type, acct_lookup)

        date = tx.get("transaction_date") or "2000-01-01"
        cat_name = tx.get("category") or tx.get("category_name")
        amount = abs(float(tx.get("amount") or 0.0))
        primary_account = self.mapper.resolve_account(firm, acct_name, resolved_id, acct_type)

        tx_type_clean = clean_api_text(tx.get("transaction_type") or "").strip().lower()
        inv_type_clean = clean_api_text(tx.get("investment_type") or "").strip().lower()
        cat_clean = clean_api_text(cat_name or "").lower()
        ticker = _clean_ticker(tx.get("symbol"))
        raw_qty = tx.get("quantity")
        raw_price = tx.get("price")
        qty = abs(float(raw_qty)) if raw_qty is not None else 0.0
        price = abs(float(raw_price)) if raw_price is not None else 0.0

        tag_str = self._tx_tag_str(tx_id)

        is_buy, is_sell, is_dividend = self._classify_investment_tx(
            tx_type_clean, inv_type_clean, cat_clean, ticker, qty
        )

        # Payee/narration for investment transactions (unused by standard handling).
        inv_payee, inv_narration = self._investment_payee_narration(tx, firm)

        return {
            "tx": tx,
            "tx_id": tx_id,
            "acct_id": acct_id,
            "firm": firm,
            "primary_account": primary_account,
            "is_asset": is_asset,
            "date": date,
            "cat_name": cat_name,
            "amount": amount,
            "is_credit": bool(tx.get("is_credit")),
            "is_cash_in": bool(tx.get("is_cash_in")),
            "is_income": bool(tx.get("is_income")),
            "is_spending": bool(tx.get("is_spending", False)),
            "tx_type_clean": tx_type_clean,
            "inv_type_clean": inv_type_clean,
            "ticker": ticker,
            "qty": qty,
            "price": price,
            "tag_str": tag_str,
            "is_buy": is_buy,
            "is_sell": is_sell,
            "is_dividend": is_dividend,
            "inv_payee": inv_payee,
            "inv_narration": inv_narration,
        }

    @staticmethod
    def _emit_tx_header(
        lines: List[str],
        date: str,
        payee: str,
        narration: str,
        tag_str: str,
        tx_id: str,
        acct_id: str,
    ) -> None:
        """Append the transaction header line plus optional id metadata lines."""
        lines.append(f'{date} * "{payee}" "{narration}"{tag_str}\n')
        if tx_id:
            lines.append(f'  empower_id: "{_escape_beancount_string(tx_id)}"\n')
        if acct_id:
            lines.append(f'  empower_account_id: "{_escape_beancount_string(acct_id)}"\n')

    def _emit_investment_buy(self, ctx: Dict[str, Any], lines: List[str]) -> None:
        primary_account = ctx["primary_account"]
        qty = ctx["qty"]
        price = ctx["price"]
        amount = ctx["amount"]
        ticker = ctx["ticker"]

        reported_amount = amount
        if price == 0 and reported_amount > 0 and qty > 0:
            price = reported_amount / qty
        # A valid Buy/Reinvest may omit ``amount`` while supplying quantity and
        # price (fetch_transactions normalizes the missing amount to zero). Derive
        # the reported cash outflow from qty × price — mirroring the sell path —
        # so the lot and funding leg are nonzero instead of inventing free shares.
        if reported_amount == 0 and price > 0 and qty > 0:
            reported_amount = round(qty * price, 2)
        # Book the lot at an explicit *total* cost so it balances exactly against
        # the cash leg. Per-unit cost syntax ({price USD}) drifts for derived
        # prices and large fractional quantities because Beancount recomputes
        # qty × rounded-price, which need not equal the reported amount.
        # Derive the lot cost from qty × price, but never let it exceed the
        # reported cash outflow: when the computed cost is unusable (≤ 0) or
        # larger than reported (which would otherwise leave a dropped negative
        # fee and an unbalanced entry), fall back to the full reported amount and
        # record no fee. Any positive remainder becomes an Expenses:Fees posting.
        calculated_cost = round(qty * price, 2) if price > 0 and qty > 0 else 0.0
        if calculated_cost <= 0 or calculated_cost > reported_amount:
            lot_cost = reported_amount
            fee = 0.0
        else:
            lot_cost = calculated_cost
            fee = round(reported_amount - calculated_cost, 2)
        # Reinvested dividends are funded by dividend income, not by brokerage cash;
        # routing them through the cash-outflow leg would wrongly drain USD and omit
        # the dividend income.
        is_reinvest = ctx["tx_type_clean"] == "reinvest" or ctx["inv_type_clean"] == "reinvest"
        funding_account = (
            (self.mapper.categories.get("Dividends") or ACCT_INCOME_DIVIDENDS)
            if is_reinvest
            else primary_account
        )
        self._emit_tx_header(
            lines, ctx["date"], ctx["inv_payee"], ctx["inv_narration"],
            ctx["tag_str"], ctx["tx_id"], ctx["acct_id"],
        )
        lines.append(
            f"  {primary_account:<36} {_format_quantity(qty):>10} {ticker} "
            f"{{{{{_format_cost(lot_cost)} USD}}}}\n"
        )
        lines.append(f"  {funding_account:<36} {-reported_amount:>8.2f} USD\n")
        if fee > 0:
            fees_acct = self.mapper.categories.get("Fees") or ACCT_EXPENSES_FEES
            lines.append(f"  {fees_acct:<36} {fee:>8.2f} USD\n")
        lines.append("\n")

    def _emit_investment_sell(self, ctx: Dict[str, Any], lines: List[str]) -> None:
        primary_account = ctx["primary_account"]
        qty = ctx["qty"]
        price = ctx["price"]
        amount = ctx["amount"]
        ticker = ctx["ticker"]

        if price == 0 and amount > 0 and qty > 0:
            price = amount / qty
        if amount == 0 and price > 0 and qty > 0:
            amount = round(qty * price, 2)
        cap_gains_acct = self.mapper.categories.get("Capital Gains") or ACCT_INCOME_CAPITAL_GAINS
        self._emit_tx_header(
            lines, ctx["date"], ctx["inv_payee"], ctx["inv_narration"],
            ctx["tag_str"], ctx["tx_id"], ctx["acct_id"],
        )
        lines.append(
            f"  {primary_account:<36} -{_format_quantity(qty)} {ticker} {{}} "
            f"@ {_format_price(price)} USD\n"
        )
        lines.append(f"  {primary_account:<36} {amount:>8.2f} USD\n")
        lines.append(f"  {cap_gains_acct}\n\n")

    def _emit_investment_dividend(self, ctx: Dict[str, Any], lines: List[str]) -> None:
        dividend_acct = self.mapper.categories.get("Dividends") or ACCT_INCOME_DIVIDENDS
        self._emit_tx_header(
            lines, ctx["date"], ctx["inv_payee"], ctx["inv_narration"],
            ctx["tag_str"], ctx["tx_id"], ctx["acct_id"],
        )
        # The shared context stores the magnitude (abs) of the amount, so the
        # posting direction must come from the transaction flags. A debit or
        # correction — a negative raw amount, or an explicit cash-out that is not
        # a credit — reverses a normal dividend receipt: cash leaves the account
        # and dividend income is reduced, instead of overstating both.
        raw_amount = float(ctx["tx"].get("amount") or 0.0)
        is_debit = raw_amount < 0 or (
            bool(ctx["tx"].get("is_cash_out")) and not ctx["is_credit"]
        )
        sign = -1.0 if is_debit else 1.0
        cash = sign * ctx["amount"]
        lines.append(f"  {ctx['primary_account']:<36} {cash:>8.2f} USD\n")
        lines.append(f"  {dividend_acct:<36} {-cash:>8.2f} USD\n\n")

    def _emit_standard_tx(self, ctx: Dict[str, Any], lines: List[str]) -> None:
        tx = ctx["tx"]
        cat_name = ctx["cat_name"]
        amount = ctx["amount"]
        primary_account = ctx["primary_account"]

        payee = _escape_beancount_string(tx.get("description") or "Unknown Payee")
        narration = _escape_beancount_string(cat_name or tx.get("original_description") or "")

        category_account = self.mapper.resolve_category_or_payee(
            category=cat_name,
            description=tx.get("description"),
            is_spending=ctx["is_spending"],
            is_income=ctx["is_income"],
            category_id=tx.get("category_id"),
            transaction_type=tx.get("transaction_type"),
            memo=tx.get("original_description"),
            tx_id=ctx.get("tx_id"),
        )

        # Determine double-entry posting signs
        is_credit = ctx["is_credit"]
        is_cash_in = ctx["is_cash_in"]
        is_income = ctx["is_income"]
        is_liability = primary_account.startswith("Liabilities") or (not ctx["is_asset"])

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

        self._emit_tx_header(
            lines, ctx["date"], payee, narration,
            ctx["tag_str"], ctx["tx_id"], ctx["acct_id"],
        )
        lines.append(f"  {primary_account:<36} {acct_amount:>8.2f} USD\n")
        lines.append(f"  {category_account:<36} {bal_amount:>8.2f} USD\n\n")

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
        dest = _prepare_ledger_path(destination_dir, is_dir=True)
        dest.mkdir(parents=True, exist_ok=True)
        effective_opening_date = _determine_opening_date(transactions, balances, opening_date)

        # Pre-flight the backward-broadening guard *before* any file is written.
        # The guard otherwise fires inside ``_write_modular_holdings``, i.e. after
        # main.bean / accounts.bean / balances.bean have already been overwritten,
        # which would leave the existing ledger partially updated on a rejected
        # append. Validating up front makes the export all-or-nothing.
        self._preflight_modular_append_guard(
            dest, append, holdings, balances, transactions, effective_opening_date,
        )

        created_files: List[Path] = [
            self._write_modular_main(dest, append),
            self._write_modular_section(
                dest, "accounts.bean",
                self.generate_accounts_bean(balances, holdings, transactions, include_pads=False),
            ),
            self._write_modular_section(
                dest, "balances.bean",
                self.generate_balances_bean(balances, holdings, transactions=transactions),
            ),
            self._write_modular_holdings(
                dest, append, holdings, balances, transactions, effective_opening_date,
            ),
            self._write_modular_section(
                dest, "prices.bean", self.generate_prices_bean(holdings),
            ),
            self._write_modular_transactions(dest, append, transactions, balances),
        ]
        return created_files

    def _preflight_modular_append_guard(
        self,
        dest: Path,
        append: bool,
        holdings: Optional[DashboardHoldings],
        balances: Optional[DashboardBalances],
        transactions: Optional[DashboardTransactions],
        effective_opening_date: Optional[str],
    ) -> None:
        """Reject a backward-broadening append before any modular file is touched.

        Mirrors the ``existing_content``/``lot_date`` resolution that
        ``_write_modular_holdings`` would perform, then runs
        :meth:`_guard_window_not_broadened`. Running it here — ahead of every
        write in :meth:`export_modular_ledger` — guarantees a rejected append
        raises :class:`LedgerAppendError` without having partially rewritten
        main.bean / accounts.bean / balances.bean. The guard re-runs harmlessly
        (idempotently) inside ``generate_holdings_bean`` later on.
        """
        holdings_path = dest / "holdings.bean"
        if not (append and holdings_path.exists()):
            return
        _verify_not_symlink(holdings_path)
        existing_h_text = holdings_path.read_text(encoding="utf-8")
        lot_date = self._resolve_lot_date(
            holdings, effective_opening_date, transactions, balances,
        )
        self._guard_window_not_broadened(existing_h_text, lot_date)

    def _write_modular_section(self, dest: Path, filename: str, content: str) -> Path:
        """Write a freshly generated modular component file (always overwritten)."""
        path = dest / filename
        _verify_not_symlink(path)
        path.write_text(content, encoding="utf-8")
        return path

    def _write_modular_main(self, dest: Path, append: bool) -> Path:
        """Write or patch main.bean, ensuring the holdings include and FIFO booking option."""
        main_path = dest / "main.bean"
        _verify_not_symlink(main_path)
        standard_beans = {"main.bean", "accounts.bean", "balances.bean", "holdings.bean", "prices.bean", "transactions.bean"}
        extra_beans = [p.name for p in dest.glob("*.bean") if p.name not in standard_beans and not p.name.startswith(".")]

        if not main_path.exists() or not append:
            main_path.write_text(self.generate_main_bean(additional_includes=extra_beans), encoding="utf-8")
            return main_path
        current_main = main_path.read_text(encoding="utf-8")
        updated_main = current_main
        # Ensure holdings.bean include is present if missing
        if 'include "holdings.bean"' not in updated_main:
            updated_main += '\ninclude "holdings.bean"\n'
        for b in sorted(set(extra_beans)):
            if f'include "{b}"' not in updated_main:
                updated_main += f'include "{b}"\n'
        # Ensure a FIFO booking method is declared so appended sale transactions
        # resolve against the oldest lot even when the ledger was created by an
        # earlier release that omitted the option.
        updated_main = _ensure_fifo_booking_method(updated_main)
        if updated_main != current_main:
            main_path.write_text(updated_main, encoding="utf-8")
        return main_path

    def _write_modular_holdings(
        self,
        dest: Path,
        append: bool,
        holdings: Optional[DashboardHoldings],
        balances: Optional[DashboardBalances],
        transactions: Optional[DashboardTransactions],
        effective_opening_date: Optional[str],
    ) -> Path:
        """Write holdings.bean, appending only the new lot body when appending to an existing file."""
        holdings_path = dest / "holdings.bean"
        _verify_not_symlink(holdings_path)
        existing_h_text = (
            holdings_path.read_text(encoding="utf-8")
            if (holdings_path.exists() and append)
            else None
        )
        # Reconstructed opening lots are derived from transactions too, so emit
        # the holdings section whenever holdings exist *or* there are trades —
        # otherwise sells would reduce an empty inventory and the ledger fails to
        # load. Price points remain gated on a holdings snapshot elsewhere.
        has_txns = bool(transactions and transactions.transactions)
        holdings_content = (
            self.generate_holdings_bean(
                holdings,
                balances=balances,
                transactions=transactions,
                existing_content=existing_h_text,
                opening_date=effective_opening_date,
            )
            if (holdings or has_txns)
            else ""
        )
        if holdings_path.exists() and append:
            if holdings_content:
                to_append = _strip_bean_header(holdings_content)
                if to_append.strip():
                    with open(holdings_path, "a", encoding="utf-8") as f:
                        f.write(to_append)
        else:
            holdings_path.write_text(holdings_content, encoding="utf-8")
        return holdings_path

    def _write_modular_transactions(
        self,
        dest: Path,
        append: bool,
        transactions: Optional[DashboardTransactions],
        balances: Optional[DashboardBalances],
    ) -> Path:
        """Write transactions.bean, appending only non-duplicate transactions on append."""
        tx_path = dest / "transactions.bean"
        _verify_not_symlink(tx_path)
        if transactions and transactions.transactions:
            if tx_path.exists() and append:
                existing_ids = set(
                    re.findall(r'empower_id:\s*"([^"]+)"', tx_path.read_text(encoding="utf-8"))
                )
                delta_container = _build_delta_transactions(transactions, existing_ids)
                if delta_container:
                    delta_text = self.generate_transactions_bean(delta_container, balances=balances)
                    to_append = _strip_bean_header(delta_text)
                    with open(tx_path, "a", encoding="utf-8") as f:
                        f.write(to_append)
            else:
                tx_content = self.generate_transactions_bean(transactions, balances=balances)
                tx_path.write_text(tx_content, encoding="utf-8")
        elif not tx_path.exists():
            tx_path.write_text("", encoding="utf-8")
        return tx_path

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
        target = _prepare_ledger_path(filepath, is_dir=False)
        target.parent.mkdir(parents=True, exist_ok=True)

        effective_opening_date = _determine_opening_date(transactions, balances, opening_date)

        if target.exists() and append:
            self._append_single_file(
                target, balances, holdings, transactions, effective_opening_date,
            )
        else:
            self._write_new_single_file(
                target, balances, holdings, transactions, effective_opening_date,
            )
        return target

    def _append_single_file(
        self,
        target: Path,
        balances: Optional[DashboardBalances],
        holdings: Optional[DashboardHoldings],
        transactions: Optional[DashboardTransactions],
        effective_opening_date: Optional[str],
    ) -> None:
        """Append new assertions, lots, prices, and non-duplicate transactions to an existing ledger."""
        existing_content = target.read_text(encoding="utf-8")
        existing_ids = set(re.findall(r'empower_id:\s*"([^"]+)"', existing_content))

        # Ensure a FIFO booking method is declared so appended sale transactions
        # resolve against the oldest lot even when the file was created by an
        # earlier release that omitted the option.
        patched_content = _ensure_fifo_booking_method(existing_content)
        if patched_content != existing_content:
            # NOSONAR pythonsecurity:S2083 — false positive. ``target`` is the
            # path already validated by _prepare_ledger_path (NUL rejection +
            # leaf-and-parent symlink refusal + resolve()); the taint engine
            # mislabels the ledger's own read-back content as a path source. The
            # write target is never derived from untrusted content.
            target.write_text(patched_content, encoding="utf-8")  # NOSONAR

        delta_chunks: List[str] = [
            "\n\n;; ------------------------------------------------------------------------------\n"
            ";; Empower Personal Dashboard Append Export\n"
            ";; ------------------------------------------------------------------------------\n\n"
        ]

        # Append balance assertions if available
        if balances or holdings:
            delta_chunks.append(self.generate_balances_bean(balances, holdings, transactions=transactions))
            delta_chunks.append("\n")

        # Append commodity holdings lots when holdings exist or transactions
        # imply lots to reconstruct (so appended sells resolve against a
        # non-empty inventory). Price points stay gated on a holdings snapshot —
        # there are no market prices to emit without one.
        if holdings or (transactions and transactions.transactions):
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
        if holdings:
            delta_chunks.append(self.generate_prices_bean(holdings))
            delta_chunks.append("\n")

        # Append non-duplicate transactions
        if transactions and transactions.transactions:
            delta_container = _build_delta_transactions(transactions, existing_ids)
            if delta_container:
                delta_chunks.append(self.generate_transactions_bean(delta_container, balances=balances))

        with open(target, "a", encoding="utf-8") as f:
            f.write("".join(delta_chunks))

    def _write_new_single_file(
        self,
        target: Path,
        balances: Optional[DashboardBalances],
        holdings: Optional[DashboardHoldings],
        transactions: Optional[DashboardTransactions],
        effective_opening_date: Optional[str],
    ) -> None:
        """Write a fresh, complete single-file ledger."""
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
