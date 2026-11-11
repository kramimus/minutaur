from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from minutaur.audio import RATE
from minutaur.config import Config


def burst_signal(pattern: list[tuple[float, bool]], seed: int = 0) -> np.ndarray:
    """int16 audio made of (seconds, loud?) stretches: noise bursts and near-silence."""
    rng = np.random.default_rng(seed)
    chunks = []
    for seconds, loud in pattern:
        n = int(seconds * RATE)
        amp = 0.2 if loud else 0.0005
        chunks.append((rng.standard_normal(n) * amp * 32767).clip(-32767, 32767).astype(np.int16))
    return np.concatenate(chunks)


def write_wav(path: Path, samples: np.ndarray, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(samples.tobytes())
    return path


class FakeTranscriber:
    """Returns the scripted lines in order, one per segment."""

    def __init__(self, lines: list[str]):
        self.lines = list(lines)
        self.durations: list[float] = []

    def transcribe(self, audio: np.ndarray) -> str:
        self.durations.append(len(audio) / RATE)
        return self.lines.pop(0) if self.lines else "more talking"


class FakeLLM:
    """Records every call; replies with a numbered notes document."""

    def __init__(self, reply=None):
        self.calls: list[dict] = []
        self.reply = reply

    def complete(self, *, model: str, system: str, user: str, max_tokens: int) -> str:
        self.calls.append({"model": model, "system": system, "user": user})
        if self.reply is not None:
            return self.reply(self.calls[-1]) if callable(self.reply) else self.reply
        return f"- note v{len(self.calls)} from {model}"


@pytest.fixture
def cfg(tmp_path) -> Config:
    config = Config()
    config.output_dir = str(tmp_path / "meetings")
    config.audio.format = "wav"
    return config
