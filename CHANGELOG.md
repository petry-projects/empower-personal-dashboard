# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-10-03

### Added
- Added `CHANGELOG.md` documenting historical and current releases.
- Registered `Changelog` URL under `[project.urls]` in `pyproject.toml` so PyPI displays a direct release notes link in the project sidebar.
- Added packaging unit tests in `tests/test_packaging.py` validating `CHANGELOG.md` presence, structure, and `Changelog` project URL registration.
- Added Beancount export enhancements: enabled Fava inversion in generated ledgers and added a baseline opening date option for holdings (`--opening-date`).

### Changed
- Updated documentation (`README.md` and `AGENTS.md`) with Changelog links and architecture references.
- Updated GitHub Actions CI dependencies (`upload-artifact`, `download-artifact`, `setup-node`, `setup-java`, `sonarqube-scan-action`).

## [0.1.1] - 2026-10-03

### Added
- **Automated On-Merge Release Workflow:**
  - Added `push: branches: [main]` trigger in `.github/workflows/publish.yml` to automatically publish releases to PyPI and generate GitHub Releases when version is bumped.
  - Added version extraction and release gate helpers in `scripts/pypi_onboard.py` (`get_package_version`, `should_release_version`) with PyPI lookup validation.
  - Implemented least-privilege isolated CI jobs (`prepare`, `build`, `publish`, `release`) with concurrency serialization to eliminate race conditions.
- Added `--timeout` CLI flag and increased default client timeout to 90s for large historical queries.

### Fixed
- Fixed CLI transaction extraction to allow full historical extraction when `--start-date` is omitted.
- Fixed Beancount export: sanitized commodity tickers, unconstrained investment accounts, opened all category legs, padded non-zero balances, and supported quoted YAML keys with colons.
- Fixed regex payee rules to match against both transaction description and memo.
- Fixed mock client start date computation across all transactions before applying limit.

## [0.1.0] - 2026-10-02

### Added
- Initial PyPI release of `empower-personal-dashboard`.
- Standalone Python client (`EmpowerDashboardClient`) for Empower Personal Dashboard (formerly Personal Capital) with:
  - Account balances and net worth aggregation (`fetch_balances`).
  - Investment holdings and security positions (`fetch_holdings`).
  - Historical transactions with filtering (`fetch_transactions`).
  - Two-phase authentication lifecycle with interactive 2FA bootstrap (`--login`) and unattended session persistence.
  - Transparent endpoint migration routing for unified accounts (`pc-api.empower-retirement.com`).
  - Text sanitization (`clean_api_text`) for Unicode replacement character (`\ufffd`) cleanup.
- Feature-complete command-line interface (`empower`):
  - Formatted tables, JSON, and CSV exports.
  - Offline sandbox simulation mode (`--sandbox`).
  - Offline data replay loading (`--from-data-dir`, `--input-*`).
- Plain-Text Accounting (PTA) Beancount export engine:
  - Double-entry transaction generation, balance assertions, price directives, and lot tracking.
  - YAML account mapping and rule configuration.
  - Architectural documentation in ADR-0003.
- FastMCP Model Context Protocol server (`empower-mcp`) providing tools and resources for AI agents.
- Authoritative OpenAPI 3.1 specification (`docs/openapi.yaml`) with Redocly CI linting and hermetic contract tests.
- PyPI onboarding probe, build verification, and Trusted Publishing OIDC workflow.

[Unreleased]: https://github.com/petry-projects/empower-personal-dashboard/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/petry-projects/empower-personal-dashboard/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/petry-projects/empower-personal-dashboard/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/petry-projects/empower-personal-dashboard/releases/tag/v0.1.0
