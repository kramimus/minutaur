"""Background mode: record whenever a browser call is in progress."""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable

from . import audio
from .config import Config
from .session import Session
from .transcribe import Transcriber

log = logging.getLogger("minutaur.watch")


def notify(summary: str, body: str = "") -> None:
    try:
        subprocess.run(["notify-send", "-a", "minutaur", "-i", "audio-input-microphone", summary, body],
                       capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def shared_transcriber(cfg: Config) -> Callable[[], Transcriber]:
    """A factory that loads the speech model once and reuses it for later meetings."""
    model: list[Transcriber] = []
    lock = threading.Lock()

    def get() -> Transcriber:
        with lock:
            if not model:
                model.append(Transcriber(cfg.transcription))
            return model[0]

    return get


def watch(cfg: Config, *, make_session: Callable[[], Session] | None = None,
          should_stop: Callable[[], bool] = lambda: False) -> None:
    """Poll the audio server until interrupted (or until `should_stop()`)."""
    w = cfg.watch
    if make_session is None:
        transcriber = shared_transcriber(cfg)
        make_session = lambda: Session(cfg, transcriber=transcriber, echo=lambda _line: None)  # noqa: E731
    session: Session | None = None
    since: float | None = None  # when the call started (or ended) being seen
    log.info("watching for browser calls (%s)", ", ".join(w.browsers))
    try:
        while not should_stop():
            try:
                in_call = bool(audio.browser_mic_streams(w.browsers))
            except audio.AudioError as exc:
                log.warning("%s", exc)
                in_call = session is not None  # don't cut a recording short on a hiccup
            now = time.monotonic()
            if in_call == (session is not None):  # nothing changing
                since = None
            elif since is None:
                since = now
            elif session is None and now - since >= w.start_after:
                since = None
                try:
                    session = make_session()
                    session.start()
                except Exception as exc:
                    log.error("could not start recording: %s", exc)
                    session = None
                else:
                    log.info("call detected, recording to %s", session.dir)
                    notify("minutaur is recording", f"Live notes: {session.dir / 'notes.md'}")
            elif session is not None and now - since >= w.stop_after:
                since = None
                _finish(session)
                session = None
            time.sleep(w.poll_seconds)
    finally:
        if session is not None:
            _finish(session)


def _finish(session: Session) -> None:
    log.info("call ended, finishing up")
    path = session.stop()
    log.info("saved %s", path)
    notify("minutaur saved your meeting", str(path))
