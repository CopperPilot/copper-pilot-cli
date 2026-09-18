from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins/copper-pilot"
PORTABLE_SKILL = ROOT / "skills/copper-pilot-review/SKILL.md"
CLAUDE_ONLY_KEYS = {
    "allowed-tools",
    "disallowed-tools",
    "context",
    "agent",
    "background",
    "disable-model-invocation",
    "argument-hint",
    "model",
    "hooks",
    "mcpServers",
}


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    _, raw, _body = text.split("---", 2)
    fields: dict[str, str] = {}
    for line in raw.strip().splitlines():
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def test_marketplace_and_plugin_manifests_parse() -> None:
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    plugin = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    mcp = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))
    assert marketplace["plugins"][0]["source"] == "./plugins/copper-pilot"
    assert plugin["name"] == "copper-pilot"
    assert re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", plugin["version"])
    server = mcp["mcpServers"]["copper-pilot"]
    assert server["command"] == "copper-pilot"
    assert server["args"] == ["mcp"]


def test_plugin_agent_and_skill_exist() -> None:
    agent = PLUGIN / "agents/copper-pilot.md"
    skill = PLUGIN / "skills/copper-pilot-review/SKILL.md"
    assert agent.is_file()
    assert skill.is_file()
    agent_text = agent.read_text(encoding="utf-8")
    assert "mcp__plugin_copper-pilot_copper-pilot__ask" in agent_text
    assert "disallowedTools: Write, Edit" in agent_text
    skill_fields = _frontmatter(skill)
    assert skill_fields["name"] == "copper-pilot-review"
    assert skill_fields["agent"] == "copper-pilot"
    assert skill_fields["context"] == "fork"


def test_portable_skill_uses_spec_frontmatter_only() -> None:
    fields = _frontmatter(PORTABLE_SKILL)
    assert fields["name"] == "copper-pilot-review"
    assert "description" in fields
    assert fields["license"] == "MIT"
    assert not CLAUDE_ONLY_KEYS.intersection(fields)
    body = PORTABLE_SKILL.read_text(encoding="utf-8")
    assert "copper-pilot -n -m" in body
    assert "--json --no-stream" in body
