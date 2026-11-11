"""The running transcript: in memory for the notes engine, on disk for you."""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Replace a file in one step so an open editor never sees half of it."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


@dataclass
class Utterance:
    start: float  # seconds since the session started
    end: float
    speaker: str
    text: str

    def render(self) -> str:
        return f"[{clock(self.start)}] {self.speaker}: {self.text}"


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


class Transcript:
    def __init__(self, session_dir: Path | None = None, title: str = "", echo_of: tuple[str, str] | None = None):
        """`echo_of=(mic_label, system_label)` enables dropping mic lines that
        are just the speakers being picked up by the microphone."""
        self._lock = threading.Lock()
        self._utterances: list[Utterance] = []
        self._echo_of = echo_of
        self._jsonl = self._md = None
        if session_dir:
            self._jsonl = session_dir / "transcript.jsonl"
            self._md = session_dir / "transcript.md"
            if not self._md.exists():
                self._md.write_text(f"# Transcript: {title or session_dir.name}\n\n")

    @classmethod
    def load(cls, session_dir: Path) -> "Transcript":
        transcript = cls()
        path = session_dir / "transcript.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    transcript._utterances.append(Utterance(**json.loads(line)))
        return transcript

    def __len__(self) -> int:
        with self._lock:
            return len(self._utterances)

    def snapshot(self) -> list[Utterance]:
        with self._lock:
            return list(self._utterances)

    def add(self, utt: Utterance) -> bool:
        """Append an utterance. Returns False if it was dropped as speaker echo."""
        with self._lock:
            if self._is_echo(utt):
                return False
            self._utterances.append(utt)
            if self._jsonl:
                with self._jsonl.open("a") as fh:
                    fh.write(json.dumps(asdict(utt)) + "\n")
                with self._md.open("a") as fh:
                    fh.write(f"**[{clock(utt.start)}] {utt.speaker}:** {utt.text}\n\n")
        return True

    def _is_echo(self, utt: Utterance) -> bool:
        if not self._echo_of or utt.speaker != self._echo_of[0]:
            return False
        mine = _words(utt.text)
        if len(mine) < 3:
            return False
        theirs: list[str] = []
        for other in self._utterances[-40:]:
            if other.speaker == self._echo_of[1] and other.start < utt.end + 2 and other.end > utt.start - 2:
                theirs += _words(other.text)
        if not theirs:
            return False
        matched = sum(b.size for b in SequenceMatcher(None, mine, theirs, autojunk=False).get_matching_blocks())
        return matched / len(mine) >= 0.6


def render(utterances: list[Utterance]) -> str:
    return "\n".join(u.render() for u in utterances)
