from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QTabWidget

from .audio_tab import AudioTab
from .devices_tab import DevicesTab


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

        tabs = QTabWidget()
        tabs.addTab(self.devices_tab, "Devices")
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

        self.devices_tab.refresh()
        self.audio_tab.refresh_status()

    def _tick_audio(self):
        self.engine.tick()
        self.audio_tab.refresh_status()

    def closeEvent(self, event):
        # Hide to tray instead of quitting -- the audio engine keeps running.
        event.ignore()
        self.hide()
