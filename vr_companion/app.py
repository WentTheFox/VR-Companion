import sys
from pathlib import Path

from PySide6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from .audio.engine import NoiseEngine
from .config import load_config, save_config
from .ui.devices_tab import vr_app_clients
from .ui.main_window import MainWindow

ICON_PATH = Path(__file__).parent / "assets" / "icon.svg"


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
    app.setApplicationName("VR Companion")
    # Wayland app_id: lets the desktop match windows to vr-companion.desktop
    # (taskbar icon/grouping) instead of showing a generic "python3".
    app.setDesktopFileName("vr-companion")
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    app.setQuitOnLastWindowClosed(False)

    cfg = load_config()
    engine = NoiseEngine(
        volume_pct=cfg["audio"]["volume_pct"],
        device_match=cfg["audio"]["device_match"],
    )
    engine.enabled = cfg["audio"]["enabled"]

    window = MainWindow(engine, cfg, save_config)
    engine.start_ticker()

    tray = QSystemTrayIcon(make_tray_icon(), app)
    tray.setToolTip("VR Companion")
    menu = QMenu()
    show_action = QAction("Show window")
    show_action.triggered.connect(window.show)
    menu.addAction(show_action)
    quit_action = QAction("Quit")

    def do_quit():
        devices = window.devices_tab
        if devices.adapter.owns_running_service():
            apps = vr_app_clients(devices.last_snapshot)
            msg = f"Quitting also stops the {devices.adapter.name} service this app started."
            if apps:
                msg += (
                    "\n\nThese VR apps are connected and will lose their session "
                    "(most likely crash):\n" + "\n".join(f"  • {c.name}" for c in apps)
                )
            window.show()
            window.raise_()
            if QMessageBox.question(window, "Quit VR Companion", msg + "\n\nQuit anyway?") != QMessageBox.Yes:
                return
        window.shutdown()
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
