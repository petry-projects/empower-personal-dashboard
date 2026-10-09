"""
empower_personal_dashboard — Python client & CLI for Empower Personal Dashboard.

Export all core models, exceptions, and client.
"""

from .client import (
    DEFAULT_BASE_URL,
    DEFAULT_SESSION_FILE,
    MIGRATED_BASE_URL,
    EmpowerDashboardClient,
)
from .exceptions import (
    EmpowerError,
    LedgerAppendError,
    LoginFailedException,
    ReconstructionWindowError,
    RequireTwoFactorException,
    SessionExpiredError,
)
from .models import (
    AccountBalance,
    DailyHistoryPoint,
    DashboardBalances,
    DashboardHistories,
    DashboardHoldings,
    DashboardTransactions,
    InvestmentHolding,
    Transaction,
)
from .beancount import (
    BeancountGenerator,
    BeancountMapper,
    slugify_account_name,
)
from .sanitizers import clean_api_text

__version__ = "0.3.0"

def create_mcp_server(*args, **kwargs):
    """Lazy loader for creating the FastMCP server instance."""
    from .mcp_server import create_mcp_server as _create_server
    return _create_server(*args, **kwargs)


__all__ = [
    "EmpowerDashboardClient",
    "AccountBalance",
    "DailyHistoryPoint",
    "DashboardBalances",
    "DashboardHistories",
    "InvestmentHolding",
    "DashboardHoldings",
    "Transaction",
    "DashboardTransactions",
    "EmpowerError",
    "RequireTwoFactorException",
    "SessionExpiredError",
    "LoginFailedException",
    "ReconstructionWindowError",
    "LedgerAppendError",
    "clean_api_text",
    "DEFAULT_BASE_URL",
    "MIGRATED_BASE_URL",
    "DEFAULT_SESSION_FILE",
    "create_mcp_server",
    "BeancountGenerator",
    "BeancountMapper",
    "slugify_account_name",
]


