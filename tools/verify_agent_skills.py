"""Validate repo-local Agent Skills without network access or dependencies."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

EXPECTED = frozenset({
    "voqelis-context",
    "voqelis-develop",
    "voqelis-quality",
    "voqelis-release",
    "voqelis-github",
    "voqelis-telegram-qa",
})
NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
LINK = re.compile(r"\]\(([^)]+)\)")


def verify(root: Path) -> list[str]:
    """Return errors instead of writing to files or changing repository state."""
    root = root.resolve()
    directory = root / ".agents" / "skills"
    errors: list[str] = []
    seen: set[str] = set()

    if not directory.is_dir():
        return ["Missing .agents/skills directory"]

    folders = sorted(p for p in directory.iterdir() if p.is_dir())
    for folder in folders:
        manifest = folder / "SKILL.md"
        rel = manifest.relative_to(root).as_posix()
        if not manifest.is_file() or manifest.is_symlink():
            errors.append(f"{rel}: missing or symlinked SKILL.md")
            continue
        try:
            content = manifest.read_text(encoding="utf-8")
        except (UnicodeError, OSError) as exc:
            errors.append(f"{rel}: cannot read UTF-8 manifest ({exc.__class__.__name__})")
            continue
        lines = content.splitlines()
        if len(lines) < 5 or lines[0] != "---":
            errors.append(f"{rel}: missing YAML frontmatter")
            continue
        try:
            closing = lines.index("---", 1)
        except ValueError:
            errors.append(f"{rel}: frontmatter is not closed")
            continue
        metadata: dict[str, str] = {}
        for item in lines[1:closing]:
            if ":" not in item:
                errors.append(f"{rel}: malformed frontmatter entry")
                continue
            key, value = item.split(":", 1)
            key, value = key.strip(), value.strip()
            if key in metadata:
                errors.append(f"{rel}: duplicate metadata field '{key}'")
            metadata[key] = value
        name = metadata.get("name", "")
        desc = metadata.get("description", "")
        if not NAME.fullmatch(name) or name != folder.name or name in seen:
            errors.append(f"{rel}: invalid, mismatched or duplicate skill name")
        seen.add(name)
        if not desc or len(desc) > 1024:
            errors.append(f"{rel}: description must contain 1-1024 characters")
        if not any(line.strip() for line in lines[closing + 1:]):
            errors.append(f"{rel}: no instructions")
        for link in LINK.findall(content):
            if not link.startswith(("./", "../")):
                continue
            target_text = link.partition("#")[0]
            target = (folder / target_text).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                errors.append(f"{rel}: broken or external relative link '{link}'")

    for name in sorted(EXPECTED - seen):
        errors.append(f"Missing required skill: {name}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1],
        help="Repository root (default: script's parent repository)",
    )
    args = parser.parse_args()
    errors = verify(args.root)
    if errors:
        for problem in errors:
            print(f"FAIL · {problem}", file=sys.stderr)
        return 1
    print(f"PASS · {len(EXPECTED)} required Voqelis Agent Skills validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
