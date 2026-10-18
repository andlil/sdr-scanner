"""IMA ADPCM encoder (4 bits per sample) for audio over the WebSocket.

Each chunk is self-contained: a 4-byte header with the encoder state
(predictor int16 little-endian, step index uint8, one zero byte) followed by
the samples, two per byte, low nibble first. A listener can therefore start
decoding at any chunk. The browser decoder is in web/app.js.
"""

import numpy as np

INDEX_TABLE = (-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8)
STEP_TABLE = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
)


class AdpcmEncoder:
    def __init__(self):
        self.predictor = 0
        self.index = 0

    def encode(self, audio):
        """float32 audio in [-1, 1] -> one chunk (header + packed nibbles)."""
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tolist()
        if len(pcm) % 2:
            pcm.append(pcm[-1])
        pred, idx = self.predictor, self.index
        header = bytes((pred & 0xFF, (pred >> 8) & 0xFF, idx, 0))
        codes = bytearray(len(pcm))
        step_table, index_table = STEP_TABLE, INDEX_TABLE
        for i, sample in enumerate(pcm):
            step = step_table[idx]
            diff = sample - pred
            code = 0
            if diff < 0:
                code = 8
                diff = -diff
            vpdiff = step >> 3
            if diff >= step:
                code |= 4
                diff -= step
                vpdiff += step
            step >>= 1
            if diff >= step:
                code |= 2
                diff -= step
                vpdiff += step
            step >>= 1
            if diff >= step:
                code |= 1
                vpdiff += step
            pred = pred - vpdiff if code & 8 else pred + vpdiff
            if pred > 32767:
                pred = 32767
            elif pred < -32768:
                pred = -32768
            idx += index_table[code]
            if idx < 0:
                idx = 0
            elif idx > 88:
                idx = 88
            codes[i] = code
        self.predictor, self.index = pred, idx
        c = np.frombuffer(bytes(codes), np.uint8)
        return header + (c[0::2] | (c[1::2] << 4)).astype(np.uint8).tobytes()


def decode(chunk):
    """Reference decoder (the browser has its own); used for testing."""
    pred = int.from_bytes(chunk[0:2], "little", signed=True)
    idx = chunk[2]
    out = []
    for byte in chunk[4:]:
        for code in (byte & 0x0F, byte >> 4):
            step = STEP_TABLE[idx]
            vpdiff = step >> 3
            if code & 4:
                vpdiff += step
            if code & 2:
                vpdiff += step >> 1
            if code & 1:
                vpdiff += step >> 2
            pred = pred - vpdiff if code & 8 else pred + vpdiff
            pred = max(-32768, min(32767, pred))
            idx = max(0, min(88, idx + INDEX_TABLE[code]))
            out.append(pred)
    return np.array(out, np.float32) / 32767
