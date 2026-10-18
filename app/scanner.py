"""The scanner engine: runs in its own thread, owns the SDR.

States
  stopped   SDR closed, nothing happens.
  scanning  hop between tuning windows until a channel's squelch opens, then
            listen to it until it has been quiet for `hang_s` seconds.
  paused    stay on the current (or chosen) channel; audio still squelched.
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .dsp import AUDIO_RATE, AutoSquelch, Channelizer, NfmDemodulator
from .sources import SourceError

log = logging.getLogger(__name__)

DC_GUARD_HZ = 15_000        # keep channels away from the SDR's DC spike
USABLE_FRACTION = 0.8       # of the sample rate (edges roll off)
LISTEN_BLOCKS = 4           # FFT blocks processed per iteration while listening


@dataclass
class Window:
    center: int
    channels: list = field(default_factory=list)


def plan_windows(channels, fs):
    """Group channels into as few tuning windows as possible."""
    span = USABLE_FRACTION * fs - 2 * DC_GUARD_HZ
    windows = []
    for ch in sorted(channels, key=lambda c: c["freq"]):
        if windows and ch["freq"] - windows[-1].channels[0]["freq"] <= span:
            windows[-1].channels.append(ch)
        else:
            windows.append(Window(0, [ch]))
    for w in windows:
        lo, hi = w.channels[0]["freq"], w.channels[-1]["freq"]
        center = (lo + hi) // 2
        # Nudge the centre so no channel sits on the DC spike.
        for nudge in range(0, int(fs * 0.1), 5_000):
            for c in (center + nudge, center - nudge):
                if all(abs(ch["freq"] - c) > DC_GUARD_HZ for ch in w.channels) and \
                        all(abs(ch["freq"] - c) < fs * USABLE_FRACTION / 2 for ch in w.channels):
                    w.center = c
                    break
            else:
                continue
            break
        else:
            w.center = center
    return windows


class Scanner:
    def __init__(self, store, make_source, on_audio, on_status,
                 hang_s=2.0, dwell_blocks=4):
        self.store = store
        self.make_source = make_source
        self.on_audio = on_audio
        self.on_status = on_status
        self.hang_s = hang_s
        self.dwell_blocks = dwell_blocks

        self.state = "stopped"
        self.error = None
        self.active = None          # channel dict currently tuned to / listened to
        self.squelch = {}           # channel id -> AutoSquelch
        self._sq_freq = {}          # channel id -> frequency the squelch learned on
        self.out_of_range = set()

        self._cmds = queue.Queue()
        self._source = None
        self._chz = None
        self._demod = NfmDemodulator()
        self._windows = []
        self._wi = 0
        self._last_open = 0.0
        self._noise = 1.0
        self._batches = 0
        self._audio_buf = []
        self._audio_len = 0
        self._last_status = 0.0
        self._status_dirty = True
        self._thread = threading.Thread(target=self._run, name="scanner", daemon=True)

    # ---- public API (any thread) -------------------------------------------

    def begin(self):
        self._thread.start()

    def command(self, action, **kw):
        self._cmds.put((action, kw))

    def shutdown(self):
        self._cmds.put(("shutdown", {}))
        self._thread.join(timeout=5)

    def snapshot(self):
        active = self.active
        sq = self.squelch.get(active["id"]) if active else None
        levels = {cid: {"snr": round(s.snr_db, 1), "open_db": round(s.open_db, 1), "open": s.is_open,
                        "offset": s.offset_db}
                  for cid, s in list(self.squelch.items())}
        src = self._source
        return {
            "type": "status",
            "state": self.state,
            "error": self.error,
            "device": src.name if src else None,
            "serial": src.serial if src else None,
            "sample_rate": src.sample_rate if src else None,
            "active": ({"id": active["id"], "name": active["name"], "freq": active["freq"]}
                       if active else None),
            "open": bool(sq and sq.is_open),
            "levels": levels,
            "out_of_range": sorted(self.out_of_range),
        }

    # ---- thread ------------------------------------------------------------

    def _run(self):
        self._replan()
        while True:
            try:
                if not self._handle_commands(block=self.state == "stopped" or self.error is not None):
                    break
                if self.state == "stopped":
                    continue
                if self._source is None:
                    self._open_source()
                    continue
                t0 = time.monotonic()
                if self.state == "paused" or self.active is not None:
                    self._listen_block()
                else:
                    self._scan_window()
                self._maybe_status()
                took = time.monotonic() - t0
                if took > 0.25:
                    log.warning("slow scanner iteration: %.0f ms", took * 1000)
            except SourceError as e:
                log.error("%s", e)
                self._fail(str(e))
            except Exception as e:      # keep the thread alive whatever happens
                log.exception("scanner error")
                self._fail(f"internal error: {e}")
        self._close_source()

    def _fail(self, msg):
        self._close_source()
        self.error = msg
        self.active = None
        self._emit_status()

    def _handle_commands(self, block):
        """Apply queued commands. Returns False on shutdown."""
        try:
            # While stopped or after an error, wait for a command (retry every 3 s).
            item = self._cmds.get(timeout=3.0) if block else self._cmds.get_nowait()
        except queue.Empty:
            if self.error is not None and self.state != "stopped":
                self.error = None       # retry opening the SDR
            return True
        while True:
            action, kw = item
            if action == "shutdown":
                return False
            self._apply(action, kw)
            try:
                item = self._cmds.get_nowait()
            except queue.Empty:
                return True

    def _apply(self, action, kw):
        if action == "start":
            self.state = "scanning"
            self.error = None
            if self.active and not self._is_open(self.active):
                self.active = None
        elif action == "pause":
            if self.state == "stopped":
                return
            if self.active is None:
                self.active = self._first_channel()
                self._tune_for(self.active)
            self.state = "paused" if self.active else "scanning"
        elif action == "hold":
            ch = next((c for c in self.store.all() if c["id"] == kw.get("id") and c["enabled"]), None)
            if ch is None or ch["id"] in self.out_of_range:
                return
            self.state = "paused"
            self.error = None
            self.active = ch
            self._tune_for(ch)
        elif action == "stop":
            self.state = "stopped"
            self.active = None
            self._close_source()
        elif action == "channels_changed":
            self._replan()
        self._status_dirty = True
        self._emit_status()

    def _is_open(self, ch):
        sq = self.squelch.get(ch["id"])
        return bool(sq and sq.is_open)

    def _first_channel(self):
        if self._windows:
            idx = (self._wi - 1) % len(self._windows)
            return self._windows[idx].channels[0]
        return None

    def _replan(self):
        channels = [c for c in self.store.all() if c["enabled"]]
        src = self._source
        if src is not None:
            ranges = src.freq_ranges
            ok = [c for c in channels if any(lo <= c["freq"] <= hi for lo, hi in ranges)]
            self.out_of_range = {c["id"] for c in channels} - {c["id"] for c in ok}
            channels = ok
            self._windows = plan_windows(channels, src.sample_rate)
        else:
            self._windows = []
        # Keep learned squelch state for channels whose frequency didn't change.
        old = self.squelch
        self.squelch = {}
        for c in channels:
            if c["id"] in old and self._sq_freq.get(c["id"]) == c["freq"]:
                self.squelch[c["id"]] = old[c["id"]]
            else:
                self.squelch[c["id"]] = AutoSquelch()
            self.squelch[c["id"]].offset_db = c.get("squelch", 0)
            self._sq_freq[c["id"]] = c["freq"]
        if self.active is not None:
            fresh = next((c for c in channels if c["id"] == self.active["id"]), None)
            retune = fresh is not None and (fresh["freq"], fresh["bw"]) != (self.active["freq"], self.active["bw"])
            self.active = fresh
            # Only reset the audio path if the channel itself moved: renaming
            # it or adjusting its squelch while listening shouldn't click.
            # (A changed window centre is picked up by _listen_block.)
            if retune and src is not None:
                self._tune_for(fresh)
        if self.active is None and self.state == "paused":
            self.state = "scanning"
        log.info("planned %d window(s) for %d channel(s)", len(self._windows), len(channels))

    def _open_source(self):
        src = self.make_source()
        src.open()
        self._source = src
        self._chz = Channelizer(src.sample_rate)
        self.error = None
        self._replan()
        if self.state == "paused" and self.active is None:
            self.active = self._first_channel()
        if self.active is not None:
            self._tune_for(self.active)
        self._emit_status()

    def _close_source(self):
        if self._source is not None:
            try:
                self._source.close()
            except Exception:
                log.exception("closing SDR")
            self._source = None
            self._chz = None

    def _window_of(self, ch):
        return next((w for w in self._windows if any(c["id"] == ch["id"] for c in w.channels)), None)

    def _tune_for(self, ch):
        if ch is None or self._source is None:
            return
        w = self._window_of(ch)
        if w is not None and self._source.center != w.center:
            self._tune(w.center)
        self._demod.reset()

    def _tune(self, center):
        self._source.tune(center)
        self._chz.reset()
        self._batches = 0

    # ---- scanning ------------------------------------------------------------

    def _scan_window(self):
        if not self._windows:
            self._maybe_status()
            time.sleep(0.2)
            return
        w = self._windows[self._wi % len(self._windows)]
        self._wi += 1
        if self._source.center != w.center:
            self._tune(w.center)
        chz = self._chz
        X = chz.spectra(self._source.read(self.dwell_blocks * chz.step))
        best, best_snr = None, -1e9
        for ch in w.channels:
            off = ch["freq"] - w.center
            power = chz.channel_power(X, off, ch["bw"])
            noise = chz.local_noise(X, off)
            sq = self.squelch[ch["id"]]
            if sq.update(power, noise) and sq.snr_db > best_snr:
                best, best_snr = ch, sq.snr_db
        if best is not None and not self._confirm(w, best):
            log.debug("unconfirmed opening on %s (%.1f dB)", best["name"], best_snr)
            best = None
        if best is not None:
            self.active = best
            self._last_open = time.monotonic()
            self._demod.reset()
            self._wi -= 1               # rescan this window first after the call ends
            log.info("signal on %s (%.1f dB)", best["name"], best_snr)
            self._emit_status()

    def _confirm(self, w, ch):
        """Second dwell without retuning: lock only if the signal is still there.

        Filters one-off transients (retune leftovers, impulse noise) that
        would otherwise make the scanner stop on an empty channel.
        """
        chz = self._chz
        X = chz.spectra(self._source.read(self.dwell_blocks * chz.step))
        off = ch["freq"] - w.center
        sq = self.squelch[ch["id"]]
        return sq.update(chz.channel_power(X, off, ch["bw"]), chz.local_noise(X, off))

    # ---- listening -------------------------------------------------------------

    def _listen_block(self):
        ch = self.active
        if ch is None:
            time.sleep(0.1)
            return
        w = self._window_of(ch)
        if w is None:
            self.active = None
            return
        if self._source.center != w.center:
            self._tune(w.center)
        chz = self._chz
        X = chz.spectra(self._source.read(LISTEN_BLOCKS * chz.step))
        off = ch["freq"] - w.center
        self._batches += 1
        if self._batches % 4 == 1:          # noise floor ~10x per second is plenty
            self._noise = chz.local_noise(X, off)
        sq = self.squelch[ch["id"]]
        was_open = sq.is_open
        is_open = sq.update(chz.channel_power(X, off, ch["bw"]), self._noise)
        audio = self._demod.process(chz.extract(X, off, ch["bw"]))
        if not is_open:
            audio = np.zeros_like(audio)
        elif not was_open:
            audio *= np.linspace(0, 1, len(audio), dtype=np.float32)   # soft open, no click
        self._push_audio(audio)

        now = time.monotonic()
        if is_open:
            self._last_open = now
        if is_open != was_open:
            self._emit_status()
        if self.state == "scanning" and not is_open and now - self._last_open > self.hang_s:
            self.active = None
            self._emit_status()

    def _push_audio(self, audio):
        self._audio_buf.append(audio)
        self._audio_len += len(audio)
        if self._audio_len >= AUDIO_RATE // 50:     # hand over ~20 ms at a time
            self.on_audio(np.concatenate(self._audio_buf))
            self._audio_buf, self._audio_len = [], 0

    # ---- status ----------------------------------------------------------------

    def _maybe_status(self):
        now = time.monotonic()
        if now - self._last_status >= 0.25:
            self._emit_status()

    def _emit_status(self):
        self._last_status = time.monotonic()
        self.on_status(self.snapshot())
