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

        # Devices poll (network/IPC-ish, a bit heavier): every 2s.
        self.devices_timer = QTimer(self)
        self.devices_timer.timeout.connect(self.devices_tab.refresh)
        self.devices_timer.start(2000)

        # Audio engine tick (cheap presence check): every 2s, independent of
        # whether the window is even open.
        self.audio_timer = QTimer(self)
        self.audio_timer.timeout.connect(self._tick_audio)
        self.audio_timer.start(2000)

        # Frame timing log tail: cheap incremental read, every 500ms.
        self.perf_timer = QTimer(self)
        self.perf_timer.timeout.connect(self.performance_tab.tick)
        self.perf_timer.start(500)

        self.devices_tab.refresh()
        self.audio_tab.refresh_status()
        self.performance_tab.tick()

    def _tick_audio(self):
        self.engine.tick()
        self.audio_tab.refresh_status()

    def closeEvent(self, event):
        # Hide to tray instead of quitting -- the audio engine keeps running.
        event.ignore()
        self.hide()
