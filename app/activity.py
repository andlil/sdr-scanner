"""How busy each channel has been: share of time its squelch was open.

Kept in clock-aligned buckets (5 minutes, the last hour). Each measurement
counts for the wall-clock time since the channel's previous measurement
(capped at MAX_GAP_S). That weighting matters: while scanning, a channel is
measured for ~24 ms per pass, while listening it is measured continuously;
counting measurement time instead would make busy channels look far busier
than they are. Time the scanner spent away from a channel beyond the cap
(e.g. listening elsewhere) counts as not observed, and the busy share is taken
over observed time only. A bucket with no observations has no value.
"""

import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)


class ActivityLog:
    MAX_GAP_S = 2.0         # one measurement stands for at most this much time

    def __init__(self, path=None, bucket_s=300, buckets=12):
        self.path = path
        self.bucket_s = bucket_s
        self.buckets = buckets
        self._lock = threading.Lock()
        self._data = {}             # channel id -> {bucket number: [observed_s, busy_s]}
        self._last_seen = {}        # channel id -> time of its previous measurement
        self._load()

    def record(self, ch_id, busy, now=None):
        """One measurement of a channel: was its squelch open?"""
        now = time.time() if now is None else now
        b = int(now // self.bucket_s)
        with self._lock:
            prev = self._last_seen.get(ch_id)
            self._last_seen[ch_id] = now
            if prev is None:
                return                          # need two measurements to know the span
            seconds = min(max(now - prev, 0.0), self.MAX_GAP_S)
            slot = self._data.setdefault(ch_id, {}).setdefault(b, [0.0, 0.0])
            slot[0] += seconds
            if busy:
                slot[1] += seconds

    def snapshot(self, ch_ids, now=None):
        """Busy share per bucket for the last hour, oldest first (None = no data)."""
        last = int((time.time() if now is None else now) // self.bucket_s)
        first = last - self.buckets + 1
        out, hour = {}, {}
        with self._lock:
            for cid, per in list(self._data.items()):
                for b in [b for b in per if b < first]:
                    del per[b]                  # older than the window
                if not per:
                    del self._data[cid]
            for cid in ch_ids:
                per = self._data.get(cid, {})
                row = []
                observed = busy = 0.0
                for b in range(first, last + 1):
                    o, k = per.get(b, (0.0, 0.0))
                    row.append(round(k / o, 3) if o > 0 else None)
                    observed += o
                    busy += k
                out[cid] = row
                hour[cid] = round(busy / observed, 3) if observed > 0 else None
        return {"type": "activity", "bucket_s": self.bucket_s,
                "start": first * self.bucket_s, "channels": out, "hour": hour}

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                raw = json.load(f)
            self._data = {cid: {int(b): list(v) for b, v in per.items()} for cid, per in raw.items()}
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable %s: %s", self.path, e)

    def save(self):
        if not self.path:
            return
        with self._lock:
            raw = {cid: {str(b): v for b, v in per.items()} for cid, per in self._data.items()}
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(raw, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("could not save %s: %s", self.path, e)
