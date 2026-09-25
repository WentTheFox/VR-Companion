import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QTabWidget

from .audio_tab import AudioTab
from .devices_tab import DevicesTab
from .performance_tab import PerformanceTab


class MainWindow(QMainWindow):
    def __init__(self, engine, cfg, save_cfg):
        super().__init__()
        self.engine = engine
        self.cfg = cfg
        self.save_cfg = save_cfg

        self.setWindowTitle("VR Companion")
        self.resize(640, 420)

        self.devices_tab = DevicesTab(cfg, save_cfg)
        self.audio_tab = AudioTab(engine, cfg, save_cfg)
        self.performance_tab = PerformanceTab(self.devices_tab.adapter)
        self.devices_tab.adapter_changed.connect(self.performance_tab.set_adapter)
        self.devices_tab.service_restarted.connect(self.performance_tab.on_service_restarted)

        tabs = QTabWidget()
        tabs.addTab(self.devices_tab, "Devices")
        tabs.addTab(self.performance_tab, "Performance")
        tabs.addTab(self.audio_tab, "Audio")
        self.setCentralWidget(tabs)

        # Devices poll: every 2s. Only *requests* a poll -- the actual adapter
        # call runs on DevicesTab's worker thread.
        self.devices_timer = QTimer(self)
        self.devices_timer.timeout.connect(self.devices_tab.refresh)
        self.devices_timer.start(2000)

        # The audio engine ticks on its own background thread (started in
        # app.py); this just keeps the status line current.
        self.audio_timer = QTimer(self)
        self.audio_timer.timeout.connect(self.audio_tab.refresh_status)
        self.audio_timer.start(1000)

        # Stall watchdog: anything blocking the UI thread shows up as a late
        # heartbeat. Cheap, and makes the next freeze diagnosable.
        self._last_beat = time.monotonic()
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.timeout.connect(self._heartbeat)
        self.heartbeat_timer.start(100)

        # Frame timing log tail: cheap incremental read, every 500ms.
        self.perf_timer = QTimer(self)
        self.perf_timer.timeout.connect(self.performance_tab.tick)
        self.perf_timer.start(500)

        self.devices_tab.refresh()
        self.audio_tab.refresh_status()
        self.performance_tab.tick()

    def _heartbeat(self):
        now = time.monotonic()
        stall_ms = (now - self._last_beat) * 1000 - 100
        if stall_ms > 300:
            print(f"vr-companion: UI thread stalled for {stall_ms:.0f} ms")
        self._last_beat = now

    def shutdown(self):
        self.devices_tab.shutdown()

    def closeEvent(self, event):
        # Hide to tray instead of quitting -- the audio engine keeps running.
        event.ignore()
        self.hide()
