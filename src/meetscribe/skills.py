"""Note-taking skills.

A skill is a folder containing a SKILL.md: YAML frontmatter (`name`,
`description`) followed by markdown instructions, the same shape as an Agent
Skill. Each enabled skill owns one section of the meeting notes and tells the
model what to capture there. Optional meetscribe settings go under a
`meetscribe:` key in the frontmatter (or under `metadata.meetscribe`).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import USER_SKILLS_DIR, Notes

log = logging.getLogger("meetscribe.skills")

BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin_skills"
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SkillError(ValueError):
    pass


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    path: Path
    title: str
    order: int = 100           # position of this skill's section in notes.md
    live: bool = True          # update during the meeting
    final: bool = True         # rewrite from the full transcript when it ends
    interval: float | None = None   # seconds between live updates (default: config)
    live_model: str | None = None   # model overrides (default: config)
    final_model: str | None = None
    max_tokens: int = 4000


SKILL_TEMPLATE = """\
---
name: {name}
description: One line on what this skill captures and when it is useful.
meetscribe:
  title: {title}      # heading of this skill's section in notes.md
  order: 50           # lower numbers come first
  live: true          # update during the meeting
  final: true         # rewrite from the full transcript when the meeting ends
  # interval: 60      # seconds between live updates (default: config)
  # live_model: claude-haiku-5-5
  # final_model: claude-sonnet-5-5
  # max_tokens: 4000
  # include: [glossary.md]   # other files in this folder to append to the instructions
---

# {title}

Describe what to pull out of the conversation and how the notes should look.

## What to capture

- ...

## Format

- ...
"""


def parse_skill(skill_md: Path) -> Skill:
    text = skill_md.read_text()
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not match:
        raise SkillError(f"{skill_md}: missing YAML frontmatter")
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"{skill_md}: bad frontmatter: {exc}") from exc
    if not isinstance(front, dict):
        raise SkillError(f"{skill_md}: frontmatter must be a mapping")
    name = str(front.get("name") or skill_md.parent.name)
    if not _NAME.match(name):
        raise SkillError(f"{skill_md}: name must be lowercase letters, digits and hyphens")
    opts = front.get("meetscribe") or (front.get("metadata") or {}).get("meetscribe") or {}
    if not isinstance(opts, dict):
        raise SkillError(f"{skill_md}: 'meetscribe' must be a mapping")

    instructions = match.group(2).strip()
    for extra in opts.get("include") or []:
        extra_path = (skill_md.parent / extra).resolve()
        if skill_md.parent.resolve() not in extra_path.parents or not extra_path.is_file():
            raise SkillError(f"{skill_md}: include {extra!r} not found in the skill folder")
        instructions += f"\n\n<file name=\"{extra}\">\n{extra_path.read_text().strip()}\n</file>"
    if not instructions:
        raise SkillError(f"{skill_md}: no instructions after the frontmatter")

    interval = opts.get("interval")
    return Skill(
        name=name,
        description=str(front.get("description") or "").strip(),
        instructions=instructions,
        path=skill_md.parent,
        title=str(opts.get("title") or name.replace("-", " ").capitalize()),
        order=int(opts.get("order", 100)),
        live=bool(opts.get("live", True)),
        final=bool(opts.get("final", True)),
        interval=float(interval) if interval is not None else None,
        live_model=opts.get("live_model"),
        final_model=opts.get("final_model"),
        max_tokens=int(opts.get("max_tokens", 4000)),
    )


def skill_dirs(cfg: Notes) -> list[Path]:
    return [BUILTIN_SKILLS_DIR, USER_SKILLS_DIR, *(Path(d).expanduser() for d in cfg.skills_dirs)]


def discover(dirs: list[Path]) -> dict[str, Skill]:
    """Find skills in `dirs`. A later directory overrides an earlier one by name."""
    found: dict[str, Skill] = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for skill_md in sorted(directory.glob("*/SKILL.md")):
            try:
                skill = parse_skill(skill_md)
            except SkillError as exc:
                log.warning("skipping skill: %s", exc)
                continue
            found[skill.name] = skill
    return found


def load_enabled(cfg: Notes, only: list[str] | None = None) -> list[Skill]:
    """The skills to run: `only` if given, else config, else everything found."""
    found = discover(skill_dirs(cfg))
    wanted = only or cfg.skills
    if wanted:
        missing = [name for name in wanted if name not in found]
        if missing:
            raise SkillError(f"unknown skill(s): {', '.join(missing)} (have: {', '.join(sorted(found))})")
        chosen = [found[name] for name in wanted]
    else:
        chosen = [s for s in found.values() if s.name not in cfg.disabled_skills]
    return sorted(chosen, key=lambda s: (s.order, s.name))
