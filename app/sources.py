"""Sample sources: real SDRs through SoapySDR, and a simulator for testing."""

import logging
import math
import random
import time

import numpy as np

from .dsp import CHANNEL_RATE

log = logging.getLogger(__name__)

# Preferred sample rates, highest first. Must be multiples of CHANNEL_RATE.
PREFERRED_RATES = (1_024_000, 768_000, 384_000, 256_000)


DRIVER_NAMES = {"rtlsdr": "RTL-SDR", "airspyhf": "Airspy HF+"}


class SourceError(RuntimeError):
    pass


def list_devices():
    """Receivers SoapySDR can see, as dicts (driver, serial, label, ...)."""
    import SoapySDR
    return [dict(d) for d in SoapySDR.Device.enumerate()
            if d["driver"] not in ("audio", "remote")]


def connected_devices():
    """Human-readable list of connected receivers, for error messages."""
    devs = list_devices()
    if not devs:
        return "No receivers are connected. Is it plugged in?"
    return "Connected: " + "; ".join(
        f"driver={d['driver']},serial={d.get('serial', '?')}" for d in devs) + "."


class SoapySource:
    """RTL-SDR, Airspy HF+ or any other SoapySDR receiver."""

    def __init__(self, args="", gain="auto", ppm=0.0, settle_s=0.05):
        self.args = args
        self.gain = gain
        self.ppm = ppm
        self.settle_s = settle_s
        self.dev = None
        self.stream = None
        self.name = "?"
        self.serial = ""
        self.sample_rate = 0
        self.freq_ranges = []
        self.center = 0

    def open(self):
        import SoapySDR
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

        wanted = dict(SoapySDR.KwargsFromString(self.args))
        if wanted.get("serial") and not SoapySDR.Device.enumerate(self.args):
            # Must check first: SoapyAirspyHF opens *any* Airspy when the
            # serial doesn't match, which would silently pick the wrong one.
            raise SourceError(f"no receiver with serial {wanted['serial']} "
                              f"(SDR_DEVICE={self.args!r}). {connected_devices()}")
        try:
            self.dev = SoapySDR.Device(self.args)
        except RuntimeError:
            raise SourceError(f"no SDR found (SDR_DEVICE={self.args!r}). {connected_devices()}") from None
        driver = self.dev.getDriverKey().lower()
        hw = dict(self.dev.getHardwareInfo())
        hw_key = self.dev.getHardwareKey()
        self.name = DRIVER_NAMES.get(driver, self.dev.getDriverKey())
        if hw_key and hw_key.lower() != driver:
            self.name += f" {hw_key}"
        self.serial = hw.get("serial") or wanted.get("serial") or ""
        log.info("opened SDR %s, serial %s (%s) %s", self.name, self.serial or "?", driver, hw)

        self.sample_rate = self._pick_rate()
        self.dev.setSampleRate(SOAPY_SDR_RX, 0, self.sample_rate)
        self.freq_ranges = [(r.minimum(), r.maximum())
                            for r in self.dev.getFrequencyRange(SOAPY_SDR_RX, 0)]
        if self.ppm and "CORR" in self.dev.listFrequencies(SOAPY_SDR_RX, 0):
            self.dev.setFrequency(SOAPY_SDR_RX, 0, "CORR", float(self.ppm))
        if str(self.gain).lower() == "auto":
            if self.dev.hasGainMode(SOAPY_SDR_RX, 0):
                self.dev.setGainMode(SOAPY_SDR_RX, 0, True)
        else:
            if self.dev.hasGainMode(SOAPY_SDR_RX, 0):
                self.dev.setGainMode(SOAPY_SDR_RX, 0, False)
            self.dev.setGain(SOAPY_SDR_RX, 0, float(self.gain))

        stream_args = {}
        if driver == "rtlsdr":
            # 16 ms USB transfers, 0.5 s of ring buffer: enough to ride out
            # stalls on a busy Pi. SoapyRTLSDR flushes the ring on retune.
            stream_args = {"bufflen": "32768", "buffers": "32"}
        self.stream = self.dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [0], stream_args)
        self.dev.activateStream(self.stream)
        self._buf = np.empty(65536, np.complex64)
        self.center = 0
        log.info("SDR %s ready at %d S/s, range %s", self.name, self.sample_rate,
                 ", ".join(f"{a/1e6:.3f}-{b/1e6:.3f} MHz" for a, b in self.freq_ranges))

    def _pick_rate(self):
        from SoapySDR import SOAPY_SDR_RX
        rates = set()
        ranges = self.dev.getSampleRateRange(SOAPY_SDR_RX, 0)
        for r in PREFERRED_RATES:
            if any(rg.minimum() - 1 <= r <= rg.maximum() + 1 for rg in ranges):
                rates.add(r)
        for r in self.dev.listSampleRates(SOAPY_SDR_RX, 0):
            if int(r) in PREFERRED_RATES:
                rates.add(int(r))
        if not rates:
            raise SourceError("SDR supports none of the sample rates "
                              + ", ".join(map(str, PREFERRED_RATES)))
        return max(rates)

    def tune(self, freq):
        from SoapySDR import SOAPY_SDR_RX
        t0 = time.monotonic()
        self.dev.setFrequency(SOAPY_SDR_RX, 0, "RF", float(freq))
        took = time.monotonic() - t0
        if took > 0.1:
            log.warning("retune to %.3f MHz took %.0f ms", freq / 1e6, took * 1000)
        self.center = freq
        # Throw away samples that were captured before/while retuning.
        self.read(int(self.settle_s * self.sample_rate))

    def read(self, n):
        out = np.empty(n, np.complex64)
        got = 0
        failures = 0
        while got < n:
            want = min(n - got, len(self._buf))
            sr = self.dev.readStream(self.stream, [self._buf], want, timeoutUs=500_000)
            if sr.ret > 0:
                out[got:got + sr.ret] = self._buf[:sr.ret]
                got += sr.ret
                failures = 0
            elif sr.ret == -4:          # SOAPY_SDR_OVERFLOW: samples dropped, carry on
                continue
            else:
                failures += 1
                if failures > 10:
                    raise SourceError(f"SDR read failed ({sr.ret}); device unplugged?")
        return out

    def close(self):
        if self.dev is not None:
            try:
                if self.stream is not None:
                    self.dev.deactivateStream(self.stream)
                    self.dev.closeStream(self.stream)
            finally:
                self.dev = None
                self.stream = None


class SimulatedSource:
    """Noise plus random NFM 'transmissions' on the configured channels.

    Lets the whole app be tested without hardware: SDR_DEVICE=driver=sim
    """

    def __init__(self, get_frequencies, sample_rate=256_000, settle_s=0.03):
        self.get_frequencies = get_frequencies
        self.sample_rate = sample_rate
        self.settle_s = settle_s
        self.name = "Simulator"
        self.serial = ""
        self.NOISE_AMP = 0.01
        self.freq_ranges = [(24e6, 1766e6)]
        self.center = 0
        self._t = 0             # global sample counter
        self._tx = {}           # freq -> transmission state
        self._wall = None

    def open(self):
        self._wall = time.monotonic()
        self._t = 0
        # Generating Gaussian noise live is too slow on a Pi; reuse a buffer.
        n = 1 << 18
        self._noise = ((np.random.standard_normal(n) + 1j * np.random.standard_normal(n))
                       * (self.NOISE_AMP / math.sqrt(2))).astype(np.complex64)

    def close(self):
        pass

    def tune(self, freq):
        self.center = freq
        self.read(int(self.settle_s * self.sample_rate))

    def _schedule(self, now):
        freqs = set(self.get_frequencies())
        for f in list(self._tx):
            if f not in freqs:
                del self._tx[f]
        for f in freqs:
            st = self._tx.get(f)
            if st is None:
                st = self._tx[f] = {"on": False, "until": now + random.uniform(1, 15)}
            if now >= st["until"]:
                if st["on"]:
                    st.update(on=False, until=now + random.uniform(5, 25))
                else:
                    st.update(on=True, until=now + random.uniform(2, 7),
                              snr=random.uniform(8, 30), pitch=random.uniform(180, 320),
                              phase=0.0)

    def read(self, n):
        fs = self.sample_rate
        now = self._t / fs
        self._schedule(now)
        noise_amp = self.NOISE_AMP
        start = random.randrange(len(self._noise) - n) if n < len(self._noise) else 0
        x = np.resize(self._noise[start:], n).copy()
        t = None
        for f, st in self._tx.items():
            off = f - self.center
            if not st["on"] or abs(off) > fs / 2:
                continue
            if t is None:
                t = (self._t + np.arange(n)) / fs
            # "Voice": a buzzy tone with a syllable-rate envelope, 2.5 kHz deviation.
            env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t) ** 2
            msg = env * np.sign(np.sin(2 * np.pi * st["pitch"] * t)) * 0.6 \
                + 0.4 * np.sin(2 * np.pi * 2.2 * st["pitch"] * t)
            dphi = 2 * np.pi * (off + 2_500 * msg) / fs
            ph = st["phase"] + np.cumsum(dphi)
            st["phase"] = float(ph[-1] % (2 * np.pi))
            # SNR measured in the ~10 kHz the signal occupies.
            amp = noise_amp * 10 ** (st["snr"] / 20) * math.sqrt(10_000 / fs)
            x += amp * np.exp(1j * ph)
        self._t += n
        # Pace like real hardware.
        ahead = self._wall + self._t / fs - time.monotonic()
        if ahead > 0:
            time.sleep(ahead)
        return x.astype(np.complex64)


def make_source(device_args, gain, ppm, get_frequencies):
    if device_args.strip().lower() in ("driver=sim", "sim"):
        return SimulatedSource(get_frequencies)
    return SoapySource(device_args, gain=gain, ppm=ppm)
