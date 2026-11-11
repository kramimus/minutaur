"""Audio capture through PulseAudio / PipeWire-pulse (`pactl`, `parec`).

Two streams are captured separately: the monitor of the output device (what
you hear, i.e. the other participants) and the microphone (what you say).
Keeping them apart gives speaker attribution for free.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

log = logging.getLogger("meetscribe.audio")

RATE = 16000  # what Whisper wants
FRAME_SAMPLES = RATE // 10  # 100 ms
FRAME_BYTES = FRAME_SAMPLES * 2  # s16le mono

# Called with (int16 samples, seconds since session start).
FrameCallback = Callable[[np.ndarray, float], None]


class AudioError(RuntimeError):
    pass


# --------------------------------------------------------------------- pactl


def pactl(*args: str) -> str:
    if not shutil.which("pactl"):
        raise AudioError("pactl not found (sudo apt install pulseaudio-utils)")
    try:
        proc = subprocess.run(
            ["pactl", *args],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C"},  # output is parsed, keep it untranslated
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError("pactl timed out; is the audio server running?") from exc
    if proc.returncode != 0:
        raise AudioError(proc.stderr.strip() or f"pactl {' '.join(args)} failed")
    return proc.stdout


def _default(kind: str) -> str:
    try:
        name = pactl(f"get-default-{kind}").strip()
    except AudioError:
        name = ""
    if not name:  # older pactl
        match = re.search(rf"^Default {kind.capitalize()}: (.+)$", pactl("info"), re.M)
        name = match.group(1).strip() if match else ""
    if not name:
        raise AudioError(f"could not determine the default {kind}")
    return name


def default_monitor() -> str:
    return _default("sink") + ".monitor"


def default_source() -> str:
    return _default("source")


def parse_pactl_list(text: str) -> list[dict[str, str]]:
    """Parse the text output of `pactl list <things>` into one dict per item."""
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            current = {"_header": line.strip()}
            items.append(current)
        elif current is not None:
            prop = re.match(r'\s+([\w.\-]+) = "(.*)"\s*$', line)
            if prop:
                current[prop.group(1)] = prop.group(2)
                continue
            attr = re.match(r"\s+([A-Za-z ]+): (.*)$", line)
            if attr:
                current[attr.group(1).strip()] = attr.group(2).strip()
    return items


def browser_mic_streams(browsers: Iterable[str]) -> list[dict[str, str]]:
    """Recording streams currently held open by a web browser.

    A browser capturing the microphone is the signal that a call is in
    progress. The audio server cannot tell Meet from any other web call.
    """
    browsers = [b.lower() for b in browsers]
    found = []
    for item in parse_pactl_list(pactl("list", "source-outputs")):
        ident = " ".join(
            item.get(key, "")
            for key in ("application.name", "application.process.binary", "application.id")
        ).lower()
        if "meetscribe" in ident or item.get("Corked") == "yes":
            continue
        if any(b in ident for b in browsers):
            found.append(item)
    return found


# ------------------------------------------------------------------- capture


class ParecSource(threading.Thread):
    """Reads one device with `parec`, records it to WAV and hands frames on."""

    def __init__(
        self,
        label: str,
        device: str,
        on_frame: FrameCallback,
        t0: float,
        wav_path: Path | None = None,
    ):
        super().__init__(name=f"capture-{label}", daemon=True)
        self.label = label
        self.device = device
        self.on_frame = on_frame
        self.t0 = t0
        self.wav_path = wav_path
        self._stopping = threading.Event()
        self._proc: subprocess.Popen | None = None

    def run(self) -> None:
        cmd = [
            "parec",
            f"--device={self.device}",
            "--format=s16le",
            f"--rate={RATE}",
            "--channels=1",
            "--latency-msec=100",
            "--client-name=meetscribe",
            f"--stream-name=meetscribe-{self.label}",
        ]
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
                    # If the server delivered nothing for a while (idle device),
                    # pad with silence so the recording stays on the wall clock.
                    gap = int(now * RATE) - written - FRAME_SAMPLES
                    if gap > RATE // 2:
                        wav.writeframes(b"\0\0" * gap)
                        written += gap
                    wav.writeframes(data)
                    written += FRAME_SAMPLES
                try:
                    self.on_frame(
                        np.frombuffer(data, dtype=np.int16), max(0.0, now - FRAME_SAMPLES / RATE)
                    )
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


def _open_wav(path: Path, channels: int = 1) -> wave.Wave_write:
    wav = wave.open(str(path), "wb")
    wav.setnchannels(channels)
    wav.setsampwidth(2)
    wav.setframerate(RATE)
    return wav


# --------------------------------------------------------------------- files


def decode_file(path: Path, split_stereo: bool = False) -> list[np.ndarray]:
    """Decode any audio file to 16 kHz int16. One array, or [left, right]."""
    channels = 2 if split_stereo else 1
    if shutil.which("ffmpeg"):
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le",
             "-ac", str(channels), "-ar", str(RATE), "-"],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise AudioError(f"ffmpeg could not read {path}: {proc.stderr.decode().strip()}")
        data = np.frombuffer(proc.stdout, dtype=np.int16)
    else:
        with wave.open(str(path), "rb") as wav:
            if wav.getframerate() != RATE or wav.getsampwidth() != 2:
                raise AudioError("without ffmpeg only 16 kHz 16-bit WAV files can be read")
            src_channels = wav.getnchannels()
            data = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        if src_channels != channels:
            frames = data.reshape(-1, src_channels)
            if channels == 1:
                data = frames.mean(axis=1).astype(np.int16)
            else:
                data = np.repeat(frames[:, :1], 2, axis=1).reshape(-1)
    if split_stereo:
        return [data[0::2], data[1::2]]
    return [data]


def feed_samples(samples: np.ndarray, on_frame: FrameCallback) -> None:
    for i in range(0, len(samples), FRAME_SAMPLES):
        on_frame(samples[i : i + FRAME_SAMPLES], i / RATE)


def merge_recordings(parts: list[Path], out_base: Path, fmt: str = "flac") -> Path | None:
    """Combine per-source WAVs into one file (one channel per source).

    Returns the path written, or None if there was nothing to merge.
    """
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
                if longest == 0:
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

    codec = {"flac": ["-c:a", "flac"], "opus": ["-c:a", "libopus", "-b:a", "48k"]}.get(fmt)
    if codec and shutil.which("ffmpeg"):
        encoded = out_base.with_suffix("." + fmt)
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(wav_path), *codec, str(encoded)],
            capture_output=True,
        )
        if proc.returncode == 0:
            wav_path.unlink()
            return encoded
        log.warning("ffmpeg encode failed, keeping WAV: %s", proc.stderr.decode().strip())
    return wav_path
