"""minutaur command line."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import signal
import sys
import time
from pathlib import Path

from . import __version__, audio, config as config_mod, llm, skills as skills_mod
from .config import Config, load_config, load_env_file


def _skill_names(value: str | None) -> list[str] | None:
    return [s.strip() for s in value.split(",") if s.strip()] if value else None


def _session(cfg: Config, args):
    from .session import Session

    skills = None
    if args.no_notes:
        skills = []
    elif args.skills:
        skills = skills_mod.load_enabled(cfg.notes, _skill_names(args.skills))
    return Session(cfg, title=args.title, skills=skills)


def _report(session) -> None:
    print(f"\nSaved to {session.dir}")
    for path in (session.dir / "notes.md", session.dir / "transcript.md", session.audio_file):
        if path and path.exists():
            print(f"  {path.name}")


# -------------------------------------------------------------------- commands


def cmd_record(cfg: Config, args) -> int:
    if args.no_mic:
        cfg.audio.capture_mic = False
    session = _session(cfg, args)
    session.start()
    print(f"Recording. Live notes: {session.dir / 'notes.md'}\nPress Ctrl-C to stop.\n", file=sys.stderr)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping (Ctrl-C again skips the final notes)...", file=sys.stderr)
    session.stop()
    _report(session)
    return 0


def cmd_watch(cfg: Config, args) -> int:
    from .watch import watch

    try:
        watch(cfg)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_transcribe(cfg: Config, args) -> int:
    path = Path(args.file).expanduser()
    if not path.exists():
        raise ValueError(f"no such file: {path}")
    args.title = args.title or path.stem
    session = _session(cfg, args)
    session.feed_file(path, split_stereo=args.split_stereo)
    session.stop()
    _report(session)
    return 0


def cmd_notes(cfg: Config, args) -> int:
    """Rebuild a past meeting's notes with the skills enabled now."""
    from .notes import NotesEngine
    from .transcript import Transcript

    folder = Path(args.session).expanduser()
    transcript = Transcript.load(folder)
    if not len(transcript):
        raise ValueError(f"no transcript found in {folder}")
    meta = folder / "session.json"
    title = json.loads(meta.read_text()).get("title", folder.name) if meta.exists() else folder.name
    skills = skills_mod.load_enabled(cfg.notes, _skill_names(args.skills))
    engine = NotesEngine(transcript, skills, llm.Claude(cfg.notes), cfg.notes, folder, title=title,
                         speakers=cfg.speakers, on_update=lambda skill, _text: print(f"  {skill.name}"))
    print(f"Rebuilding notes with: {', '.join(s.name for s in skills)}")
    engine.finalize()
    print(f"Wrote {folder / 'notes.md'}")
    return 0


def cmd_skills(cfg: Config, args) -> int:
    if args.action == "new":
        if not args.name or not skills_mod.NAME_RE.match(args.name):
            raise ValueError("usage: minutaur skills new <lowercase-name>")
        target = config_mod.USER_SKILLS_DIR / args.name
        target.mkdir(parents=True)  # fails if it already exists
        title = args.name.replace("-", " ").capitalize()
        (target / "SKILL.md").write_text(skills_mod.SKILL_TEMPLATE.format(name=args.name, title=title))
        print(f"Created {target / 'SKILL.md'}; it is used from the next meeting.")
        return 0

    enabled = {s.name for s in skills_mod.load_enabled(cfg.notes)}
    print("Skill folders:", *(f"  {d}" for d in skills_mod.skill_dirs(cfg.notes)), "", sep="\n")
    found = skills_mod.discover(skills_mod.skill_dirs(cfg.notes))
    for skill in sorted(found.values(), key=lambda s: (s.order, s.name)):
        mode = "+".join(m for m, on in (("live", skill.live), ("final", skill.final)) if on) or "off"
        state = "enabled " if skill.name in enabled else "disabled"
        print(f"  {state}  {skill.name:<22} {mode:<11} {skill.description}")
    return 0


def cmd_doctor(cfg: Config, args) -> int:
    failed = False

    def check(label: str, ok: bool, detail: str, required: bool = True) -> None:
        nonlocal failed
        failed |= required and not ok
        print(f"  [{'ok  ' if ok else 'FAIL' if required else 'warn'}] {label}: {detail}")

    print(f"minutaur {__version__}   config: {config_mod.CONFIG_PATH}")
    for tool, package in (("parec", "pulseaudio-utils"), ("ffmpeg", "ffmpeg")):
        check(tool, bool(shutil.which(tool)), "found" if shutil.which(tool) else f"sudo apt install {package}")
    for label, find, required in (("system audio", audio.default_monitor, True),
                                  ("microphone", audio.default_source, False)):
        try:
            device = find()
            check(label, not (label == "microphone" and device.endswith(".monitor")), device, required)
        except audio.AudioError as exc:
            check(label, False, str(exc), required)
    try:
        import faster_whisper  # noqa: F401

        check("transcription", True, f"Whisper {cfg.transcription.model} (downloaded on first use)")
    except ImportError as exc:
        check("transcription", False, str(exc))
    if cfg.notes.enabled:
        check("notes model", *llm.check(cfg.notes))
        try:
            names = [s.name for s in skills_mod.load_enabled(cfg.notes)]
            check("skills", bool(names), ", ".join(names) or "none enabled")
        except skills_mod.SkillError as exc:
            check("skills", False, str(exc))
    print(f"  [info] meetings go to {cfg.output_path}")
    return 1 if failed else 0


# ------------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minutaur",
                                     description="Record and transcribe meetings, with live notes from pluggable skills.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help=f"config file (default {config_mod.CONFIG_PATH})")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.set_defaults(func=func)
        return p

    def notes_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--title", default="", help="meeting title (used in the folder name and notes)")
        p.add_argument("--skills", help="comma-separated skills to run instead of the configured set")
        p.add_argument("--no-notes", action="store_true", help="transcribe only")

    p = add("record", cmd_record, "record now, until Ctrl-C")
    notes_flags(p)
    p.add_argument("--no-mic", action="store_true", help="record only what you hear")

    add("watch", cmd_watch, "record automatically whenever a browser call is in progress")

    p = add("transcribe", cmd_transcribe, "transcribe an existing recording and take notes on it")
    p.add_argument("file")
    notes_flags(p)
    p.add_argument("--split-stereo", action="store_true",
                   help="left channel is you, right is everyone else (how minutaur records)")

    p = add("notes", cmd_notes, "rebuild a past meeting's notes with the current skills")
    p.add_argument("session", help="a meeting folder (contains transcript.jsonl)")
    p.add_argument("--skills", help="comma-separated skills to run instead of the configured set")

    p = add("skills", cmd_skills, "list skills, or scaffold a new one")
    p.add_argument("action", nargs="?", choices=["list", "new"], default="list")
    p.add_argument("name", nargs="?")

    add("doctor", cmd_doctor, "check that everything minutaur needs is in place")
    return parser


def _sigterm(_signum, _frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stderr)
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "botocore", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, _sigterm)  # systemd stop = clean shutdown with final notes
    load_env_file()
    try:
        return args.func(load_config(args.config), args)
    except (audio.AudioError, skills_mod.SkillError, llm.LLMError, ValueError, FileExistsError) as exc:
        print(f"minutaur: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
