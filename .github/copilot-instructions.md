# Copilot Instructions — empower-personal-dashboard

## About

`empower-personal-dashboard` is an open-source, standalone Python client, CLI, and Model Context Protocol (FastMCP) server for Empower Personal Dashboard (formerly Personal Capital). It allows users and unattended automated systems to aggregate linked financial institution data—including bank balances, investment holdings, net worth, and historical transactions—and export data to Beancount, CSV, and JSON.

## Tech Stack

- **Runtime:** Python 3.9, 3.10, 3.11, 3.12, 3.13
- **Core Library:** `requests>=2.28.0` (zero heavyweight framework dependencies for the core client)
- **MCP Server:** FastMCP via `mcp>=1.2.0` (Python 3.10+)
- **API Specification:** OpenAPI 3.1.0 (`docs/openapi.yaml`) linted via Redocly CLI
- **Testing:** `unittest`, `unittest.mock`, `jsonschema>=4.20.0`, `coverage>=7.0`
- **Packaging:** PEP 621 build system via `setuptools>=68.0`, `build`, `twine`

## Project Structure

```
empower-personal-dashboard/
├── .github/
│   ├── workflows/            # GitHub Actions CI, packaging, and org automation stubs
│   ├── CODEOWNERS            # Org team ownership
│   ├── copilot-instructions.md
│   └── dependabot.yml        # Security-focused dependency management
├── empower_personal_dashboard/
│   ├── __init__.py           # Public exports (Client, Models, Exceptions)
│   ├── client.py             # Session management, 2FA bootstrap, API communication
│   ├── models.py             # Strongly typed dataclasses for financial payloads
│   ├── sanitizers.py         # Unicode text sanitization (\ufffd stripping, normalization)
│   ├── exceptions.py         # Domain error hierarchy (EmpowerError, 2FA, SessionExpired)
│   ├── beancount.py          # Pure-Python Beancount PTA export engine
│   ├── cli.py                # Command-line interface (tables, JSON, CSV, Beancount)
│   └── mcp_server.py         # Model Context Protocol (FastMCP) server
├── docs/
│   ├── adr/                  # Architecture Decision Records
│   └── openapi.yaml          # Authoritative OpenAPI 3.1 specification
├── scripts/
│   └── pypi_onboard.py       # PyPI packaging & verification tooling
└── tests/                    # Hermetic, offline unit and contract tests
```

## Local Dev Commands

- Install: `pip install -e ".[all]"`
- Test: `PYTHONPATH=. python3 -m unittest discover tests -v`
- Coverage: `coverage run -m unittest discover tests && coverage report`
- Contract tests: `PYTHONPATH=. python3 -m unittest tests/test_openapi_contract.py`
- OpenAPI lint: `npx --yes @redocly/cli@2.57.0 lint docs/openapi.yaml`
- Byte-compile check: `python3 -m compileall empower_personal_dashboard tests`
- Build package: `python3 -m build`

## Required Environment Variables

- `EMPOWER_USERNAME`: (Optional) User login email for Empower Personal Dashboard bootstrap.
- `EMPOWER_PASSWORD`: (Optional) User password for Empower Personal Dashboard bootstrap.
- `EMPOWER_SESSION_FILE`: (Optional) Custom session cache path (defaults to `~/.empower_personal_dashboard_session.json`).

> **Security Mandate:** Never commit real credentials, API tokens, session files, or personal financial data. All test fixtures must be 100% synthetic placeholders.

## Testing Framework

- Runner: Python standard library `unittest`
- Hermetic & offline: Tests must pass in under 1 second without real network requests.
- Contract validation: `jsonschema` validates API mocks against `docs/openapi.yaml`.
- Zero-PII policy: All test fixtures use generic synthetic placeholders.

## Repo-Specific Overrides

- Transport Cleanliness: Passes `"Accept-Encoding": "identity"` to prevent edge gzip corruption.
- Text Sanitization: Always use `clean_api_text()` in `empower_personal_dashboard/sanitizers.py` to scrub unicode replacement characters (`\ufffd`).
- Session Security: Session files are written with POSIX `0600` permissions.

## Org Standards

See [petry-projects/.github — AGENTS.md](https://github.com/petry-projects/.github/blob/main/AGENTS.md)
for org-wide development standards.
