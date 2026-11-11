"""meetscribe command line."""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from . import __version__, audio, config as config_mod, skills as skills_mod
from .config import Config, load_config, load_env_file

log = logging.getLogger("meetscribe")


def _skill_list(value: str | None) -> list[str] | None:
    return [s.strip() for s in value.split(",") if s.strip()] if value else None


def _session(cfg: Config, args, **kwargs):
    from .session import Session

    skills = None
    if getattr(args, "no_notes", False):
        skills = []
    elif getattr(args, "skills", None):
        skills = skills_mod.load_enabled(cfg.notes, _skill_list(args.skills))
    return Session(cfg, title=getattr(args, "title", "") or "", skills=skills, **kwargs)


def _report(session) -> None:
    print(f"\nSaved to {session.dir}")
    for name in ("notes.md", "transcript.md"):
        if (session.dir / name).exists():
            print(f"  {name}")
    if session.audio_file:
        print(f"  {session.audio_file.name}")


# ------------------------------------------------------------------ commands


def cmd_record(cfg: Config, args) -> int:
    if args.no_mic:
        cfg.audio.capture_mic = False
    session = _session(cfg, args)
    session.start()
    print(f"Recording. Live notes: {session.dir / 'notes.md'}", file=sys.stderr)
    print("Press Ctrl-C to stop.\n", file=sys.stderr)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping (Ctrl-C again to abandon the final notes)...", file=sys.stderr)
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
        print(f"no such file: {path}", file=sys.stderr)
        return 1
    if not args.title:
        args.title = path.stem
    session = _session(cfg, args)
    session.feed_file(path, split_stereo=args.split_stereo)
    session.stop()
    _report(session)
    return 0


def cmd_notes(cfg: Config, args) -> int:
    """Rebuild the notes of a past meeting with the skills enabled now."""
    import json

    from .llm import make_llm
    from .notes import NotesEngine
    from .transcript import Transcript

    session_dir = Path(args.session).expanduser()
    transcript = Transcript.load(session_dir)
    if not len(transcript):
        print(f"no transcript found in {session_dir}", file=sys.stderr)
        return 1
    title = session_dir.name
    meta = session_dir / "session.json"
    if meta.exists():
        title = json.loads(meta.read_text()).get("title", title)
    skills = skills_mod.load_enabled(cfg.notes, _skill_list(args.skills))
    engine = NotesEngine(
        transcript, skills, make_llm(cfg.notes), cfg.notes, session_dir,
        title=title, speakers=cfg.speakers,
        on_update=lambda skill, _text: print(f"  {skill.name}"),
    )
    print(f"Rebuilding notes with: {', '.join(s.name for s in skills)}")
    engine.finalize()
    print(f"Wrote {session_dir / 'notes.md'}")
    return 0


def cmd_skills(cfg: Config, args) -> int:
    if args.action == "new":
        if not args.name or not skills_mod._NAME.match(args.name):
            print("usage: meetscribe skills new <lowercase-name>", file=sys.stderr)
            return 1
        target = config_mod.USER_SKILLS_DIR / args.name
        if target.exists():
            print(f"{target} already exists", file=sys.stderr)
            return 1
        target.mkdir(parents=True)
        title = args.name.replace("-", " ").capitalize()
        (target / "SKILL.md").write_text(skills_mod.SKILL_TEMPLATE.format(name=args.name, title=title))
        print(f"Created {target / 'SKILL.md'} (edit it; it is picked up at the next meeting)")
        return 0

    enabled = {s.name for s in skills_mod.load_enabled(cfg.notes)}
    found = skills_mod.discover(skills_mod.skill_dirs(cfg.notes))
    print("Skill directories:")
    for directory in skills_mod.skill_dirs(cfg.notes):
        print(f"  {directory}")
    print()
    for skill in sorted(found.values(), key=lambda s: (s.order, s.name)):
        mode = "+".join(m for m, on in (("live", skill.live), ("final", skill.final)) if on) or "off"
        state = "enabled " if skill.name in enabled else "disabled"
        print(f"  {state}  {skill.name:<22} {mode:<11} {skill.description}")
    return 0


def cmd_doctor(cfg: Config, args) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "", required: bool = True) -> None:
        nonlocal ok
        mark = "ok  " if good else ("FAIL" if required else "warn")
        ok = ok and (good or not required)
        print(f"  [{mark}] {label}" + (f": {detail}" if detail else ""))

    print(f"meetscribe {__version__}   config: {config_mod.CONFIG_PATH}")
    for tool in ("pactl", "parec"):
        check(tool, bool(shutil.which(tool)), "" if shutil.which(tool) else "sudo apt install pulseaudio-utils")
    try:
        check("system audio", True, audio.default_monitor())
    except audio.AudioError as exc:
        check("system audio", False, str(exc))
    try:
        source = audio.default_source()
        check("microphone", not source.endswith(".monitor"), source, required=False)
    except audio.AudioError as exc:
        check("microphone", False, str(exc), required=False)
    check("ffmpeg", bool(shutil.which("ffmpeg")), "" if shutil.which("ffmpeg") else "needed for flac/opus and non-WAV input", required=False)
    check("notify-send", bool(shutil.which("notify-send")), "" if shutil.which("notify-send") else "sudo apt install libnotify-bin", required=False)

    t = cfg.transcription
    if t.backend == "faster-whisper":
        try:
            import faster_whisper  # noqa: F401

            check("transcription", True, f"faster-whisper, model {t.model} (downloaded on first use)")
        except ImportError as exc:
            check("transcription", False, str(exc))
    else:
        check("transcription", bool(os.environ.get(t.api_key_env)) or "localhost" in t.api_base,
              f"{t.api_base} ({t.api_key_env})")

    n = cfg.notes
    if n.enabled:
        if n.provider == "bedrock":
            from .llm import bedrock_status

            good, detail = bedrock_status(n)
            check("notes via Bedrock", good, detail)
        else:
            key_env = n.api_key_env or ("ANTHROPIC_API_KEY" if n.provider == "anthropic" else "OPENAI_API_KEY")
            has_key = bool(os.environ.get(key_env)) or (n.provider == "openai" and bool(n.api_base))
            check("notes API key", has_key, f"{key_env} " + ("set" if os.environ.get(key_env) else "not set"))
        try:
            names = [s.name for s in skills_mod.load_enabled(n)]
            check("skills", bool(names), ", ".join(names) or "none enabled")
        except skills_mod.SkillError as exc:
            check("skills", False, str(exc))
    else:
        check("notes", True, "disabled in config")
    check("output folder", True, str(cfg.output_path))
    return 0 if ok else 1


# ---------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meetscribe",
        description="Record and transcribe browser meetings and take live notes with pluggable skills.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help=f"config file (default {config_mod.CONFIG_PATH})")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    def notes_flags(p):
        p.add_argument("--title", default="", help="meeting title (used in the folder name and notes)")
        p.add_argument("--skills", help="comma-separated skills to run instead of the configured set")
        p.add_argument("--no-notes", action="store_true", help="transcribe only")

    p = sub.add_parser("record", help="record now, until Ctrl-C")
    notes_flags(p)
    p.add_argument("--no-mic", action="store_true", help="record only what you hear")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("watch", help="record automatically whenever a browser call is in progress")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("transcribe", help="transcribe an existing recording and take notes on it")
    p.add_argument("file")
    notes_flags(p)
    p.add_argument("--split-stereo", action="store_true",
                   help="treat left/right as you/others (meetscribe recordings are laid out this way)")
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("notes", help="rebuild a past meeting's notes with the current skills")
    p.add_argument("session", help="session folder (contains transcript.jsonl)")
    p.add_argument("--skills", help="comma-separated skills to run instead of the configured set")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("skills", help="list skills, or scaffold a new one")
    p.add_argument("action", nargs="?", choices=["list", "new"], default="list")
    p.add_argument("name", nargs="?")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("doctor", help="check that everything meetscribe needs is in place")
    p.set_defaults(func=cmd_doctor)
    return parser


def _sigterm(_signum, _frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    for noisy in ("httpx", "httpcore", "anthropic", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, _sigterm)  # systemd stop = clean shutdown
    load_env_file()
    try:
        cfg = load_config(args.config)
        return args.func(cfg, args)
    except (audio.AudioError, skills_mod.SkillError, ValueError) as exc:
        print(f"meetscribe: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
