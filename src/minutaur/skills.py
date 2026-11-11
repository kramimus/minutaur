"""Note-taking skills.

A skill is a folder holding a SKILL.md: YAML frontmatter (`name`,
`description`) and then markdown instructions, the same shape as an Agent
Skill. Each enabled skill owns one section of the meeting notes. Optional
minutaur settings go under an `minutaur:` key in the frontmatter.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import USER_SKILLS_DIR, Notes

log = logging.getLogger("minutaur.skills")

BUILTIN_SKILLS_DIR = Path(__file__).parent / "builtin_skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SkillError(ValueError):
    pass


@dataclass
class Skill:
    name: str
    description: str
    instructions: str
    title: str
    order: int = 100                 # position of this skill's section in notes.md
    live: bool = True                # update during the meeting
    final: bool = True               # rewrite from the full transcript at the end
    interval: float | None = None    # seconds between live updates (default: config)
    live_model: str | None = None    # model overrides (default: config)
    final_model: str | None = None
    max_tokens: int = 4000


SKILL_TEMPLATE = """\
---
name: {name}
description: One line on what this skill captures and when it is useful.
minutaur:
  title: {title}      # heading of this skill's section in notes.md
  order: 50           # lower numbers come first
  live: true          # update during the meeting
  final: true         # rewrite from the full transcript when the meeting ends
  # interval: 60      # seconds between live updates (default: config)
  # live_model: anthropic.claude-haiku-5-5
  # final_model: anthropic.claude-sonnet-5-5
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
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", skill_md.read_text(), re.S)
    if not match:
        raise SkillError(f"{skill_md}: missing YAML frontmatter")
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"{skill_md}: bad frontmatter: {exc}") from exc
    name = str(front.get("name") or skill_md.parent.name)
    if not NAME_RE.match(name):
        raise SkillError(f"{skill_md}: name must be lowercase letters, digits and hyphens")
    opts = front.get("minutaur") or {}

    instructions = match.group(2).strip()
    folder = skill_md.parent.resolve()
    for extra in opts.get("include") or []:
        extra_path = (folder / extra).resolve()
        if folder not in extra_path.parents or not extra_path.is_file():
            raise SkillError(f"{skill_md}: include {extra!r} not found in the skill folder")
        instructions += f'\n\n<file name="{extra}">\n{extra_path.read_text().strip()}\n</file>'
    if not instructions:
        raise SkillError(f"{skill_md}: no instructions after the frontmatter")

    return Skill(
        name=name,
        description=str(front.get("description") or "").strip(),
        instructions=instructions,
        title=str(opts.get("title") or name.replace("-", " ").capitalize()),
        order=int(opts.get("order", 100)),
        live=bool(opts.get("live", True)),
        final=bool(opts.get("final", True)),
        interval=float(opts["interval"]) if "interval" in opts else None,
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
        for skill_md in sorted(directory.glob("*/SKILL.md")) if directory.is_dir() else []:
            try:
                skill = parse_skill(skill_md)
            except SkillError as exc:
                log.warning("skipping skill: %s", exc)
            else:
                found[skill.name] = skill
    return found


def load_enabled(cfg: Notes, only: list[str] | None = None) -> list[Skill]:
    """The skills to run: `only` if given, else the config's list, else all found."""
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
