"""WebRTC: an audio track per browser fed from the scanner, plus helpers."""

import asyncio
import fractions
import time
from collections import deque

import av
import numpy as np
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from .dsp import AUDIO_RATE

FRAME = AUDIO_RATE // 50            # 20 ms frames
PRIME = 3 * FRAME                   # buffer 60 ms before (re)starting playback
MAX_BUFFER = AUDIO_RATE // 3        # beyond ~330 ms we are drifting: drop audio
TIME_BASE = fractions.Fraction(1, AUDIO_RATE)


class ScannerAudioTrack(MediaStreamTrack):
    """Real-time paced audio track; plays silence when nothing is received."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._chunks = deque()
        self._buffered = 0
        self._primed = False
        self._start = None
        self._pts = 0

    def feed(self, audio):
        self._chunks.append(audio)
        self._buffered += len(audio)
        while self._buffered > MAX_BUFFER:
            self._buffered -= len(self._chunks.popleft())

    def _take(self, n):
        if not self._primed:
            if self._buffered < PRIME:
                return np.zeros(n, np.float32)
            self._primed = True
        out = np.zeros(n, np.float32)
        got = 0
        while got < n and self._chunks:
            c = self._chunks[0]
            k = min(n - got, len(c))
            out[got:got + k] = c[:k]
            got += k
            if k == len(c):
                self._chunks.popleft()
            else:
                self._chunks[0] = c[k:]
        self._buffered -= got
        if got < n:
            self._primed = False        # underrun: re-buffer before continuing
        return out

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        if self._start is None:
            self._start = time.monotonic()
        else:
            wait = self._start + self._pts / AUDIO_RATE - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        pcm = (np.clip(self._take(FRAME), -1, 1) * 32767).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = AUDIO_RATE
        frame.pts = self._pts
        frame.time_base = TIME_BASE
        self._pts += FRAME
        return frame


class AudioBroadcaster:
    def __init__(self):
        self.tracks = set()

    def push(self, audio):
        for t in list(self.tracks):
            t.feed(audio)
