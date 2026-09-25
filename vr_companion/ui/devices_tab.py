from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QBrush, QFont, QPalette
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget
)

from ..adapters import ALL_ADAPTERS, make_adapter
from ..adapters.base import DeviceKind

# libmonado status-only connections (this app's own poll, other monitoring
# tools) show up as clients too, but aren't VR apps and have no session to lose.
STATUS_CLIENT_NAMES = {"libmonado"}


def vr_app_clients(snap):
    """Clients in a snapshot that are actual VR apps, not status monitors."""
    if snap is None:
        return []
    return [c for c in snap.clients if c.name not in STATUS_CLIENT_NAMES]


KIND_LABELS = {
    DeviceKind.HMD: "HMD",
    DeviceKind.CONTROLLER: "Controller",
    DeviceKind.TRACKER: "Tracker",
    DeviceKind.BASE_STATION: "Base Station",
    DeviceKind.OTHER: "Other",
}


class RestartWorker(QObject):
    """Runs adapter.restart_service() off the UI thread -- it blocks for up
    to several seconds while the old service process shuts down."""
    finished = Signal(bool)

    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter

    def run(self):
        try:
            ok = self.adapter.restart_service()
        except Exception as e:
            print(f"vr-companion: restart_service() raised: {e}")
            ok = False
        self.finished.emit(ok)


class DevicesTab(QWidget):
    adapter_changed = Signal(object)   # new VRAdapter
    service_restarted = Signal(bool)   # whether the relaunch succeeded

    def __init__(self, cfg, save_cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.save_cfg = save_cfg
        self.adapter = make_adapter(cfg["backend"])
        self._restart_thread = None
        self._restart_worker = None
        self.last_snapshot = None

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Backend:"))
        self.backend_combo = QComboBox()
        for key in ALL_ADAPTERS:
            self.backend_combo.addItem(key)
        self.backend_combo.setCurrentText(cfg["backend"])
        self.backend_combo.currentTextChanged.connect(self._on_backend_changed)
        top.addWidget(self.backend_combo)
        self.status_label = QLabel("")
        top.addWidget(self.status_label, stretch=1)
        self.restart_btn = QPushButton("Restart service")
        self.restart_btn.setToolTip(
            "Stops and relaunches the VR service so newly powered-on devices are picked up.\n"
            "Any connected VR app (game) will lose its session and likely crash."
        )
        self.restart_btn.clicked.connect(self._on_restart_clicked)
        top.addWidget(self.restart_btn)
        layout.addLayout(top)

        # Per-backend service options, rebuilt from adapter.service_options().
        self.options_box = QWidget()
        self.options_layout = QHBoxLayout(self.options_box)
        self.options_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.options_box)

        self.warnings_label = QLabel("")
        self.warnings_label.setWordWrap(True)
        self.warnings_label.hide()
        layout.addWidget(self.warnings_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Device", "Kind", "Role", "Tracking", "Battery"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)


        self.clients_label = QLabel("Clients: (none)")
        layout.addWidget(self.clients_label)

        self._update_restart_visibility()
        self._rebuild_service_options()

    def _update_restart_visibility(self):
        self.restart_btn.setVisible(self.adapter.supports_service_restart())

    def _rebuild_service_options(self):
        while self.options_layout.count():
            w = self.options_layout.takeAt(0).widget()
            if w:
                w.deleteLater()
        options = self.adapter.service_options()
        saved = self.cfg.setdefault("service_options", {}).get(self.cfg["backend"], {})
        for opt in options:
            value = saved.get(opt.key, opt.default)
            self.adapter.set_service_option(opt.key, value)
            label = QLabel(f"{opt.label}:")
            label.setToolTip(opt.tooltip)
            spin = QSpinBox()
            spin.setRange(opt.minimum, opt.maximum)
            spin.setSingleStep(opt.step)
            spin.setSuffix(opt.suffix)
            spin.setValue(value)
            spin.setToolTip(opt.tooltip + f"\nDefault: {opt.default}{opt.suffix}")
            spin.valueChanged.connect(lambda v, key=opt.key: self._on_option_changed(key, v))
            self.options_layout.addWidget(label)
            self.options_layout.addWidget(spin)
        if options:
            self.options_layout.addWidget(QLabel("(applies on next restart)"))
        self.options_layout.addStretch(1)
        self.options_box.setVisible(bool(options))

    def _on_option_changed(self, key, value):
        self.adapter.set_service_option(key, value)
        self.cfg["service_options"].setdefault(self.cfg["backend"], {})[key] = value
        self.save_cfg(self.cfg)

    def _on_backend_changed(self, key):
        self.adapter.disconnect()
        self.cfg["backend"] = key
        self.save_cfg(self.cfg)
        self.adapter = make_adapter(key)
        self._update_restart_visibility()
        self._rebuild_service_options()
        self.adapter_changed.emit(self.adapter)

    def _on_restart_clicked(self):
        apps = vr_app_clients(self.last_snapshot)
        msg = f"Restart the {self.adapter.name} service?\n\n"
        if apps:
            msg += (
                "These VR apps are connected and will lose their session "
                "(most likely crash):\n" + "\n".join(f"  • {c.name}" for c in apps)
            )
        else:
            msg += "No VR apps are connected right now."
        if QMessageBox.question(self, "Restart service", msg) != QMessageBox.Yes:
            return

        self.restart_btn.setEnabled(False)
        self.backend_combo.setEnabled(False)
        self.options_box.setEnabled(False)
        self.status_label.setText(f"⟳ Restarting {self.adapter.name} service...")

        self._restart_thread = QThread(self)
        self._restart_worker = RestartWorker(self.adapter)
        self._restart_worker.moveToThread(self._restart_thread)
        self._restart_thread.started.connect(self._restart_worker.run)
        self._restart_worker.finished.connect(self._on_restart_finished)
        self._restart_worker.finished.connect(self._restart_thread.quit)
        self._restart_thread.finished.connect(self._restart_worker.deleteLater)
        self._restart_thread.finished.connect(self._restart_thread.deleteLater)
        self._restart_thread.start()

    def _on_restart_finished(self, ok):
        self._restart_thread = None
        self._restart_worker = None
        self.restart_btn.setEnabled(True)
        self.backend_combo.setEnabled(True)
        self.options_box.setEnabled(True)
        self.refresh()
        if not ok:
            self.status_label.setText(f"⚠ {self.adapter.name}: service restart failed (see terminal output)")
        self.service_restarted.emit(ok)

    def refresh(self):
        if self._restart_worker is not None:
            # The worker thread owns the adapter while restarting -- polling
            # now would race it and reconnect to the service being killed.
            return
        snap = self.adapter.poll()
        self.last_snapshot = snap

        if not snap.connected:
            self.status_label.setText(f"⚠ {snap.backend_name}: {snap.error or 'not connected'}")
        else:
            self.status_label.setText(f"✓ {snap.backend_name} connected")

        self.table.setRowCount(len(snap.devices))
        for row, dev in enumerate(snap.devices):
            if dev.placeholder:
                cells = [dev.name, "?", "", "Not added", "--"]
            else:
                tracking = "OK" if dev.tracking_ok else ("--" if dev.tracking_ok is None else "Lost")
                if dev.battery_percent is not None:
                    batt = f"{dev.battery_percent:.0f}%" + (" (charging)" if dev.charging else "")
                else:
                    batt = "--"
                cells = [dev.name, KIND_LABELS.get(dev.kind, "?"), dev.role or "", tracking, batt]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if dev.placeholder:
                    font = QFont(item.font())
                    font.setItalic(True)
                    item.setFont(font)
                    item.setForeground(QBrush(self.palette().color(QPalette.Disabled, QPalette.Text)))
                    item.setToolTip(dev.note or "")
                self.table.setItem(row, col, item)
        self.warnings_label.setText("\n".join(f"⚠ {w}" for w in snap.warnings))
        self.warnings_label.setVisible(bool(snap.warnings))

        if snap.clients:
            parts = []
            for c in snap.clients:
                flags = []
                if c.active:
                    flags.append("active")
                if c.focused:
                    flags.append("focused")
                parts.append(f"{c.name} ({', '.join(flags) or 'idle'})")
            self.clients_label.setText("Clients: " + "; ".join(parts))
        else:
            self.clients_label.setText("Clients: (none)")
