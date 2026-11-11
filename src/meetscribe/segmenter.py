"""Cuts a live audio stream into utterance-sized chunks for the transcriber.

Energy-based endpointing with an adaptive noise floor. It only decides where
to cut; the transcriber runs a proper VAD on each chunk to reject non-speech.
"""
from __future__ import annotations

from collections import deque
from typing import Callable

import numpy as np

from .audio import FRAME_SAMPLES, RATE

FRAME_SECONDS = FRAME_SAMPLES / RATE

# Called with (start seconds, float32 samples in [-1, 1]).
SegmentCallback = Callable[[float, np.ndarray], None]


class Segmenter:
    def __init__(
        self,
        on_segment: SegmentCallback,
        *,
        min_speech: float = 0.3,   # shorter bursts are dropped (clicks, key presses)
        end_silence: float = 0.7,  # pause that ends an utterance
        soft_max: float = 12.0,    # past this, any short pause ends it
        hard_max: float = 25.0,    # past this, cut regardless
        preroll: float = 0.3,      # audio kept from before speech was detected
        min_threshold: float = 0.006,
    ):
        self.on_segment = on_segment
        self.min_speech = min_speech
        self.end_silence = end_silence
        self.soft_max = soft_max
        self.hard_max = hard_max
        self.min_threshold = min_threshold
        self._noise = min_threshold / 3
        self._preroll: deque[tuple[np.ndarray, float]] = deque(maxlen=max(1, round(preroll / FRAME_SECONDS)))
        self._frames: list[np.ndarray] = []
        self._start = 0.0
        self._speech_frames = 0
        self._silent_run = 0

    def feed(self, frame: np.ndarray, t: float) -> None:
        """Feed one frame of int16 samples that began `t` seconds into the session."""
        if len(frame) == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(frame.astype(np.float32) / 32768.0))))
        threshold = max(self.min_threshold, self._noise * 3)
        loud = rms >= threshold

        if not self._frames:
            if loud:
                self._frames = [f for f, _ in self._preroll] + [frame]
                self._start = self._preroll[0][1] if self._preroll else t
                self._preroll.clear()
                self._speech_frames = 1
                self._silent_run = 0
            else:
                self._noise = min(0.05, 0.95 * self._noise + 0.05 * rms)
                self._preroll.append((frame, t))
            return

        self._frames.append(frame)
        if loud:
            self._speech_frames += 1
            self._silent_run = 0
        else:
            self._silent_run += 1
        duration = len(self._frames) * FRAME_SECONDS
        pause = self._silent_run * FRAME_SECONDS
        needed = 0.3 if duration >= self.soft_max else self.end_silence
        if pause >= needed or duration >= self.hard_max:
            self._emit()

    def flush(self) -> None:
        """Emit whatever is in progress (call when the stream ends)."""
        if self._frames:
            self._emit()

    def _emit(self) -> None:
        frames, start = self._frames, self._start
        enough = self._speech_frames * FRAME_SECONDS >= self.min_speech
        # Leave a little trailing silence; drop the rest.
        keep = len(frames) - max(0, self._silent_run - 2)
        self._frames = []
        self._speech_frames = 0
        self._silent_run = 0
        if enough:
            audio = np.concatenate(frames[:keep]).astype(np.float32) / 32768.0
            self.on_segment(start, audio)
