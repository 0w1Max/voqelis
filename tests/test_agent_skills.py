"""Regression tests for repository-local Agent Skill metadata validation."""

from __future__ import annotations

from pathlib import Path

from tools.verify_agent_skills import EXPECTED, verify


def populate(root: Path) -> None:
    for name in EXPECTED:
        path = root / ".agents" / "skills" / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\n"
            f"description: Apply {name} in a defined Voqelis workflow.\n"
            "---\n\n# Instructions\n\nFollow the requested procedure.\n",
            encoding="utf-8",
        )


def test_valid_six_skills_pass(tmp_path: Path) -> None:
    populate(tmp_path)
    assert verify(tmp_path) == []


def test_mismatched_metadata_name_rejected(tmp_path: Path) -> None:
    populate(tmp_path)
    path = tmp_path / ".agents/skills/voqelis-context/SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "name: voqelis-context", "name: incorrect-name"
        ),
        encoding="utf-8",
    )
    assert any("mismatched" in message for message in verify(tmp_path))


def test_broken_relative_reference_rejected(tmp_path: Path) -> None:
    populate(tmp_path)
    path = tmp_path / ".agents/skills/voqelis-develop/SKILL.md"
    with path.open("a", encoding="utf-8") as f:
        f.write("\nRead [missing](../../../does-not-exist.md).\n")
    assert any("broken" in message for message in verify(tmp_path))


def test_missing_skill_rejected(tmp_path: Path) -> None:
    populate(tmp_path)
    path = tmp_path / ".agents/skills/voqelis-github/SKILL.md"
    path.unlink()
    assert any("Missing required skill: voqelis-github" in m for m in verify(tmp_path))
