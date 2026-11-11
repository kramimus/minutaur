"""Background mode: start recording when a browser call begins, stop when it ends."""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import Callable

from . import audio
from .config import Config
from .session import Session
from .transcribe import Transcriber, make_transcriber

log = logging.getLogger("meetscribe.watch")


def notify(summary: str, body: str = "") -> None:
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-a", "meetscribe", "-i", "audio-input-microphone", summary, body],
            capture_output=True,
        )


class SharedTranscriber:
    """Loads the speech model on first use and keeps it for later meetings."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._model: Transcriber | None = None
        self._lock = threading.Lock()

    def __call__(self) -> Transcriber:
        with self._lock:
            if self._model is None:
                self._model = make_transcriber(self._cfg.transcription)
            return self._model


def watch(
    cfg: Config,
    *,
    make_session: Callable[[], Session] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Poll the audio server until interrupted (or `should_stop()` is true)."""
    w = cfg.watch
    transcriber = SharedTranscriber(cfg)
    make_session = make_session or (lambda: Session(cfg, transcriber=transcriber, echo=lambda _line: None))
    session: Session | None = None
    seen_since: float | None = None
    gone_since: float | None = None
    log.info("watching for browser calls (%s)", ", ".join(w.browsers))
    try:
        while not should_stop():
            try:
                in_call = bool(audio.browser_mic_streams(w.browsers))
            except audio.AudioError as exc:
                log.warning("%s", exc)
                in_call = session is not None  # don't cut a recording short on a hiccup
            now = time.monotonic()
            if session is None:
                seen_since = (seen_since or now) if in_call else None
                if seen_since is not None and now - seen_since >= w.start_after:
                    try:
                        session = make_session()
                        session.start()
                    except Exception as exc:
                        log.error("could not start recording: %s", exc)
                        session = None
                    else:
                        log.info("call detected, recording to %s", session.dir)
                        notify("meetscribe is recording", f"Live notes: {session.dir / 'notes.md'}")
                    seen_since = gone_since = None
            else:
                gone_since = None if in_call else (gone_since or now)
                if gone_since is not None and now - gone_since >= w.stop_after:
                    _finish(session)
                    session = None
                    gone_since = None
            time.sleep(w.poll_seconds)
    finally:
        if session is not None:
            _finish(session)


def _finish(session: Session) -> None:
    log.info("call ended, finishing up")
    path = session.stop()
    log.info("saved %s", path)
    notify("meetscribe saved your meeting", str(path))
