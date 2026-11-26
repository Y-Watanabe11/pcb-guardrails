"""The skill and plugin files for AI agents stay in step with each other and with the package."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "pcb-guardrails"
SKILL = PLUGIN / "skills" / "pcb-guardrails" / "SKILL.md"
# Copies for agents that read project skills from fixed folders: Claude Code (.claude/skills) and Codex
# (.agents/skills). The plugin's copy is the source; edit it and copy it over.
SKILL_COPIES = [
    ROOT / ".claude" / "skills" / "pcb-guardrails" / "SKILL.md",
    ROOT / ".agents" / "skills" / "pcb-guardrails" / "SKILL.md",
]


def package_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match, "no version in pyproject.toml"
    return match.group(1)


def test_skill_copies_match_the_plugin_skill():
    source = SKILL.read_bytes()
    for copy in SKILL_COPIES:
        assert copy.read_bytes() == source, f"{copy.relative_to(ROOT)} differs from {SKILL.relative_to(ROOT)}"


def test_skill_frontmatter_has_only_agent_skills_fields():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---\n")[1]
    keys = {line.split(":", 1)[0] for line in frontmatter.splitlines() if line and not line.startswith(" ")}
    assert {"name", "description"} <= keys
    assert keys <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


def test_plugin_version_is_the_package_version():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == package_version()


def test_marketplace_points_at_the_plugin():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    (entry,) = marketplace["plugins"]
    plugin_dir = (ROOT / entry["source"]).resolve()
    assert plugin_dir == PLUGIN.resolve()
    manifest = json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == entry["name"]
