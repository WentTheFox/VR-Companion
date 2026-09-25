"""Incrementally tails a Monado service log for `U_PACING_LIVE_STATS=1`
blocks and turns them into structured samples for the performance graph.

Expected block shape (as actually emitted by monado-service):

     INFO [print_and_reset] Compositor frame timing:
                name          median            mean           worst
                 cpu         0.003ms         0.002ms         0.010ms
                draw         0.015ms         0.015ms         0.024ms
              submit         0.309ms         0.333ms         1.216ms
                 gpu         0.130ms         0.131ms         0.186ms
           gpu_delay         0.084ms         0.168ms         1.221ms
         total_frame         0.243ms         0.318ms         1.347ms
"""
import re
from dataclasses import dataclass, field

BLOCK_START = re.compile(r"Compositor frame timing:")
ROW = re.compile(r"^\s*(\w+)\s+([\d.]+)ms\s+([\d.]+)ms\s+([\d.]+)ms\s*$")
KNOWN_ROWS = {"cpu", "draw", "submit", "gpu", "gpu_delay", "total_frame"}


@dataclass
class FrameTimingSample:
    median_ms: dict = field(default_factory=dict)  # row name -> ms
    mean_ms: dict = field(default_factory=dict)
    worst_ms: dict = field(default_factory=dict)


class FrameTimingTailer:
    def __init__(self, path: str):
        self.path = path
        self._fh = None
        self._inode = None
        self._in_block = False
        self._pending = None

    def _ensure_open(self):
        import os
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return False
        truncated = self._fh is not None and st.st_size < self._fh.tell()
        if self._fh is None or self._inode != st.st_ino or truncated:
            # File changed (service restarted, log truncated/replaced) -- reopen
            # from the top. restart_service() truncates in place with "w", which
            # keeps the same inode, hence the size check too.
            if self._fh:
                self._fh.close()
            self._fh = open(self.path, "r", errors="replace")
            self._inode = st.st_ino
            self._in_block = False
            self._pending = None
        return True

    def poll_new_samples(self) -> list:
        """Returns any complete FrameTimingSample blocks that appeared since
        the last call. Safe to call frequently; never blocks."""
        if not self._ensure_open():
            return []

        samples = []
        while True:
            # readline() rather than iterating the file: iteration disables
            # tell(), which the truncation check above depends on.
            pos = self._fh.tell()
            line = self._fh.readline()
            if not line:
                break
            if not line.endswith("\n"):
                # Writer is mid-line -- rewind and pick it up whole next poll.
                self._fh.seek(pos)
                break
            if BLOCK_START.search(line):
                self._in_block = True
                self._pending = FrameTimingSample()
                continue
            if not self._in_block:
                continue
            m = ROW.match(line)
            if m:
                row, med, mean, worst = m.group(1), float(m.group(2)), float(m.group(3)), float(m.group(4))
                if row in KNOWN_ROWS:
                    self._pending.median_ms[row] = med
                    self._pending.mean_ms[row] = mean
                    self._pending.worst_ms[row] = worst
                if row == "total_frame":
                    # Last row in the block -- it's complete.
                    samples.append(self._pending)
                    self._in_block = False
                    self._pending = None
            elif line.strip() and "name" not in line and self._pending and not self._pending.median_ms:
                continue  # header row, ignore
        return samples
