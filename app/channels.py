"""Channel list, persisted as JSON in the data directory."""

import json
import os
import threading
import uuid

# First-start channel list: marine VHF (25 kHz raster) and PMR446.
MARINE_VHF = {9: 156_450_000, 11: 156_550_000, 16: 156_800_000, 71: 156_575_000, 73: 156_675_000}
DEFAULT_CHANNELS = sorted(
    [{"name": f"Marine {ch}", "freq": f, "bw": 25_000, "enabled": True}
     for ch, f in MARINE_VHF.items()]
    + [{"name": f"PMR {i + 1}", "freq": 446_006_250 + i * 12_500, "bw": 12_500, "enabled": True}
       for i in range(8)],
    key=lambda c: c["freq"],
)

BANDWIDTHS = (12_500, 25_000)
# Per-channel squelch fine-tuning, dB relative to the automatic level.
SQUELCH_MIN, SQUELCH_MAX = -4, 12


class ChannelError(ValueError):
    pass


def _clean(data, partial=False):
    out = {}
    if "name" in data or not partial:
        name = str(data.get("name", "")).strip()
        if not name:
            raise ChannelError("channel name is required")
        out["name"] = name[:40]
    if "freq" in data or not partial:
        try:
            freq = int(round(float(data.get("freq"))))
        except (TypeError, ValueError):
            raise ChannelError("frequency must be a number in Hz") from None
        if not 1_000_000 <= freq <= 6_000_000_000:
            raise ChannelError("frequency must be between 1 MHz and 6 GHz")
        out["freq"] = freq
    if "bw" in data or not partial:
        bw = int(data.get("bw", 12_500))
        if bw not in BANDWIDTHS:
            raise ChannelError("bandwidth must be 12500 or 25000")
        out["bw"] = bw
    if "enabled" in data or not partial:
        out["enabled"] = bool(data.get("enabled", True))
    if "squelch" in data or not partial:
        try:
            sq = int(round(float(data.get("squelch", 0))))
        except (TypeError, ValueError):
            raise ChannelError("squelch must be a number of dB") from None
        if not SQUELCH_MIN <= sq <= SQUELCH_MAX:
            raise ChannelError(f"squelch must be between {SQUELCH_MIN} and +{SQUELCH_MAX} dB")
        out["squelch"] = sq
    return out


class ChannelStore:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "channels.json")
        self._lock = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)
        if os.path.exists(self.path):
            with open(self.path) as f:
                self._channels = json.load(f)["channels"]
        else:
            self._channels = [{"id": uuid.uuid4().hex[:8], **c} for c in DEFAULT_CHANNELS]
            self._save()

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"channels": self._channels}, f, indent=2)
        os.replace(tmp, self.path)

    def all(self):
        with self._lock:
            return [dict(c) for c in self._channels]

    def add(self, data):
        ch = {"id": uuid.uuid4().hex[:8], **_clean(data)}
        with self._lock:
            self._channels.append(ch)
            self._channels.sort(key=lambda c: c["freq"])
            self._save()
        return ch

    def update(self, ch_id, data):
        changes = _clean(data, partial=True)
        with self._lock:
            for ch in self._channels:
                if ch["id"] == ch_id:
                    ch.update(changes)
                    self._channels.sort(key=lambda c: c["freq"])
                    self._save()
                    return dict(ch)
        raise ChannelError("no such channel")

    def delete(self, ch_id):
        with self._lock:
            before = len(self._channels)
            self._channels = [c for c in self._channels if c["id"] != ch_id]
            if len(self._channels) == before:
                raise ChannelError("no such channel")
            self._save()
