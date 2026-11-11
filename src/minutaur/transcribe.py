"""Speech to text with faster-whisper, locally."""
from __future__ import annotations

import logging
import re

import numpy as np

from .audio import RATE
from .config import Transcription

log = logging.getLogger("minutaur.transcribe")

# Whisper invents these on near-silent audio.
_HALLUCINATION = re.compile(
    r"^\W*(thanks? (you )?for watching|please subscribe|subtitles by.*|transcribed by.*)\W*$", re.I)


def clean(text: str) -> str:
    text = " ".join(text.split())
    return "" if _HALLUCINATION.match(text) else text


class Transcriber:
    def __init__(self, cfg: Transcription):
        self.cfg = cfg
        device = cfg.device
        if device == "auto":
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        try:
            self.model = self._load(device)
        except Exception as exc:
            if device != "cuda" or cfg.device == "cuda":
                raise
            log.warning("GPU unavailable (%s); using the CPU", exc)
            self.model = self._load("cpu")

    def _load(self, device: str):
        from faster_whisper import WhisperModel

        compute = "float16" if device == "cuda" else "int8"
        log.info("loading Whisper model %s on %s", self.cfg.model, device)
        model = WhisperModel(self.cfg.model, device=device, compute_type=compute)
        # GPU libraries load lazily: run once so a broken CUDA setup fails here.
        list(model.transcribe(np.zeros(RATE, dtype=np.float32))[0])
        return model

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio,
            language=self.cfg.language or None,
            beam_size=self.cfg.beam_size,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=self.cfg.vocabulary or None,
        )
        return clean(" ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6))
