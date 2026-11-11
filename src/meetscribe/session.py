"""One recorded meeting: capture -> segment -> transcribe -> notes."""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from . import audio
from .config import Config
from .llm import LLM, make_llm
from .notes import NotesEngine
from .segmenter import Segmenter
from .skills import Skill, load_enabled
from .transcribe import Transcriber, make_transcriber
from .transcript import Transcript, Utterance

log = logging.getLogger("meetscribe.session")


@dataclass(order=True)
class _Job:
    priority: float
    start: float = field(compare=False)
    speaker: str = field(compare=False)
    audio: np.ndarray | None = field(compare=False)


class Session:
    def __init__(
        self,
        cfg: Config,
        *,
        title: str = "",
        skills: list[Skill] | None = None,
        transcriber: Transcriber | Callable[[], Transcriber] | None = None,
        llm: LLM | None = None,
        echo: Callable[[str], None] = print,
    ):
        """`transcriber` may be an instance or a zero-argument factory; a factory
        is called on the worker thread so capture starts before the model loads.
        `skills` overrides config; pass [] to take no notes."""
        self.cfg = cfg
        self.started = datetime.now()
        self.title = title or f"Meeting {self.started:%Y-%m-%d %H:%M}"
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        name = f"{self.started:%Y-%m-%d_%H%M%S}" + (f"_{slug}" if slug else "")
        self.dir = cfg.output_path / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        self.live = False
        self.audio_file: Path | None = None

        sp = cfg.speakers
        self.transcript = Transcript(self.dir, self.title, echo_of=(sp.me, sp.others))
        self._transcriber = transcriber or (lambda: make_transcriber(cfg.transcription))
        self._jobs: queue.PriorityQueue[_Job] = queue.PriorityQueue()
        self._worker = threading.Thread(target=self._transcribe_loop, name="transcribe", daemon=True)
        self._sources: list[audio.ParecSource] = []
        self._segmenters: list[Segmenter] = []
        self._parts: list[Path] = []
        self._t0 = time.monotonic()
        self._lag_warned = 0.0

        self.notes: NotesEngine | None = None
        if skills is None:
            skills = load_enabled(cfg.notes) if cfg.notes.enabled else []
        self.skills = skills
        if skills:
            try:
                self.notes = NotesEngine(
                    self.transcript, skills, llm or make_llm(cfg.notes), cfg.notes, self.dir,
                    title=self.title, speakers=sp,
                    on_update=lambda skill, _text: log.info("notes updated: %s", skill.name),
                )
            except Exception as exc:
                log.error("notes disabled: %s", exc)

    # ---------------------------------------------------------------- inputs

    def _segmenter(self, speaker: str) -> Segmenter:
        # The mic gets a small priority penalty so that, when both streams have
        # audio waiting, the system stream is transcribed first and mic echo of
        # it can be recognised and dropped.
        penalty = 1.5 if speaker == self.cfg.speakers.me else 0.0
        seg = Segmenter(
            lambda start, samples: self._jobs.put(_Job(start + penalty, start, speaker, samples))
        )
        self._segmenters.append(seg)
        return seg

    def start(self) -> None:
        """Start live capture from the audio server."""
        a, sp = self.cfg.audio, self.cfg.speakers
        specs: list[tuple[str, str, str]] = []
        if a.capture_mic:
            mic = a.mic_device or audio.default_source()
            if mic.endswith(".monitor"):
                log.warning("no microphone found (default input is a monitor); recording system audio only")
            else:
                specs.append((sp.me, mic, "mic"))
        if a.capture_system:
            specs.append((sp.others, a.system_device or audio.default_monitor(), "system"))
        if not specs:
            raise audio.AudioError("nothing to record: both capture_mic and capture_system are off")

        self.live = True
        self._t0 = time.monotonic()
        for speaker, device, stem in specs:
            part = self.dir / f".{stem}.wav" if a.keep_audio else None
            if part:
                self._parts.append(part)
            src = audio.ParecSource(stem, device, self._segmenter(speaker).feed, self._t0, part)
            self._sources.append(src)
            src.start()
            log.info("capturing %s from %s", speaker, device)
        self._worker.start()
        if self.notes:
            self.notes.start()

    def feed_file(self, path: Path, split_stereo: bool = False) -> None:
        """Run an existing recording through the pipeline (no live notes)."""
        sp = self.cfg.speakers
        channels = audio.decode_file(path, split_stereo)
        labels = [sp.me, sp.others] if split_stereo else ["Speaker"]
        self._worker.start()
        for label, samples in zip(labels, channels):
            audio.feed_samples(samples, self._segmenter(label).feed)

    # ------------------------------------------------------------ transcribe

    def _transcribe_loop(self) -> None:
        transcriber = self._transcriber
        if callable(transcriber) and not hasattr(transcriber, "transcribe"):
            try:
                transcriber = transcriber()
            except Exception as exc:
                log.error("transcription unavailable: %s", exc)
                if self.live and self.cfg.audio.keep_audio:
                    log.error("audio is still being recorded; run `meetscribe transcribe` on it later")
                transcriber = None
        while True:
            job = self._jobs.get()
            if job.audio is None:
                return
            if transcriber is None:
                continue
            try:
                text = transcriber.transcribe(job.audio)
            except Exception as exc:
                log.warning("transcription of one segment failed: %s", exc)
                continue
            if not text:
                continue
            utt = Utterance(job.start, job.start + len(job.audio) / audio.RATE, job.speaker, text)
            if self.transcript.add(utt):
                self.echo(utt.render())
            if self.live:
                lag = time.monotonic() - self._t0 - utt.end
                if lag > 30 and time.monotonic() - self._lag_warned > 60:
                    self._lag_warned = time.monotonic()
                    log.warning("transcription is %.0fs behind; try a smaller model", lag)

    # ------------------------------------------------------------------ stop

    def stop(self) -> Path:
        """Stop capture, finish transcribing, write final notes. Returns the session folder."""
        for src in self._sources:
            src.stop()
        for src in self._sources:
            src.join(timeout=5)
        for seg in self._segmenters:
            seg.flush()
        self._jobs.put(_Job(float("inf"), 0.0, "", None))
        if self._worker.is_alive():
            self._worker.join()
        # Audio and metadata first, so interrupting the final notes loses nothing else.
        if self._parts:
            self.audio_file = audio.merge_recordings(self._parts, self.dir / "audio", self.cfg.audio.format)
        self._write_meta()
        if self.notes:
            if len(self.transcript):
                log.info("writing final notes")
            self.notes.stop()
        return self.dir

    def _write_meta(self) -> None:
        ended = datetime.now()
        meta = {
            "title": self.title,
            "started": self.started.isoformat(timespec="seconds"),
            "ended": ended.isoformat(timespec="seconds"),
            "utterances": len(self.transcript),
            "audio": self.audio_file.name if self.audio_file else None,
            "audio_channels": [s.label for s in self._sources],
            "skills": [s.name for s in self.skills] if self.notes else [],
            "transcription": {
                "backend": self.cfg.transcription.backend,
                "model": self.cfg.transcription.model,
            },
        }
        (self.dir / "session.json").write_text(json.dumps(meta, indent=2) + "\n")
