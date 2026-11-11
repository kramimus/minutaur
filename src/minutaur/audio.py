"""Audio in and out, through PulseAudio / PipeWire (`pactl`, `parec`) and ffmpeg.

Two streams are captured separately: the monitor of the output device (what
you hear, i.e. the other participants) and the microphone (what you say).
Keeping them apart gives speaker labels for free.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

log = logging.getLogger("minutaur.audio")

RATE = 16000  # what Whisper wants
FRAME_SAMPLES = RATE // 10  # 100 ms
FRAME_BYTES = FRAME_SAMPLES * 2  # s16le mono

# Called with (int16 samples, seconds since the session started).
FrameCallback = Callable[[np.ndarray, float], None]


class AudioError(RuntimeError):
    pass


def _run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600,
                              env={**os.environ, "LC_ALL": "C"})  # output is parsed: keep it English
    except FileNotFoundError:
        package = "ffmpeg" if cmd[0] == "ffmpeg" else "pulseaudio-utils"
        raise AudioError(f"{cmd[0]} not found (sudo apt install {package})") from None
    except subprocess.TimeoutExpired:
        raise AudioError(f"{cmd[0]} timed out") from None
    if proc.returncode != 0:
        raise AudioError(f"{what}: {proc.stderr.decode(errors='replace').strip()}")
    return proc


# --------------------------------------------------------------------- devices


def pactl(*args: str) -> str:
    return _run(["pactl", *args], f"pactl {' '.join(args)}").stdout.decode()


def default_monitor() -> str:
    return pactl("get-default-sink").strip() + ".monitor"


def default_source() -> str:
    return pactl("get-default-source").strip()


def parse_pactl_list(text: str) -> list[dict[str, str]]:
    """Parse the text output of `pactl list <things>` into one dict per item."""
    items: list[dict[str, str]] = []
    for line in text.splitlines():
        if line and not line[0].isspace():
            items.append({"_header": line.strip()})
        elif items and (m := re.match(r'\s+([\w.\-]+) = "(.*)"\s*$', line)):
            items[-1][m.group(1)] = m.group(2)
        elif items and (m := re.match(r"\s+([A-Za-z ]+): (.*)$", line)):
            items[-1][m.group(1).strip()] = m.group(2).strip()
    return items


def browser_mic_streams(browsers: Iterable[str]) -> list[dict[str, str]]:
    """Recording streams a web browser currently holds open.

    A browser using the microphone is the sign that a call is in progress. The
    audio server cannot tell Meet from any other site, so any web call counts.
    """
    browsers = [b.lower() for b in browsers]
    found = []
    for item in parse_pactl_list(pactl("list", "source-outputs")):
        ident = " ".join(item.get(k, "") for k in
                         ("application.name", "application.process.binary", "application.id")).lower()
        if "minutaur" not in ident and item.get("Corked") != "yes" and any(b in ident for b in browsers):
            found.append(item)
    return found


# --------------------------------------------------------------------- capture


def _open_wav(path: Path, channels: int = 1) -> wave.Wave_write:
    wav = wave.open(str(path), "wb")
    wav.setnchannels(channels)
    wav.setsampwidth(2)
    wav.setframerate(RATE)
    return wav


class ParecSource(threading.Thread):
    """Reads one device with `parec`, records it to WAV and hands each frame on."""

    def __init__(self, label: str, device: str, on_frame: FrameCallback, t0: float,
                 wav_path: Path | None = None):
        super().__init__(name=f"capture-{label}", daemon=True)
        self.label, self.device, self.on_frame, self.t0, self.wav_path = label, device, on_frame, t0, wav_path
        self._stopping = threading.Event()
        self._proc: subprocess.Popen | None = None

    def run(self) -> None:
        try:
            self._proc = subprocess.Popen(
                ["parec", f"--device={self.device}", "--format=s16le", f"--rate={RATE}", "--channels=1",
                 "--latency-msec=100", "--client-name=minutaur", f"--stream-name=minutaur-{self.label}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            log.error("parec not found (sudo apt install pulseaudio-utils)")
            return
        wav = _open_wav(self.wav_path) if self.wav_path else None
        written = 0
        try:
            while not self._stopping.is_set():
                data = self._proc.stdout.read(FRAME_BYTES)
                if len(data) < FRAME_BYTES:
                    break
                now = time.monotonic() - self.t0
                if wav:
                    # An idle device can deliver nothing for a while; pad with
                    # silence so the recording stays on the wall clock.
                    gap = int(now * RATE) - written - FRAME_SAMPLES
                    if gap > RATE // 2:
                        wav.writeframes(b"\0\0" * gap)
                        written += gap
                    wav.writeframes(data)
                    written += FRAME_SAMPLES
                try:
                    self.on_frame(np.frombuffer(data, dtype=np.int16), max(0.0, now - FRAME_SAMPLES / RATE))
                except Exception:
                    log.exception("frame handler failed for %s", self.label)
        finally:
            if wav:
                wav.close()
            if not self._stopping.is_set():
                err = self._proc.stderr.read().decode(errors="replace").strip()
                log.error("capture of %s (%s) stopped: %s", self.label, self.device, err or "EOF")
            self._proc.stdout.close()
            self._proc.stderr.close()

    def stop(self) -> None:
        self._stopping.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


# ----------------------------------------------------------------------- files


def decode_file(path: Path, split_stereo: bool = False) -> list[np.ndarray]:
    """Decode any audio file to 16 kHz int16: one array, or [left, right]."""
    channels = 2 if split_stereo else 1
    out = _run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", str(channels),
                "-ar", str(RATE), "-"], f"could not read {path}").stdout
    data = np.frombuffer(out, dtype=np.int16)
    return [data[0::2], data[1::2]] if split_stereo else [data]


def feed_samples(samples: np.ndarray, on_frame: FrameCallback) -> None:
    for i in range(0, len(samples), FRAME_SAMPLES):
        on_frame(samples[i : i + FRAME_SAMPLES], i / RATE)


def merge_recordings(parts: list[Path], out_base: Path, fmt: str = "flac") -> Path | None:
    """Combine per-source WAVs into one file with a channel per source, then
    encode it. Returns the file written, or None if nothing was recorded."""
    parts = [p for p in parts if p.exists() and p.stat().st_size > 44]
    if not parts:
        return None
    wav_path = out_base.with_suffix(".wav")
    readers = [wave.open(str(p), "rb") for p in parts]
    try:
        with _open_wav(wav_path, channels=len(readers)) as out:
            while True:
                chunks = [np.frombuffer(r.readframes(RATE), dtype=np.int16) for r in readers]
                longest = max(len(c) for c in chunks)
                if not longest:
                    break
                block = np.zeros((longest, len(chunks)), dtype=np.int16)
                for i, chunk in enumerate(chunks):
                    block[: len(chunk), i] = chunk
                out.writeframes(block.tobytes())
    finally:
        for reader in readers:
            reader.close()
    for part in parts:
        part.unlink()
    if fmt == "wav":
        return wav_path
    codec = {"flac": ["-c:a", "flac"], "opus": ["-c:a", "libopus", "-b:a", "48k"]}[fmt]
    encoded = out_base.with_suffix("." + fmt)
    try:
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(wav_path), *codec, str(encoded)], "encoding failed")
    except AudioError as exc:
        log.warning("%s; keeping the WAV", exc)
        return wav_path
    wav_path.unlink()
    return encoded
