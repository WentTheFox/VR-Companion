"""Live compositor frame-timing graph, fed by the backend's service log.

Hand-painted rather than using pyqtgraph/matplotlib to avoid pulling in a
graphing dependency for one rolling bar chart."""
from collections import deque

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget
)

from ..perf.frame_log import FrameTimingTailer

HISTORY_LEN = 120   # samples kept on screen (Monado prints roughly one per second)
STAGES = ["cpu", "draw", "submit", "gpu", "gpu_delay", "total_frame"]
GRAPH_STAGE = "total_frame"
# The Index can run at any of these and the log doesn't say which one is
# active, so the user picks the frame budget to compare against.
REFRESH_RATES_HZ = [80, 90, 120, 144]
DEFAULT_REFRESH_HZ = 90

BG = QColor(24, 26, 30)
GRID = QColor(60, 64, 72)
BAR_MEDIAN = QColor(80, 180, 110)
BAR_WORST = QColor(80, 180, 110, 70)
BAR_OVER = QColor(220, 90, 70)
BUDGET = QColor(230, 190, 80)
TEXT = QColor(200, 204, 210)


class FrameGraph(QWidget):
    """Rolling bar graph: one bar per sample, solid to the median, faded
    up to the worst frame of that interval -- similar to SteamVR's view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.samples = deque(maxlen=HISTORY_LEN)
        self.budget_ms = 1000 / DEFAULT_REFRESH_HZ
        self.budget_label = f"{DEFAULT_REFRESH_HZ} Hz"
        self.setMinimumHeight(160)

    def set_refresh_rate(self, hz):
        self.budget_ms = 1000 / hz
        self.budget_label = f"{hz} Hz"
        self.update()

    def add_samples(self, samples):
        self.samples.extend(samples)
        self.update()

    def clear(self):
        self.samples.clear()
        self.update()

    def _scale_ms(self):
        peak = max((s.worst_ms.get(GRAPH_STAGE, 0.0) for s in self.samples), default=0.0)
        # Round up to a "nice" ceiling so the axis doesn't jitter every sample.
        for ceiling in (0.5, 1, 2, 5, 10, 15, 20, 30, 50, 100):
            if peak <= ceiling * 0.9:
                return ceiling
        return peak * 1.1

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, BG)

        left, top, bottom = 48, 8, 8
        plot = QRectF(left, top, w - left - 8, h - top - bottom)
        scale = self._scale_ms()

        def y_for(ms):
            return plot.bottom() - min(ms / scale, 1.0) * plot.height()

        # Grid + axis labels at 0, 1/2, full scale.
        p.setPen(QPen(GRID, 1))
        for frac in (0.0, 0.5, 1.0):
            y = plot.bottom() - frac * plot.height()
            p.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            p.setPen(TEXT)
            p.drawText(QRectF(0, y - 8, left - 6, 16), Qt.AlignRight | Qt.AlignVCenter,
                       f"{scale * frac:g}ms")
            p.setPen(QPen(GRID, 1))

        bar_w = plot.width() / HISTORY_LEN
        # Right-align so the newest sample is always at the right edge.
        x0 = plot.right() - len(self.samples) * bar_w
        for i, s in enumerate(self.samples):
            x = x0 + i * bar_w
            med = s.median_ms.get(GRAPH_STAGE, 0.0)
            worst = s.worst_ms.get(GRAPH_STAGE, med)
            bw = max(bar_w - 1, 1)
            p.fillRect(QRectF(x, y_for(worst), bw, plot.bottom() - y_for(worst)), BAR_WORST)
            color = BAR_OVER if med > self.budget_ms else BAR_MEDIAN
            p.fillRect(QRectF(x, y_for(med), bw, plot.bottom() - y_for(med)), color)

        # Budget line only once the scale reaches it -- otherwise it would pin
        # to the top edge and read as if frames were right at the limit.
        if self.budget_ms <= scale:
            y = y_for(self.budget_ms)
            p.setPen(QPen(BUDGET, 1, Qt.DashLine))
            p.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            p.drawText(QRectF(plot.left() + 4, y - 16, 120, 16), Qt.AlignLeft | Qt.AlignBottom,
                       f"{self.budget_label} budget")

        if not self.samples:
            p.setPen(TEXT)
            p.drawText(plot, Qt.AlignCenter, "Waiting for frame timing data...")
        p.end()


class PerformanceTab(QWidget):
    def __init__(self, adapter, parent=None):
        super().__init__(parent)
        self.adapter = adapter
        self.tailer = None

        layout = QVBoxLayout(self)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        # Page 0: unsupported / no log yet.
        self.message_label = QLabel("")
        self.message_label.setAlignment(Qt.AlignCenter)
        self.message_label.setWordWrap(True)
        self.stack.addWidget(self.message_label)

        # Page 1: live graph + numbers.
        live = QWidget()
        live_layout = QVBoxLayout(live)
        live_layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.addWidget(QLabel(f"Compositor {GRAPH_STAGE} (bar = median, faded = worst):"))
        header.addStretch(1)
        header.addWidget(QLabel("Budget:"))
        self.rate_combo = QComboBox()
        for hz in REFRESH_RATES_HZ:
            self.rate_combo.addItem(f"{hz} Hz", hz)
        self.rate_combo.setCurrentIndex(REFRESH_RATES_HZ.index(DEFAULT_REFRESH_HZ))
        self.rate_combo.currentIndexChanged.connect(
            lambda _: self.graph.set_refresh_rate(self.rate_combo.currentData()))
        header.addWidget(self.rate_combo)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear)
        header.addWidget(clear_btn)
        live_layout.addLayout(header)

        self.graph = FrameGraph()
        live_layout.addWidget(self.graph, stretch=1)

        self.table = QTableWidget(len(STAGES), 3)
        self.table.setHorizontalHeaderLabels(["Median", "Mean", "Worst"])
        self.table.setVerticalHeaderLabels(STAGES)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setFixedHeight(
            self.table.horizontalHeader().sizeHint().height()
            + self.table.verticalHeader().length()
            + 2 * self.table.frameWidth()
        )
        live_layout.addWidget(self.table)
        self.stack.addWidget(live)

        self._show_message("")

    def set_adapter(self, adapter):
        self.adapter = adapter
        self.tailer = None
        self._clear()

    def on_service_restarted(self, ok):
        # The log is truncated on restart; old samples belong to the previous
        # run. Also drop the tailer: if the new log already grew past our old
        # read offset, its own truncation check can't notice.
        if ok:
            self.tailer = None
            self._clear()

    def _clear(self):
        self.graph.clear()
        for row in range(len(STAGES)):
            for col in range(3):
                self.table.setItem(row, col, QTableWidgetItem("--"))

    def _show_message(self, text):
        self.message_label.setText(text)
        self.stack.setCurrentIndex(0)

    def tick(self):
        if not self.adapter.supports_frame_timing():
            self.tailer = None
            self._show_message(f"Frame timing is not available for the {self.adapter.name} backend.")
            return

        path = self.adapter.get_frame_timing_log_path()
        if path is None:
            self.tailer = None
            self._show_message(
                "No frame timing log yet.\n\n"
                f"Use \"Restart service\" on the Devices tab so {self.adapter.name} is "
                "launched by VR Companion with live stats logging enabled."
            )
            return

        if self.tailer is None or self.tailer.path != path:
            self.tailer = FrameTimingTailer(path)
        self.stack.setCurrentIndex(1)

        samples = self.tailer.poll_new_samples()
        if not samples:
            return
        self.graph.add_samples(samples)
        latest = samples[-1]
        for row, stage in enumerate(STAGES):
            for col, values in enumerate((latest.median_ms, latest.mean_ms, latest.worst_ms)):
                v = values.get(stage)
                self.table.setItem(row, col, QTableWidgetItem("--" if v is None else f"{v:.3f} ms"))
