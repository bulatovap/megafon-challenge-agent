"""Конфигурация Claude Code: каждый skill и субагент — непустой файл с именем и описанием."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def front(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return {}
    fields = dict(re.findall(r"(?m)^([\w-]+):\s*(.*)$", m.group(1)))
    fields["_body"] = m.group(2)
    return fields


class TestClaudeConfig(unittest.TestCase):
    def test_skills(self):
        skills = sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))
        self.assertGreaterEqual(len(skills), 10)
        for p in skills:
            f = front(p)
            self.assertEqual(f.get("name"), p.parent.name, p)
            self.assertGreater(len(f.get("description", "")), 40, p)
            self.assertGreater(len(f.get("_body", "").strip()), 200, p)

    def test_agents(self):
        agents = sorted((ROOT / ".claude" / "agents").glob("*.md"))
        self.assertEqual(len(agents), 4)
        for p in agents:
            f = front(p)
            self.assertEqual(f.get("name"), p.stem, p)
            self.assertTrue(f.get("description") and f.get("tools") and f.get("model"), p)
            self.assertGreater(len(f.get("_body", "").strip()), 100, p)

    def test_claude_md_points_to_existing_skills(self):
        names = {p.parent.name for p in (ROOT / ".claude" / "skills").glob("*/SKILL.md")}
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8") + (ROOT / "CLAUDE.features.md").read_text(encoding="utf-8")
        used = set(re.findall(r"`/([a-z][a-z-]+)`", text))
        self.assertFalse(used - names, f"в CLAUDE.md упомянуты несуществующие skills: {used - names}")
