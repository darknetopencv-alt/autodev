"""Skill catalog helpers for bundled and project-local skills."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from importlib.resources import files


DEFAULT_PROJECT_SKILLS: tuple[str, ...] = (
    "autodev-runtime",
    "coca-spec",
    "spec-driven-develop",
    "find-skills",
    "skill-creator",
    "color-font-skill",
    "design-style-skill",
    "ppt-editing-skill",
    "ppt-orchestra-skill",
    "slide-making-skill",
    "pptx-agents",
)


@dataclass(frozen=True)
class SkillInfo:
    """Metadata for one discoverable skill."""

    name: str
    directory_name: str
    description: str
    category: str
    triggers: tuple[str, ...]
    path: str


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def bundled_skills_root():
    """Return the packaged skills root."""
    return files("autodev").joinpath("skills")


def _extract_frontmatter_value(frontmatter: str, key: str) -> str:
    lines = frontmatter.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(f"{key}:"):
            continue

        value = stripped.split(":", 1)[1].strip()
        if value in {">", ">-", "|", "|-"}:
            collected: list[str] = []
            for next_line in lines[index + 1 :]:
                if next_line.startswith(" ") or next_line.startswith("\t"):
                    collected.append(next_line.strip())
                    continue
                break
            return " ".join(part for part in collected if part).strip()
        return value.strip('"').strip("'")
    return ""


def _extract_frontmatter_list(frontmatter: str, key: str) -> tuple[str, ...]:
    lines = frontmatter.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith(f"{key}:"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        value = stripped.split(":", 1)[1].strip()
        if value.startswith("[") and value.endswith("]"):
            parts = [part.strip().strip('"').strip("'") for part in value[1:-1].split(",")]
            return tuple(part for part in parts if part)
        if value:
            return (value.strip('"').strip("'"),)

        items: list[str] = []
        for next_line in lines[index + 1 :]:
            next_indent = len(next_line) - len(next_line.lstrip(" "))
            next_stripped = next_line.strip()
            if not next_stripped:
                continue
            if next_indent <= indent:
                break
            if next_stripped.startswith("- "):
                item = next_stripped[2:].strip().strip('"').strip("'")
                if item:
                    items.append(item)
        return tuple(items)
    return ()


def _extract_nested_frontmatter_value(frontmatter: str, parent: str, key: str) -> str:
    lines = frontmatter.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped != f"{parent}:":
            continue

        parent_indent = len(line) - len(line.lstrip(" "))
        for next_line in lines[index + 1 :]:
            next_indent = len(next_line) - len(next_line.lstrip(" "))
            next_stripped = next_line.strip()
            if not next_stripped:
                continue
            if next_indent <= parent_indent:
                break
            if next_stripped.startswith(f"{key}:"):
                value = next_stripped.split(":", 1)[1].strip()
                return value.strip('"').strip("'")
        break
    return ""


def parse_skill_markdown(text: str, *, fallback_name: str, path: str) -> SkillInfo:
    """Parse minimal skill metadata from a ``SKILL.md`` document."""
    frontmatter = ""
    if text.startswith("---\n"):
        parts = text.split("---\n", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]

    name = _extract_frontmatter_value(frontmatter, "name") or fallback_name
    description = _extract_frontmatter_value(frontmatter, "description")
    category = _extract_frontmatter_value(frontmatter, "category")
    if not category:
        category = _extract_nested_frontmatter_value(frontmatter, "metadata", "category")
    triggers = _extract_frontmatter_list(frontmatter, "triggers")
    if not description:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and stripped != "---":
                description = stripped
                break

    return SkillInfo(
        name=name,
        directory_name=fallback_name,
        description=description.strip(),
        category=category.strip(),
        triggers=triggers,
        path=path,
    )


def iter_skills_from_root(root: Path) -> list[SkillInfo]:
    """Discover skill directories under a filesystem root."""
    if not root.exists():
        return []

    skills: list[SkillInfo] = []
    for path in sorted(root.iterdir()):
        skill_file = path / "SKILL.md"
        if not path.is_dir() or not skill_file.exists():
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue
        skills.append(
            parse_skill_markdown(
                text,
                fallback_name=path.name,
                path=str(skill_file),
            )
        )
    return skills


def list_bundled_skills() -> list[SkillInfo]:
    """List bundled skills shipped with autodev."""
    root = Path(str(bundled_skills_root()))
    return iter_skills_from_root(root)


def _tokenize(text: str) -> set[str]:
    return {token for token in _WORD_RE.findall(text.lower()) if token}


def recommend_skills(skills: Iterable[SkillInfo], query: str, *, limit: int = 5) -> list[SkillInfo]:
    """Return the best matching skills for a free-form query."""
    query_text = str(query or "").strip().lower()
    if not query_text:
        return []

    query_tokens = _tokenize(query_text)
    scored: list[tuple[int, SkillInfo]] = []
    for skill in skills:
        trigger_text = " ".join(trigger.lower() for trigger in skill.triggers)
        haystack = " ".join(
            [
                skill.name.lower(),
                skill.directory_name.lower(),
                skill.description.lower(),
                skill.category.lower(),
                trigger_text,
                skill.path.lower(),
            ]
        )
        tokens = _tokenize(haystack)
        overlap = len(query_tokens & tokens)
        score = overlap * 10
        if skill.name.lower() in query_text or skill.directory_name.lower() in query_text:
            score += 25
        if query_text in haystack:
            score += 15
        if skill.category and skill.category.lower() in query_text:
            score += 12
        if trigger_text:
            trigger_tokens = _tokenize(trigger_text)
            trigger_overlap = len(query_tokens & trigger_tokens)
            score += trigger_overlap * 15
            if any(query_text in trigger.lower() for trigger in skill.triggers):
                score += 20
        if score <= 0:
            continue
        scored.append((score, skill))

    scored.sort(key=lambda item: (-item[0], item[1].directory_name))
    return [skill for _, skill in scored[:limit]]
