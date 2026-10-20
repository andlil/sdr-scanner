"""Signal processing: FFT channelizer, NFM demodulator and automatic squelch.

The receiver delivers complex samples at `fs` (e.g. 1.024 MS/s). Every block is
FFT'd once; that single spectrum is used both to measure the power of every
channel inside the tuned window (for scanning) and to extract the one channel
being listened to (overlap-save filtering + decimation done in the frequency
domain, which is far cheaper on a Pi than time-domain FIR filters).
"""

import collections
import functools
import math
import time

import numpy as np
import scipy.fft as sfft
from scipy import signal, special

CHANNEL_RATE = 32_000       # minimum complex baseband rate of an extracted channel
AUDIO_RATE = 16_000         # audio sample rate sent to the browser
SUB_FFT = 256               # IFFT size of an extracted channel


@functools.lru_cache(maxsize=None)
def _gamma_median(k):
    """Median of the mean of k unit-mean exponential variables."""
    return float(special.gammaincinv(k, 0.5)) / k


class Channelizer:
    """Overlap-save FFT channelizer.

    The decimation D is the largest whole number keeping the channel rate
    fs / D at or above CHANNEL_RATE: exactly 32 kHz for 1.024 MS/s and
    768 kS/s, 32.57 kHz for the Airspy HF+'s 912 kS/s.

    Block layout: N = SUB_FFT * D samples per FFT, of which `overlap` are
    repeated from the previous block. Output per block is `step / D` samples
    of channel baseband at `channel_rate`.
    """

    def __init__(self, fs):
        if fs < CHANNEL_RATE:
            raise ValueError(f"sample rate {fs} is below {CHANNEL_RATE}")
        self.fs = fs
        self.D = int(fs // CHANNEL_RATE)
        self.channel_rate = fs / self.D
        self.N = SUB_FFT * self.D
        self.overlap = self.N // 4
        self.step = self.N - self.overlap
        self.bin_hz = fs / self.N
        self._hist = np.zeros(self.overlap, np.complex64)
        self.block_index = -1
        self._sub_bins = (np.fft.fftfreq(SUB_FFT) * SUB_FFT).astype(int)
        self._filters = {bw: self._design(bw) for bw in (12_500, 25_000)}

    def _design(self, bw):
        # Linear-phase low-pass with at most overlap+1 taps, so overlap-save
        # is exact. 12.5 kHz NFM occupies about +-5.5 kHz, 25 kHz about +-8 kHz.
        cutoff = 5_500 if bw <= 12_500 else 8_500
        taps = signal.firwin(self.overlap + 1, cutoff, fs=self.fs, window=("kaiser", 8.0))
        H = sfft.fft(taps, self.N).astype(np.complex64)
        return H[self._sub_bins % self.N]

    def reset(self):
        self._hist[:] = 0
        self.block_index = -1

    def spectra(self, samples):
        """Feed K*step new samples, return the (K, N) spectra of those blocks.

        Batching blocks into one FFT call keeps Python overhead low on a Pi.
        """
        buf = np.concatenate((self._hist, samples))
        k = len(samples) // self.step
        frames = np.lib.stride_tricks.sliding_window_view(buf, self.N)[::self.step][:k]
        self._hist = buf[k * self.step:k * self.step + self.overlap].copy()
        self._first = self.block_index + 1
        self.block_index += k
        return sfft.fft(frames, axis=1)

    def bin_of(self, offset_hz):
        return int(round(offset_hz / self.bin_hz))

    def _bins(self, offset_hz, half_hz):
        k = self.bin_of(offset_hz)
        half = max(1, int(half_hz / self.bin_hz))
        return np.arange(k - half, k + half + 1) % self.N

    def channel_power(self, X, offset_hz, bw):
        """Mean power per bin across the channel bandwidth, over all blocks."""
        Xc = X[:, self._bins(offset_hz, 0.4 * bw)]
        return float(np.mean(Xc.real ** 2 + Xc.imag ** 2))

    def local_noise(self, X, offset_hz, span_hz=100_000):
        """Robust noise estimate around a channel: median of nearby bins.

        Each bin is averaged over the K blocks; for pure noise that average is
        Gamma(K)-distributed, so mean = median / gamma_median(K). A few
        signals in the neighbourhood barely move the median.
        """
        Xn = X[:, self._bins(offset_hz, span_hz)]
        p = np.mean(Xn.real ** 2 + Xn.imag ** 2, axis=0)
        return float(np.median(p)) / _gamma_median(X.shape[0])

    def extract(self, X, offset_hz, bw):
        """Channel baseband at channel_rate (K * step/D samples) from spectra()."""
        k = self.bin_of(offset_hz)
        sel = X[:, (k + self._sub_bins) % self.N] * self._filters[bw]
        y = sfft.ifft(sel, axis=1)[:, self.overlap // self.D:]
        # Undo the phase jump that the bin shift causes between blocks.
        blocks = self._first + np.arange(X.shape[0])
        rot = np.exp(-2j * np.pi * ((k * self.step * blocks) % self.N) / self.N)
        return (y * rot[:, None]).astype(np.complex64).ravel()


class Resampler:
    """Streaming linear-interpolation resampler for band-limited audio.

    Used after the 300-3400 Hz voice filter, so the interpolation's gentle
    roll-off and residual aliasing are negligible. An exact 2:1 ratio takes
    every other sample, as the original fixed 32 kHz -> 16 kHz path did.
    """

    def __init__(self, in_rate, out_rate):
        self.step = in_rate / out_rate
        self.exact = None
        if abs(self.step - round(self.step)) < 1e-9:
            self.exact = int(round(self.step))
        self.reset()

    def reset(self):
        self._pos = 0.0             # position of the next output, in input samples
        self._last = 0.0            # last input sample of the previous block

    def process(self, x):
        if self.exact:
            start = int(self._pos)
            out = x[start::self.exact]
            self._pos = (start - len(x)) % self.exact
            return out
        buf = np.concatenate(([self._last], x))     # buf[0] is input position -1
        n = int(np.floor((len(x) - 1 - self._pos) / self.step)) + 1 if len(x) - 1 >= self._pos else 0
        t = self._pos + np.arange(n) * self.step + 1.0
        i = np.floor(t).astype(int)
        frac = t - i
        out = buf[i] * (1 - frac) + buf[np.minimum(i + 1, len(buf) - 1)] * frac
        self._pos += n * self.step - len(x)
        self._last = x[-1]
        return out


class NfmDemodulator:
    """Narrowband FM discriminator -> 300-3400 Hz voice audio at AUDIO_RATE."""

    def __init__(self, rate=CHANNEL_RATE, deviation=3_000):
        self.scale = rate / (2 * np.pi * deviation)
        self._prev = np.complex64(0)
        # Voice band-pass (removes CTCSS tones and hiss), also the anti-alias
        # filter for resampling down to AUDIO_RATE.
        self._sos = signal.butter(4, [300, 3400], btype="band", fs=rate, output="sos")
        self._zi = signal.sosfilt_zi(self._sos) * 0
        self._resampler = Resampler(rate, AUDIO_RATE)
        # De-emphasis: single pole at 300 Hz (inverse of the 6 dB/octave
        # pre-emphasis used on land-mobile NFM).
        a = math.exp(-2 * math.pi * 300 / AUDIO_RATE)
        self._de_b, self._de_a = [1 - a], [1, -a]
        self._de_zi = np.zeros(1)
        self._agc = 0.02

    def reset(self):
        self._prev = np.complex64(0)
        self._zi[:] = 0
        self._de_zi[:] = 0
        self._resampler.reset()

    def process(self, iq):
        x = np.concatenate(([self._prev], iq))
        self._prev = iq[-1]
        fm = np.angle(x[1:] * np.conj(x[:-1])) * self.scale
        audio, self._zi = signal.sosfilt(self._sos, fm, zi=self._zi)
        audio = self._resampler.process(audio)
        audio, self._de_zi = signal.lfilter(self._de_b, self._de_a, audio, zi=self._de_zi)
        return self._level(audio)

    TARGET_RMS = 0.35           # about -12 dBFS average level after the limiter
    MAX_GAIN = 40.0

    def _level(self, audio):
        # AGC on the average (RMS) level, so all channels play equally loud:
        # quick to turn down (~0.1 s), slow to turn up again (~5 s) so pauses
        # between words don't pump up the noise. tanh then rounds off the
        # peaks instead of clipping them.
        if not len(audio):
            return audio.astype(np.float32)
        rms = float(np.sqrt(np.mean(audio * audio)))
        rate = 0.3 if rms > self._agc else 0.005
        self._agc += rate * (rms - self._agc)
        gain = min(self.TARGET_RMS / max(self._agc, 1e-4), self.MAX_GAIN)
        return np.tanh(audio * gain).astype(np.float32)


class AutoSquelch:
    """Per-channel automatic squelch.

    SNR is measured against the local noise floor (median of neighbouring
    bins), so it adapts instantly to gain changes. On top of that each channel
    learns its own idle baseline, so a permanent spur or interferer on a
    channel raises that channel's threshold instead of holding the scanner.

    The baseline is the *minimum* SNR seen over the last few minutes
    ("minimum statistics"). A busy channel still drops back to pure noise
    between transmissions, if only briefly, so frequent weak signals cannot
    push its threshold up. Only something that never goes away for the whole
    window, such as a spur, raises it.
    """

    OPEN_MIN_DB = 6.0       # never open below this SNR (before the user's offset)
    MARGIN_DB = 6.0         # open this far above the learned idle baseline
    HYSTERESIS_DB = 3.0     # close this far below the open threshold
    # Floors so a negative offset can't put the squelch inside the noise
    # (noise-only channels measure up to ~0.8 dB SNR).
    FLOOR_OPEN_DB = 2.0
    FLOOR_CLOSE_DB = 1.5
    WINDOW_S = 300.0        # how far back the minimum looks
    BUCKET_S = 15.0         # minimum kept per bucket, so memory stays small

    def __init__(self, offset_db=0.0):
        self.offset_db = offset_db  # user fine-tuning: <0 more sensitive, >0 stricter
        self.baseline_db = 0.0
        self.snr_db = -99.0
        self.is_open = False
        self._bucket_start = None
        self._bucket_min = math.inf
        self._buckets = collections.deque(maxlen=int(self.WINDOW_S / self.BUCKET_S))

    @property
    def open_db(self):
        auto = max(self.OPEN_MIN_DB, self.baseline_db + self.MARGIN_DB)
        return max(self.FLOOR_OPEN_DB, auto + self.offset_db)

    @property
    def close_db(self):
        return max(self.FLOOR_CLOSE_DB, self.open_db - self.HYSTERESIS_DB)

    def update(self, power, noise, now=None):
        """Feed a power measurement; returns is_open.

        `now` (seconds, monotonic) defaults to the wall clock; the window is
        real time, however often the scanner gets round to this channel.
        """
        now = time.monotonic() if now is None else now
        snr = 10 * math.log10(max(power, 1e-20) / max(noise, 1e-20))
        self.snr_db = snr
        if self._bucket_start is None:
            self._bucket_start = now
        self._bucket_min = min(self._bucket_min, snr)
        if now - self._bucket_start >= self.BUCKET_S:
            self._buckets.append(self._bucket_min)
            self._bucket_start = now
            self._bucket_min = math.inf
        lowest = min(min(self._buckets, default=math.inf), self._bucket_min)
        self.baseline_db = max(-3.0, min(lowest, 30.0))
        if self.is_open:
            self.is_open = snr >= self.close_db
        else:
            self.is_open = snr >= self.open_db
        return self.is_open
