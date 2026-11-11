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
from .llm import Claude
from .notes import NotesEngine
from .segmenter import Segmenter
from .skills import Skill, load_enabled
from .transcribe import Transcriber
from .transcript import Transcript, Utterance

log = logging.getLogger("minutaur.session")


@dataclass(order=True)
class _Job:
    priority: float
    start: float = field(compare=False)
    speaker: str = field(compare=False)
    audio: np.ndarray | None = field(compare=False)  # None = stop


class Session:
    def __init__(
        self,
        cfg: Config,
        *,
        title: str = "",
        skills: list[Skill] | None = None,
        transcriber: Callable[[], Transcriber] | None = None,
        llm: Claude | None = None,
        echo: Callable[[str], None] = print,
    ):
        """`transcriber` is a factory, called on the worker thread so that capture
        starts while the speech model loads. `skills=[]` takes no notes;
        `skills=None` uses the configured set."""
        self.cfg = cfg
        self.started = datetime.now()
        self.title = title or f"Meeting {self.started:%Y-%m-%d %H:%M}"
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        self.dir = cfg.output_path / (f"{self.started:%Y-%m-%d_%H%M%S}" + (f"_{slug}" if slug else ""))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        self.live = False
        self.audio_file: Path | None = None

        sp = cfg.speakers
        self.transcript = Transcript(self.dir, self.title, echo_of=(sp.me, sp.others))
        self._make_transcriber = transcriber or (lambda: Transcriber(cfg.transcription))
        self._jobs: queue.PriorityQueue[_Job] = queue.PriorityQueue()
        self._worker = threading.Thread(target=self._transcribe_loop, name="transcribe", daemon=True)
        self._sources: list[audio.ParecSource] = []
        self._segmenters: list[Segmenter] = []
        self._parts: list[Path] = []
        self._t0 = time.monotonic()
        self._lag_warned = 0.0

        if skills is None:
            skills = load_enabled(cfg.notes) if cfg.notes.enabled else []
        self.skills = skills
        self.notes: NotesEngine | None = None
        if skills:
            try:
                self.notes = NotesEngine(
                    self.transcript, skills, llm or Claude(cfg.notes), cfg.notes, self.dir,
                    title=self.title, speakers=sp,
                    on_update=lambda skill, _text: log.info("notes updated: %s", skill.name),
                )
            except Exception as exc:
                log.error("notes disabled: %s", exc)

    # ---------------------------------------------------------------- inputs

    def _segmenter(self, speaker: str) -> Segmenter:
        # Mic audio waits slightly longer in the queue, so when both streams have
        # speech pending the system stream is transcribed first and the mic
        # picking up the speakers can be recognised as echo and dropped.
        penalty = 1.5 if speaker == self.cfg.speakers.me else 0.0
        seg = Segmenter(lambda start, samples: self._jobs.put(_Job(start + penalty, start, speaker, samples)))
        self._segmenters.append(seg)
        return seg

    def start(self) -> None:
        """Start capturing from the audio server."""
        a, sp = self.cfg.audio, self.cfg.speakers
        specs = []
        if a.capture_mic:
            mic = a.mic_device or audio.default_source()
            if mic.endswith(".monitor"):
                log.warning("no microphone (the default input is a monitor); recording system audio only")
            else:
                specs.append((sp.me, mic, "mic"))
        if a.capture_system:
            specs.append((sp.others, a.system_device or audio.default_monitor(), "system"))
        if not specs:
            raise audio.AudioError("nothing to record: capture_mic and capture_system are both off")

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
        """Run an existing recording through the pipeline (notes at the end only)."""
        sp = self.cfg.speakers
        labels = [sp.me, sp.others] if split_stereo else ["Speaker"]
        self._worker.start()
        for label, samples in zip(labels, audio.decode_file(path, split_stereo)):
            audio.feed_samples(samples, self._segmenter(label).feed)

    # ------------------------------------------------------------ transcribe

    def _transcribe_loop(self) -> None:
        try:
            transcriber = self._make_transcriber()
        except Exception as exc:
            log.error("transcription unavailable: %s", exc)
            if self.live and self.cfg.audio.keep_audio:
                log.error("audio is still being recorded; run `minutaur transcribe` on it later")
            transcriber = None
        while (job := self._jobs.get()).audio is not None:
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
            lag = time.monotonic() - self._t0 - utt.end
            if self.live and lag > 30 and time.monotonic() - self._lag_warned > 60:
                self._lag_warned = time.monotonic()
                log.warning("transcription is %.0fs behind; try a smaller model", lag)

    # ------------------------------------------------------------------ stop

    def stop(self) -> Path:
        """Stop capture, finish transcribing, write the final notes. Returns the folder."""
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
        meta = {
            "title": self.title,
            "started": self.started.isoformat(timespec="seconds"),
            "ended": datetime.now().isoformat(timespec="seconds"),
            "utterances": len(self.transcript),
            "audio": self.audio_file.name if self.audio_file else None,
            "audio_channels": [s.label for s in self._sources],
            "skills": [s.name for s in self.skills] if self.notes else [],
            "whisper_model": self.cfg.transcription.model,
        }
        (self.dir / "session.json").write_text(json.dumps(meta, indent=2) + "\n")
