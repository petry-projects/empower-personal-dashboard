"""
models.py — Data models for Empower Personal Dashboard balances, holdings, and transactions.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AccountBalance:
    account_id: str
    account_name: str
    firm_name: str
    account_type: str
    balance: float
    is_asset: bool = True
    currency: str = "USD"
    last_refreshed: Optional[str] = None
    user_account_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "firm_name": self.firm_name,
            "account_type": self.account_type,
            "balance": round(self.balance, 2),
            "is_asset": self.is_asset,
            "currency": self.currency,
            "last_refreshed": self.last_refreshed,
            "user_account_id": self.user_account_id,
        }


@dataclass
class DashboardBalances:
    as_of_date: str
    net_worth: float
    total_cash: float
    total_investment: float
    total_credit_card: float
    total_loan: float
    total_mortgage: float
    accounts: List[Dict[str, Any]]
    total_other_assets: float = 0.0
    total_other_liabilities: float = 0.0
    mode: str = "live"
    raw_response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of_date": self.as_of_date,
            "net_worth": round(self.net_worth, 2),
            "total_cash": round(self.total_cash, 2),
            "total_investment": round(self.total_investment, 2),
            "total_credit_card": round(self.total_credit_card, 2),
            "total_loan": round(self.total_loan, 2),
            "total_mortgage": round(self.total_mortgage, 2),
            "total_other_assets": round(self.total_other_assets, 2),
            "total_other_liabilities": round(self.total_other_liabilities, 2),
            "accounts_count": len(self.accounts),
            "accounts": self.accounts,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DashboardBalances":
        return cls(
            as_of_date=data.get("as_of_date", ""),
            net_worth=float(data.get("net_worth", 0.0)),
            total_cash=float(data.get("total_cash", 0.0)),
            total_investment=float(data.get("total_investment", 0.0)),
            total_credit_card=float(data.get("total_credit_card", 0.0)),
            total_loan=float(data.get("total_loan", 0.0)),
            total_mortgage=float(data.get("total_mortgage", 0.0)),
            total_other_assets=float(data.get("total_other_assets", 0.0)),
            total_other_liabilities=float(data.get("total_other_liabilities", 0.0)),
            accounts=list(data.get("accounts", [])),
            mode=data.get("mode", "historical"),
            raw_response=data.get("raw_response"),
        )


@dataclass
class InvestmentHolding:
    user_account_id: Optional[int]
    account_name: str
    ticker: str
    cusip: str
    description: str
    holding_type: str
    quantity: float
    price: float
    value: float
    cost_basis: Optional[float] = None
    holding_percentage: Optional[float] = None
    one_day_percent_change: Optional[float] = None
    one_day_value_change: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_account_id": self.user_account_id,
            "account_name": self.account_name,
            "ticker": self.ticker,
            "cusip": self.cusip,
            "description": self.description,
            "holding_type": self.holding_type,
            "quantity": self.quantity,
            "price": round(self.price, 2),
            "value": round(self.value, 2),
            "cost_basis": round(self.cost_basis, 2) if self.cost_basis is not None else None,
            "holding_percentage": round(self.holding_percentage, 2) if self.holding_percentage is not None else None,
            "one_day_percent_change": self.one_day_percent_change,
            "one_day_value_change": round(self.one_day_value_change, 2) if self.one_day_value_change is not None else None,
        }


@dataclass
class DashboardHoldings:
    as_of_date: str
    total_value: float
    holdings: List[Dict[str, Any]]
    mode: str = "live"
    raw_response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of_date": self.as_of_date,
            "total_value": round(self.total_value, 2),
            "holdings_count": len(self.holdings),
            "holdings": self.holdings,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DashboardHoldings":
        return cls(
            as_of_date=data.get("as_of_date", ""),
            total_value=float(data.get("total_value", 0.0)),
            holdings=list(data.get("holdings", [])),
            mode=data.get("mode", "historical"),
            raw_response=data.get("raw_response"),
        )


@dataclass
class Transaction:
    user_transaction_id: str
    account_id: str
    user_account_id: Optional[int]
    account_name: str
    transaction_date: str
    description: str
    original_description: str
    amount: float
    is_credit: bool
    is_cash_in: bool
    is_cash_out: bool
    is_income: bool
    is_spending: bool
    transaction_type: str
    investment_type: Optional[str] = None
    symbol: Optional[str] = None
    price: Optional[float] = None
    quantity: Optional[float] = None
    status: str = "posted"
    category_id: Optional[int] = None
    category: Optional[str] = None
    category_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_transaction_id": self.user_transaction_id,
            "account_id": self.account_id,
            "user_account_id": self.user_account_id,
            "account_name": self.account_name,
            "transaction_date": self.transaction_date,
            "description": self.description,
            "original_description": self.original_description,
            "amount": round(self.amount, 2),
            "is_credit": self.is_credit,
            "is_cash_in": self.is_cash_in,
            "is_cash_out": self.is_cash_out,
            "is_income": self.is_income,
            "is_spending": self.is_spending,
            "transaction_type": self.transaction_type,
            "investment_type": self.investment_type,
            "symbol": self.symbol,
            "price": self.price,
            "quantity": self.quantity,
            "status": self.status,
            "category_id": self.category_id,
            "category": self.category,
            "category_name": self.category_name,
        }


@dataclass
class DashboardTransactions:
    start_date: str
    end_date: str
    total_transactions: int
    money_in: float
    money_out: float
    net_cashflow: float
    transactions: List[Dict[str, Any]]
    mode: str = "live"
    raw_response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total_transactions": self.total_transactions,
            "money_in": round(self.money_in, 2),
            "money_out": round(self.money_out, 2),
            "net_cashflow": round(self.net_cashflow, 2),
            "transactions": self.transactions,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DashboardTransactions":
        txs = list(data.get("transactions", []))
        return cls(
            start_date=data.get("start_date", ""),
            end_date=data.get("end_date", ""),
            total_transactions=int(data.get("total_transactions", len(txs))),
            money_in=float(data.get("money_in", 0.0)),
            money_out=float(data.get("money_out", 0.0)),
            net_cashflow=float(data.get("net_cashflow", 0.0)),
            transactions=txs,
            mode=data.get("mode", "historical"),
            raw_response=data.get("raw_response"),
        )


@dataclass
class DailyHistoryPoint:
    date: str
    net_worth: float
    total_assets: float = 0.0
    total_liabilities: float = 0.0
    balances: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "net_worth": round(self.net_worth, 2),
            "total_assets": round(self.total_assets, 2),
            "total_liabilities": round(self.total_liabilities, 2),
            "balances": self.balances,
        }


@dataclass
class DashboardHistories:
    start_date: str
    end_date: str
    histories: List[Dict[str, Any]]
    total_points: int = 0
    mode: str = "live"
    raw_response: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total_points": self.total_points or len(self.histories),
            "histories": self.histories,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DashboardHistories":
        raw_hist = list(data.get("histories", []))
        return cls(
            start_date=data.get("start_date", ""),
            end_date=data.get("end_date", ""),
            histories=raw_hist,
            total_points=int(data.get("total_points", len(raw_hist))),
            mode=data.get("mode", "historical"),
            raw_response=data.get("raw_response"),
        )

