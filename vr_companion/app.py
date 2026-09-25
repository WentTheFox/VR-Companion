import sys

from PySide6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .audio.engine import NoiseEngine
from .config import load_config, save_config
from .ui.main_window import MainWindow


def make_tray_icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setBrush(QColor(100, 200, 255))
    p.setPen(QColor(100, 200, 255))
    p.drawEllipse(8, 8, 48, 48)
    p.end()
    return QIcon(pix)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    cfg = load_config()
    engine = NoiseEngine(
        volume_pct=cfg["audio"]["volume_pct"],
        device_match=cfg["audio"]["device_match"],
    )
    engine.enabled = cfg["audio"]["enabled"]

    window = MainWindow(engine, cfg, save_config)

    tray = QSystemTrayIcon(make_tray_icon(), app)
    tray.setToolTip("VR Companion")
    menu = QMenu()
    show_action = QAction("Show window")
    show_action.triggered.connect(window.show)
    menu.addAction(show_action)
    quit_action = QAction("Quit")

    def do_quit():
        engine.shutdown()
        app.quit()

    quit_action.triggered.connect(do_quit)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: window.show() if reason == QSystemTrayIcon.Trigger else None)
    tray.show()

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
