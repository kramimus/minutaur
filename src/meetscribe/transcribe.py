"""Speech-to-text backends. Each takes 16 kHz float32 audio and returns text."""
from __future__ import annotations

import io
import logging
import os
import re
import wave
from typing import Protocol

import numpy as np

from .audio import RATE
from .config import Transcription

log = logging.getLogger("meetscribe.transcribe")

# Whisper invents these on near-silent audio.
_HALLUCINATIONS = re.compile(
    r"^\W*(thanks? (you )?for watching|please subscribe|subtitles by.*|transcribed by.*)\W*$", re.I
)


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray) -> str: ...


def _clean(text: str) -> str:
    text = " ".join(text.split())
    return "" if _HALLUCINATIONS.match(text) else text


class FasterWhisperTranscriber:
    """Local transcription with faster-whisper (CTranslate2)."""

    def __init__(self, cfg: Transcription):
        from faster_whisper import WhisperModel

        self.cfg = cfg
        device = cfg.device
        if device == "auto":
            try:
                import ctranslate2

                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"

        def load(dev: str) -> WhisperModel:
            compute = cfg.compute_type
            if compute == "auto":
                compute = "float16" if dev == "cuda" else "int8"
            log.info("loading Whisper model %s on %s (%s)", cfg.model, dev, compute)
            model = WhisperModel(cfg.model, device=dev, compute_type=compute)
            # GPU libraries load lazily; run once so a broken CUDA setup fails here.
            list(model.transcribe(np.zeros(RATE, dtype=np.float32), language="en")[0])
            return model

        try:
            self.model = load(device)
        except Exception as exc:
            if device != "cuda" or cfg.device == "cuda":
                raise
            log.warning("GPU unavailable (%s); falling back to CPU", exc)
            self.model = load("cpu")

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _info = self.model.transcribe(
            audio,
            language=self.cfg.language or None,
            beam_size=self.cfg.beam_size,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=self.cfg.vocabulary or None,
        )
        return _clean(" ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6))


class OpenAITranscriber:
    """Any endpoint that speaks the OpenAI /audio/transcriptions API."""

    def __init__(self, cfg: Transcription):
        import httpx

        self.cfg = cfg
        key = os.environ.get(cfg.api_key_env, "")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.client = httpx.Client(base_url=cfg.api_base.rstrip("/"), headers=headers, timeout=60)

    def transcribe(self, audio: np.ndarray) -> str:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        data = {"model": self.cfg.api_model, "response_format": "json"}
        if self.cfg.language:
            data["language"] = self.cfg.language
        if self.cfg.vocabulary:
            data["prompt"] = self.cfg.vocabulary
        resp = self.client.post(
            "/audio/transcriptions",
            data=data,
            files={"file": ("audio.wav", buf.getvalue(), "audio/wav")},
        )
        resp.raise_for_status()
        return _clean(resp.json().get("text", ""))


def make_transcriber(cfg: Transcription) -> Transcriber:
    if cfg.backend == "faster-whisper":
        return FasterWhisperTranscriber(cfg)
    if cfg.backend == "openai":
        return OpenAITranscriber(cfg)
    raise ValueError(f"unknown transcription backend: {cfg.backend!r}")
