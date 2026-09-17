from __future__ import annotations

import ast
import importlib.metadata
import re
import tomllib
from pathlib import Path

from copper_pilot_cli.diagnostics import redact

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "src/copper_pilot_cli"


def test_no_forbidden_server_references_or_runtime_surfaces() -> None:
    forbidden_content = ("copper-pilot-server", "copper_pilot_core")
    forbidden_files = (
        "model_provider",
        "mcp",
        "sandbox",
        "computer_use",
        "langsmith",
    )
    for file in PACKAGE.rglob("*"):
        if not file.is_file() or file.suffix not in {".py", ".md", ".json"}:
            continue
        relative = file.relative_to(PACKAGE).as_posix().lower()
        assert not any(token in relative for token in forbidden_files), relative
        text = file.read_text(encoding="utf-8").lower()
        assert not any(token in text for token in forbidden_content), relative


def test_presentation_does_not_import_upstream_runtime_authority() -> None:
    forbidden_imports = (
        "deepagents_code.agent",
        "deepagents_code.app",
        "deepagents_code.tui.textual_adapter",
        "langsmith",
    )
    for file in PACKAGE.rglob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        imports = {
            alias.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module.lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert not any(
            imported.startswith(token) for imported in imports for token in forbidden_imports
        ), file.relative_to(PACKAGE)


def test_product_ui_has_copper_only_identity() -> None:
    surfaces = [
        PACKAGE / "copper_app.py",
        PACKAGE / "copper_main.py",
        PACKAGE / "copper_widgets.py",
    ]
    combined = "\n".join(file.read_text(encoding="utf-8").lower() for file in surfaces)
    assert "model selector" not in combined
    assert "/model" not in combined
    assert "langsmith" not in combined
    assert "deep agents" not in combined


def test_both_console_aliases_are_installed() -> None:
    entrypoints = {
        item.name: item.value
        for item in importlib.metadata.entry_points(group="console_scripts")
        if item.name.startswith("copper-pilot")
    }
    assert entrypoints == {
        "copper-pilot": "copper_pilot_cli:cli_main",
        "copper-pilot-cli": "copper_pilot_cli:cli_main",
    }


def test_deepagents_runtime_is_pinned_as_a_required_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "deepagents==0.7.14" in project["dependencies"]
    assert "deepagents" not in project.get("optional-dependencies", {})


def test_diagnostics_redact_credentials_and_content() -> None:
    value = redact("Authorization: Bearer cf_live_secret\nprivate prompt")
    assert "cf_live_secret" not in value
    assert "private prompt" not in value


def test_version_file_is_source_of_truth() -> None:
    version_path = ROOT / "VERSION"
    assert version_path.is_file()
    file_version = version_path.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", file_version)

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    hatch_version = data["tool"]["hatch"]["version"]
    assert "version" not in project
    assert project.get("dynamic") == ["version"]
    assert hatch_version["source"] == "regex"
    assert hatch_version["path"] == "VERSION"

    from copper_pilot_cli import __version__

    assert __version__ == file_version
    assert importlib.metadata.version("copper-pilot-cli") == file_version


def test_community_health_files_exist() -> None:
    required = (
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "SUPPORT.md",
        "HISTORY.md",
        "LICENSE",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/docs.yml",
        ".github/dependabot.yml",
        "docs/cli.md",
        "docs/development.md",
    )
    missing = [path for path in required if not (ROOT / path).is_file()]
    assert missing == []


def test_issue_templates_are_github_forms() -> None:
    for name in ("bug_report.yml", "feature_request.yml", "docs.yml"):
        text = (ROOT / ".github/ISSUE_TEMPLATE" / name).read_text(encoding="utf-8")
        assert re.search(r"^name:", text, flags=re.MULTILINE)
        assert re.search(r"^description:", text, flags=re.MULTILINE)
    bug = (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(encoding="utf-8")
    assert "Steps to reproduce" in bug
    assert "required: true" in bug


def test_readme_links_community_docs() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in (
        "CONTRIBUTING.md",
        "LICENSE",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "docs/cli.md",
    ):
        assert needle in readme


def test_project_urls_include_community_links() -> None:
    urls = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["urls"]
    for key in ("Homepage", "Repository", "Issues", "Documentation", "Changelog"):
        assert key in urls, key


def test_readme_media_uses_absolute_github_urls() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    prefix = "https://github.com/CopperPilot/copper-pilot-cli/blob/main/"
    suffix = "?raw=true"
    sources = re.findall(r'<img\b[^>]*\bsrc="([^"]+)"', readme)
    assert sources, "README must include media for PyPI rendering"
    for src in sources:
        assert src.startswith(prefix) and src.endswith(suffix), src
        assert (ROOT / src.removeprefix(prefix).removesuffix(suffix)).is_file(), src
