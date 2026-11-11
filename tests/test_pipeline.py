"""End-to-end runs with a scripted transcriber and LLM.

The live tests put fake `pactl` and `parec` programs on PATH: the fake parec
streams prepared PCM in real time, exactly as the real one would.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import wave

import pytest

from meetscribe import skills as skills_mod
from meetscribe.audio import RATE
from meetscribe.config import Notes
from meetscribe.session import Session
from meetscribe.watch import watch

from conftest import FakeLLM, FakeTranscriber, burst_signal, write_wav

FAKE_PAREC = f"""#!{sys.executable}
import os, sys, time
device = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--device="))
path = os.path.join(os.environ["FAKE_AUDIO_DIR"], device + ".raw")
data = open(path, "rb").read() if os.path.exists(path) else b""
frame, out, start, i = 3200, sys.stdout.buffer, time.monotonic(), 0
while True:
    chunk = data[i * frame:(i + 1) * frame].ljust(frame, b"\\0")
    out.write(chunk); out.flush()
    i += 1
    time.sleep(max(0, start + i * 0.1 - time.monotonic()))
"""

FAKE_PACTL = """#!/bin/sh
case "$*" in
  get-default-sink) echo fake_sink ;;
  get-default-source) echo fake_mic ;;
  "list source-outputs") cat "$FAKE_AUDIO_DIR/source-outputs.txt" 2>/dev/null ;;
  *) echo "unsupported: $*" >&2; exit 1 ;;
esac
"""

CHROME_ON_CALL = 'Source Output #1\n\tCorked: no\n\tProperties:\n\t\tapplication.process.binary = "chrome"\n'


@pytest.fixture
def fake_audio(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("parec", FAKE_PAREC), ("pactl", FAKE_PACTL)):
        script = bindir / name
        script.write_text(body)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    data = tmp_path / "audio"
    data.mkdir()
    # Others speak at 0.5-2.0s and 3.0-4.0s; the user speaks at 2.2-3.0s.
    (data / "fake_sink.monitor.raw").write_bytes(
        burst_signal([(0.5, False), (1.5, True), (1.0, False), (1.0, True), (1.0, False)]).tobytes())
    (data / "fake_mic.raw").write_bytes(
        burst_signal([(2.2, False), (0.8, True), (2.0, False)], seed=1).tobytes())
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_AUDIO_DIR", str(data))
    return data


def builtin_skills():
    return skills_mod.load_enabled(Notes())


def test_file_to_transcript_and_notes(cfg, tmp_path):
    src = write_wav(tmp_path / "call.wav", burst_signal(
        [(1.0, False), (2.0, True), (1.5, False), (3.0, True), (1.0, False)]))
    llm = FakeLLM()
    printed = []
    session = Session(cfg, title="Design review", skills=builtin_skills(),
                      transcriber=FakeTranscriber(["We should ship on Friday.", "I'll write the doc."]),
                      llm=llm, echo=printed.append)
    session.feed_file(src)
    out = session.stop()

    assert out.name.endswith("_design-review")
    assert printed == ["[00:00:00] Speaker: We should ship on Friday.", "[00:00:04] Speaker: I'll write the doc."]
    lines = [json.loads(l) for l in (out / "transcript.jsonl").read_text().splitlines()]
    assert [l["text"] for l in lines] == ["We should ship on Friday.", "I'll write the doc."]
    assert lines[1]["start"] == pytest.approx(4.2, abs=0.11)

    # No live updates for a file: one final call per skill, on the final model.
    assert len(llm.calls) == 2 and {c["model"] for c in llm.calls} == {"anthropic.claude-sonnet-5-5"}
    notes = (out / "notes.md").read_text()
    assert notes.startswith("# Design review\n") and "## Summary" in notes and "- note v" in notes
    meta = json.loads((out / "session.json").read_text())
    assert meta["utterances"] == 2 and meta["skills"] == ["meeting-notes", "action-items"]


def test_split_stereo_labels_speakers(cfg, tmp_path):
    import numpy as np

    left = burst_signal([(0.5, False), (1.0, True), (3.0, False)])
    right = burst_signal([(2.5, False), (1.0, True), (1.0, False)], seed=2)
    stereo = np.stack([left, right], axis=1).reshape(-1)
    src = write_wav(tmp_path / "audio.wav", stereo, channels=2)
    cfg.speakers.me = "Mark"
    session = Session(cfg, skills=[], transcriber=FakeTranscriber(["mine", "theirs"]), echo=lambda _l: None)
    session.feed_file(src, split_stereo=True)
    out = session.stop()
    assert [(u.speaker, u.text) for u in session.transcript.snapshot()] == [("Mark", "mine"), ("Others", "theirs")]
    assert not (out / "notes.md").exists()


def test_transcriber_failure_keeps_session_alive(cfg, tmp_path):
    src = write_wav(tmp_path / "call.wav", burst_signal([(0.5, False), (1.0, True), (1.0, False)]))

    def broken():
        raise RuntimeError("model missing")

    session = Session(cfg, skills=[], transcriber=broken)
    session.feed_file(src)
    out = session.stop()
    assert json.loads((out / "session.json").read_text())["utterances"] == 0


def test_live_capture(cfg, fake_audio):
    cfg.notes.interval = 1
    llm = FakeLLM()
    printed = []
    session = Session(cfg, skills=builtin_skills(), llm=llm, echo=printed.append,
                      transcriber=lambda: FakeTranscriber(["others one", "me one", "others two"]))
    session.start()
    time.sleep(5.0)
    live_calls = len(llm.calls)
    out = session.stop()

    got = [(u.speaker, u.text, round(u.start, 1)) for u in session.transcript.snapshot()]
    assert [(s, t) for s, t, _ in got] == [("Others", "others one"), ("Me", "me one"), ("Others", "others two")]
    for (_, _, start), expected in zip(got, (0.2, 1.9, 2.7)):
        assert start == pytest.approx(expected, abs=0.35)
    assert printed[0].startswith("[00:00:00] Others: others one")

    assert live_calls >= 2, "notes should have been updated while recording"
    assert any(c["model"] == "anthropic.claude-haiku-5-5" for c in llm.calls[:live_calls])
    assert [c["model"] for c in llm.calls[live_calls:]] == ["anthropic.claude-sonnet-5-5"] * 2

    with wave.open(str(out / "audio.wav")) as wav:  # L = mic, R = system
        assert wav.getnchannels() == 2
        assert wav.getnframes() / RATE == pytest.approx(5.0, abs=0.6)
    assert not list(out.glob(".*.wav"))
    meta = json.loads((out / "session.json").read_text())
    assert meta["audio"] == "audio.wav" and meta["audio_channels"] == ["mic", "system"]


def test_flac_encoding(cfg, fake_audio):
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    cfg.audio.format = "flac"
    session = Session(cfg, skills=[], transcriber=lambda: FakeTranscriber([]), echo=lambda _l: None)
    session.start()
    time.sleep(1.0)
    out = session.stop()
    assert (out / "audio.flac").stat().st_size > 0 and not (out / "audio.wav").exists()


def test_watch_records_only_during_a_call(cfg, fake_audio):
    cfg.watch.poll_seconds, cfg.watch.start_after, cfg.watch.stop_after = 0.1, 0.3, 0.5
    sessions: list[Session] = []

    def make_session():
        sessions.append(Session(cfg, skills=[], echo=lambda _l: None,
                                transcriber=lambda: FakeTranscriber(["hello"])))
        return sessions[-1]

    done = threading.Event()
    thread = threading.Thread(target=watch, args=(cfg,),
                              kwargs={"make_session": make_session, "should_stop": done.is_set})
    thread.start()
    try:
        time.sleep(0.6)
        assert sessions == []                       # no call, no recording
        (fake_audio / "source-outputs.txt").write_text(CHROME_ON_CALL)
        time.sleep(2.5)
        assert len(sessions) == 1 and sessions[0].live
        (fake_audio / "source-outputs.txt").write_text("")
        time.sleep(1.5)                             # call over -> session finalised
        assert (sessions[0].dir / "session.json").exists()
        assert len(sessions) == 1
    finally:
        done.set()
        thread.join(timeout=10)
    assert len(sessions[0].transcript) >= 1
