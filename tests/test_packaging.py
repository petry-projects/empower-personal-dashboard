"""Unit tests for packaging metadata, PyPI onboarding script, and publish workflow."""
from __future__ import annotations

import socket
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT_PATH = ROOT / "pyproject.toml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
EXPECTED_VERSION = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))["project"]["version"]


class TestPackaging(unittest.TestCase):
    def test_pyproject_toml_structure(self):
        self.assertTrue(PYPROJECT_PATH.exists(), "pyproject.toml must exist")
        content = PYPROJECT_PATH.read_text(encoding="utf-8")
        data = tomllib.loads(content)

        project = data.get("project", {})
        self.assertEqual(project.get("name"), "empower-personal-dashboard")
        self.assertTrue(project.get("version"))
        self.assertEqual(project.get("license"), "MIT")
        keywords = project.get("keywords", [])
        self.assertIn("empower", keywords)
        self.assertIn("personal-capital", keywords)
        self.assertIn("finance", keywords)

        authors = project.get("authors", [])
        self.assertGreater(len(authors), 0)
        self.assertEqual(authors[0].get("name"), "Don Petry")

        classifiers = project.get("classifiers", [])
        self.assertNotIn("License :: OSI Approved :: MIT License", classifiers)  # PEP 639
        self.assertIn("Programming Language :: Python :: 3", classifiers)

        urls = project.get("urls", {})
        self.assertIn("https://github.com/petry-projects/empower-personal-dashboard", urls.get("Homepage", ""))
        self.assertIn("https://github.com/petry-projects/empower-personal-dashboard", urls.get("Repository", ""))
        self.assertEqual(urls.get("Changelog"), "https://github.com/petry-projects/empower-personal-dashboard/blob/main/CHANGELOG.md")

        opt_deps = project.get("optional-dependencies", {})
        self.assertIn("build", opt_deps)
        self.assertIn("dev", opt_deps)
        self.assertIn("all", opt_deps)
        self.assertIn("mcp", opt_deps)
        self.assertIn("test", opt_deps)
        self.assertIn("beancount", opt_deps)

    def test_check_pypi_status_available(self):
        from scripts.pypi_onboard import PACKAGE_NAME, check_pypi_status

        mock_err = urllib.error.HTTPError(
            url="https://pypi.org/pypi/empower-personal-dashboard/json",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=mock_err):
            status, detail = check_pypi_status(PACKAGE_NAME)
            self.assertEqual(status, "AVAILABLE")
            self.assertIn("available", detail.lower())

    def test_check_pypi_status_taken(self):
        from scripts.pypi_onboard import PACKAGE_NAME, check_pypi_status

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"info": {"version": "0.1.0"}}'
        mock_resp.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_resp):
            status, detail = check_pypi_status(PACKAGE_NAME)
            self.assertEqual(status, "TAKEN")
            self.assertIn("0.1.0", detail)

    def test_check_pypi_status_http_error(self):
        from scripts.pypi_onboard import PACKAGE_NAME, check_pypi_status

        mock_err = urllib.error.HTTPError(
            url="https://pypi.org/pypi/empower-personal-dashboard/json",
            code=500,
            msg="Server Error",
            hdrs={},
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=mock_err):
            status, detail = check_pypi_status(PACKAGE_NAME)
            self.assertEqual(status, "UNKNOWN")
            self.assertIn("500", detail)

    def test_check_build_tools(self):
        from scripts.pypi_onboard import check_build_tools

        missing = check_build_tools()
        self.assertIsInstance(missing, list)

    def test_pending_publisher_hint_content(self):
        from scripts.pypi_onboard import PACKAGE_NAME, PENDING_PUBLISHER_HINT

        self.assertIn(PACKAGE_NAME, PENDING_PUBLISHER_HINT)
        self.assertIn("petry-projects", PENDING_PUBLISHER_HINT)
        self.assertIn("publish.yml", PENDING_PUBLISHER_HINT)
        self.assertIn("pypi", PENDING_PUBLISHER_HINT)

    def test_get_package_version(self):
        import tempfile
        from scripts.pypi_onboard import get_package_version

        content = PYPROJECT_PATH.read_text(encoding="utf-8")
        expected_version = tomllib.loads(content)["project"]["version"]
        self.assertEqual(get_package_version(), expected_version)

        with tempfile.TemporaryDirectory() as tmpdir:
            custom_toml = Path(tmpdir) / "pyproject.toml"
            custom_toml.write_text('[project]\nname = "test"\nversion = "1.2.3"\n', encoding="utf-8")
            self.assertEqual(get_package_version(custom_toml), "1.2.3")

            # Fallback to regex when both tomllib and tomli are unavailable
            with patch.dict("sys.modules", {"tomllib": None, "tomli": None}):
                spaced_toml = Path(tmpdir) / "spaced.toml"
                spaced_toml.write_text('[project]\nversion = "2.3.4"\n', encoding="utf-8")
                self.assertEqual(get_package_version(spaced_toml), "2.3.4")

    def test_project_version_from_text_scoped_to_project_section(self):
        from scripts.pypi_onboard import _project_version_from_text

        toml_text = (
            '[tool.some_tool]  # config\n'
            'version = "9.9.9"\n'
            '\n'
            '[project]  # project metadata\n'
            'name = "demo"\n'
            'version = "1.2.3"  # semver version\n'
            '\n'
            '[tool.other]\n'
            'version = "0.0.0"\n'
        )
        self.assertEqual(_project_version_from_text(toml_text), "1.2.3")
        self.assertEqual(_project_version_from_text('[tool.x]\nversion = "7.7.7"\n'), "")

    def test_should_release_version(self):
        from scripts.pypi_onboard import should_release_version

        existing = ["v0.1.0", "v0.1.1", "v0.1.2"]
        self.assertFalse(should_release_version("0.1.2", existing))
        self.assertTrue(should_release_version("0.1.3", existing))
        self.assertFalse(should_release_version("", existing))
        self.assertFalse(should_release_version("0.1.0", ["0.1.0"]))

    def test_should_release_version_with_pypi_check(self):
        from scripts.pypi_onboard import should_release_version

        with patch("scripts.pypi_onboard.is_version_published_on_pypi", return_value=True):
            # Version not in git tags, but already exists on PyPI
            self.assertFalse(should_release_version("0.1.0", [], check_pypi=True))

        with patch("scripts.pypi_onboard.is_version_published_on_pypi", return_value=False):
            # Version not in git tags and not on PyPI
            self.assertTrue(should_release_version("0.1.1", [], check_pypi=True))

        mock_500 = urllib.error.HTTPError(
            url="https://pypi.org/pypi/empower-personal-dashboard/0.1.1/json",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )
        with patch("scripts.pypi_onboard.is_version_published_on_pypi", side_effect=mock_500):
            with self.assertRaises(urllib.error.HTTPError):
                should_release_version("0.1.1", [], check_pypi=True)

        mock_timeout_url_err = urllib.error.URLError(socket.timeout("timed out"))
        with patch("scripts.pypi_onboard.is_version_published_on_pypi", side_effect=mock_timeout_url_err):
            with self.assertRaises(urllib.error.URLError):
                should_release_version("0.1.1", [], check_pypi=True)

    def test_is_version_published_on_pypi(self):
        from scripts.pypi_onboard import is_version_published_on_pypi

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_resp):
            self.assertTrue(is_version_published_on_pypi("empower-personal-dashboard", "0.1.0"))

        mock_404 = urllib.error.HTTPError(
            url="https://pypi.org/pypi/empower-personal-dashboard/0.1.1/json",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=mock_404):
            self.assertFalse(is_version_published_on_pypi("empower-personal-dashboard", "0.1.1"))
        self.assertFalse(is_version_published_on_pypi("empower-personal-dashboard", ""))

        mock_500 = urllib.error.HTTPError(
            url="https://pypi.org/pypi/empower-personal-dashboard/0.1.1/json",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=mock_500):
            with self.assertRaises(urllib.error.HTTPError):
                is_version_published_on_pypi("empower-personal-dashboard", "0.1.1")

        mock_timeout_url_err = urllib.error.URLError(socket.timeout("timed out"))
        with patch("urllib.request.urlopen", side_effect=mock_timeout_url_err):
            with self.assertRaises(urllib.error.URLError):
                is_version_published_on_pypi("empower-personal-dashboard", "0.1.1")

    def test_pypi_onboard_version_cli(self):
        import io
        from scripts.pypi_onboard import main

        content = PYPROJECT_PATH.read_text(encoding="utf-8")
        expected_version = tomllib.loads(content)["project"]["version"]

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue().strip(), expected_version)

    def test_publish_workflow_structure(self):
        self.assertTrue(PUBLISH_WORKFLOW.exists(), "publish.yml workflow must exist")
        text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

        # Workflow triggers
        self.assertIn("release:", text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("dry_run:", text)
        self.assertIn("push:", text)
        self.assertIn("branches: [main]", text)

        # Permissions
        self.assertIn("id-token: write", text)
        self.assertIn("contents: read", text)
        self.assertIn("contents: write", text)

        # Environment
        self.assertIn("environment:", text)
        self.assertIn("name: pypi", text)

        # Checkout credentials must not be persisted
        self.assertIn("persist-credentials: false", text)

        # Publication runs are serialized
        self.assertIn("concurrency:", text)
        self.assertIn("group: pypi-publish", text)

        # Release events must match packaged version
        self.assertIn("RELEASE_TAG", text)

        # Build happens in a dedicated job passing artifacts
        self.assertIn("upload-artifact", text)
        self.assertIn("download-artifact", text)

        # Action pinning to commit SHAs (no naked @v1 or @v4)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("uses:"):
                parts = line.split("@", 1)
                self.assertEqual(len(parts), 2, f"Action reference must be pinned with @: {line}")
                ref_part = parts[1].split()[0]
                self.assertEqual(len(ref_part), 40, f"Action must be pinned to 40-character SHA: {line}")

    def test_changelog_structure(self):
        changelog_path = ROOT / "CHANGELOG.md"
        self.assertTrue(changelog_path.exists(), "CHANGELOG.md must exist")
        content = changelog_path.read_text(encoding="utf-8")
        self.assertIn("# Changelog", content)
        self.assertIn("## [Unreleased]", content)
        self.assertIn(f"## [{EXPECTED_VERSION}]", content)
        self.assertIn("## [0.1.1]", content)
        self.assertIn("## [0.1.0]", content)
        self.assertIn("[Unreleased]:", content)
        self.assertIn(f"[{EXPECTED_VERSION}]:", content)


if __name__ == "__main__":
    unittest.main()
