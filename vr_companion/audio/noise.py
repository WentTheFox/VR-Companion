"""Pink noise generation and the dB-based volume dial, shared by the
standalone tray app's history and now this GUI's Audio tab.

Volume is a perceptual (dB) scale, not linear amplitude: hearing is roughly
logarithmic, so a linear 0.30 multiplier sounds much louder than "30%"
suggests. 0% is silent, 100% is full-scale (0dB).
"""
import numpy as np

MIN_DB = -50.0

# Slider ceiling: above 50% it's just loud static, not useful for a
# background masking noise.
VOLUME_MAX_PCT = 50


def pct_to_gain(pct: float) -> float:
    if pct <= 0:
        return 0.0
    db = MIN_DB + (pct / 100.0) * (0.0 - MIN_DB)
    return 10 ** (db / 20.0)


class PinkNoise:
    """Voss-McCartney pink noise generator, streamed in chunks."""

    def __init__(self, channels=2, num_rows=16):
        self.channels = channels
        self.num_rows = num_rows
        self.rows = np.zeros((num_rows, channels))
        self.counter = 0
        self.rng = np.random.default_rng()

    def next_chunk(self, n_frames: int) -> np.ndarray:
        out = np.zeros((n_frames, self.channels))
        for i in range(n_frames):
            self.counter += 1
            row_to_update = (self.counter & -self.counter).bit_length() - 1
            row_to_update %= self.num_rows
            self.rows[row_to_update] = self.rng.uniform(-1, 1, self.channels)
            out[i] = self.rows.sum(axis=0)
        out /= self.num_rows
        return out.astype(np.float32)
