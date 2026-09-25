from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
)

from ..adapters import ALL_ADAPTERS, make_adapter
from ..adapters.base import DeviceKind

KIND_LABELS = {
    DeviceKind.HMD: "HMD",
    DeviceKind.CONTROLLER: "Controller",
    DeviceKind.TRACKER: "Tracker",
    DeviceKind.BASE_STATION: "Base Station",
    DeviceKind.OTHER: "Other",
}


class DevicesTab(QWidget):
    def __init__(self, cfg, save_cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.save_cfg = save_cfg
        self.adapter = make_adapter(cfg["backend"])

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
        layout.addLayout(top)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Device", "Kind", "Role", "Tracking", "Battery"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)

        self.clients_label = QLabel("Clients: (none)")
        layout.addWidget(self.clients_label)

    def _on_backend_changed(self, key):
        self.adapter.disconnect()
        self.cfg["backend"] = key
        self.save_cfg(self.cfg)
        self.adapter = make_adapter(key)

    def refresh(self):
        snap = self.adapter.poll()

        if not snap.connected:
            self.status_label.setText(f"⚠ {snap.backend_name}: {snap.error or 'not connected'}")
        else:
            self.status_label.setText(f"✓ {snap.backend_name} connected")

        self.table.setRowCount(len(snap.devices))
        for row, dev in enumerate(snap.devices):
            self.table.setItem(row, 0, QTableWidgetItem(dev.name))
            self.table.setItem(row, 1, QTableWidgetItem(KIND_LABELS.get(dev.kind, "?")))
            self.table.setItem(row, 2, QTableWidgetItem(dev.role or ""))
            tracking = "OK" if dev.tracking_ok else ("--" if dev.tracking_ok is None else "Lost")
            self.table.setItem(row, 3, QTableWidgetItem(tracking))
            if dev.battery_percent is not None:
                batt = f"{dev.battery_percent:.0f}%" + (" (charging)" if dev.charging else "")
            else:
                batt = "--"
            self.table.setItem(row, 4, QTableWidgetItem(batt))

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
