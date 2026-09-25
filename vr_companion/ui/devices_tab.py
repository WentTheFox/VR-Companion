from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QBrush, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QInputDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget
)

from ..adapters import ALL_ADAPTERS, make_adapter
from ..adapters.base import BackendSnapshot, DeviceKind
from ..roles import (
    NO_ROLE, TRACKER_ROLES, find_steamvr_settings, get_role, read_steamvr_roles, set_role
)

ROLE_LABELS = dict(TRACKER_ROLES)

# libmonado status-only connections (this app's own poll, other monitoring
# tools) show up as clients too, but aren't VR apps and have no session to lose.
STATUS_CLIENT_NAMES = {"libmonado"}


def vr_app_clients(snap):
    """Clients in a snapshot that are actual VR apps, not status monitors."""
    if snap is None:
        return []
    return [c for c in snap.clients if c.name not in STATUS_CLIENT_NAMES]


COLUMNS = ["Device", "Serial", "Kind", "Role", "Tracking", "Battery"]
COL_ROLE = 3

KIND_LABELS = {
    DeviceKind.HMD: "HMD",
    DeviceKind.CONTROLLER: "Controller",
    DeviceKind.TRACKER: "Tracker",
    DeviceKind.BASE_STATION: "Base Station",
    DeviceKind.OTHER: "Other",
}


class AdapterWorker(QObject):
    """Runs every potentially blocking adapter call on its own thread: poll()
    can block for seconds inside libmonado while a freshly started service
    is still initialising, and restart_service() waits for the old process
    to exit. Requests arrive as queued signals, so a poll and a restart can
    never run concurrently against the same adapter."""
    polled = Signal(object, object, bool)   # adapter, BackendSnapshot, service running
    restarted = Signal(object, bool)        # adapter, whether the relaunch succeeded

    @Slot(object)
    def poll(self, adapter):
        try:
            snap = adapter.poll()
        except Exception as e:
            snap = BackendSnapshot(adapter.name, connected=False, error=str(e))
        running = adapter.supports_service_restart() and adapter.is_service_running()
        self.polled.emit(adapter, snap, running)

    @Slot(object)
    def restart(self, adapter):
        try:
            ok = adapter.restart_service()
        except Exception as e:
            print(f"vr-companion: restart_service() raised: {e}")
            ok = False
        self.restarted.emit(adapter, ok)

    @Slot(object)
    def disconnect_adapter(self, adapter):
        adapter.disconnect()


class DevicesTab(QWidget):
    adapter_changed = Signal(object)   # new VRAdapter
    service_restarted = Signal(bool)   # whether the relaunch succeeded
    # To the worker thread (queued):
    _request_poll = Signal(object)
    _request_restart = Signal(object)
    _request_disconnect = Signal(object)

    def __init__(self, cfg, save_cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.save_cfg = save_cfg
        self.adapter = make_adapter(cfg["backend"])
        self._restarting = False
        self._poll_in_flight = False
        self._service_running = False   # as of the last poll
        self.last_snapshot = None

        self._worker_thread = QThread(self)
        self._worker = AdapterWorker()
        self._worker.moveToThread(self._worker_thread)
        self._request_poll.connect(self._worker.poll)
        self._request_restart.connect(self._worker.restart)
        self._request_disconnect.connect(self._worker.disconnect_adapter)
        self._worker.polled.connect(self._on_polled)
        self._worker.restarted.connect(self._on_restart_finished)
        self._worker_thread.finished.connect(self._worker.deleteLater)
        self._worker_thread.start()
        # Qt aborts the process if a running QThread is destroyed, so stop it
        # on every exit path, not just the tray's Quit.
        QApplication.instance().aboutToQuit.connect(self.shutdown)

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

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeaderItem(COL_ROLE).setToolTip(
            "The role the VR runtime assigned (head / left / right hand), if any.\n"
            "Otherwise, for trackers: a body role you pick, saved per serial number in\n"
            "VR Companion's config (same values as SteamVR's \"Manage Trackers\").\n"
            "Monado/xrizer don't pass body roles on to games yet, so for now they're\n"
            "for telling your trackers apart.")
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(COL_ROLE, QHeaderView.ResizeToContents)
        # Row identity (serial or id) -> live role combo. Kept across refreshes
        # so a periodic poll doesn't close a dropdown the user has open.
        self._row_keys = []
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)


        bottom = QHBoxLayout()
        self.clients_label = QLabel("Clients: (none)")
        bottom.addWidget(self.clients_label, stretch=1)
        import_btn = QPushButton("Import body roles from SteamVR...")
        import_btn.setToolTip(
            "Copy tracker roles from a SteamVR steamvr.vrsettings file (e.g. your Windows\n"
            "install) into VR Companion, matched by serial number.")
        import_btn.clicked.connect(self._on_import_roles)
        bottom.addWidget(import_btn)
        layout.addLayout(bottom)

        self._update_restart_visibility()
        self._rebuild_service_options()

    def _update_restart_visibility(self):
        self.restart_btn.setVisible(self.adapter.supports_service_restart())
        self._update_service_button()

    def _update_service_button(self):
        if not self.adapter.supports_service_restart():
            return
        if self._service_running:
            self.restart_btn.setText("Restart service")
            self.restart_btn.setToolTip(
                "Stops and relaunches the VR service so newly powered-on devices are picked up.\n"
                "Any connected VR app (game) will lose its session and likely crash."
            )
        else:
            self.restart_btn.setText("Start service")
            self.restart_btn.setToolTip("Launches the VR service, owned by VR Companion.")

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
        self._request_disconnect.emit(self.adapter)
        self.cfg["backend"] = key
        self.save_cfg(self.cfg)
        self.adapter = make_adapter(key)
        self._service_running = False   # until the new adapter's first poll
        self._update_restart_visibility()
        self._rebuild_service_options()
        self.adapter_changed.emit(self.adapter)

    def _on_restart_clicked(self):
        # Re-check rather than trusting the label: the service may have been
        # started (or died) since the last poll. Cheap for Monado (pidfile +
        # /proc), unlike poll() itself.
        running = self.adapter.is_service_running()
        if running:
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
        verb = "Restarting" if running else "Starting"
        self.status_label.setText(f"⟳ {verb} {self.adapter.name} service...")

        self._restarting = True
        self._request_restart.emit(self.adapter)

    def _on_restart_finished(self, adapter, ok):
        self._restarting = False
        self.restart_btn.setEnabled(True)
        self.backend_combo.setEnabled(True)
        self.options_box.setEnabled(True)
        if ok:
            self.refresh()
        else:
            # No immediate poll: its result would overwrite this message
            # before anyone could read it.
            self.status_label.setText(f"⚠ {self.adapter.name}: service (re)start failed (see terminal output)")
        self.service_restarted.emit(ok)

    @staticmethod
    def _can_have_body_role(dev):
        # Only where the runtime didn't already assign a role; needs a serial
        # to persist against.
        return (bool(dev.serial) and not dev.placeholder and not dev.role
                and dev.kind == DeviceKind.TRACKER)

    def _make_role_combo(self, serial):
        combo = QComboBox()
        for key, label in TRACKER_ROLES:
            combo.addItem(label, key)
        idx = combo.findData(get_role(self.cfg, serial))
        combo.setCurrentIndex(idx if idx >= 0 else combo.findData(NO_ROLE))
        combo.currentIndexChanged.connect(
            lambda _, c=combo, s=serial: self._on_role_changed(s, c.currentData()))
        return combo

    def _on_import_roles(self):
        found = find_steamvr_settings()
        if len(found) == 1:
            path = found[0]
        elif found:
            choice, ok = QInputDialog.getItem(
                self, "Import body roles", "SteamVR settings file:", [str(p) for p in found], 0, False)
            if not ok:
                return
            path = choice
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "Find steamvr.vrsettings", "", "SteamVR settings (steamvr.vrsettings *.vrsettings)")
            if not path:
                return
        try:
            imported = read_steamvr_roles(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Import body roles", f"Couldn't read {path}:\n{e}")
            return
        if not imported:
            QMessageBox.information(self, "Import body roles", f"No tracker roles found in:\n{path}")
            return

        lines = []
        for serial, role in sorted(imported.items()):
            current = get_role(self.cfg, serial)
            change = "" if current in (NO_ROLE, role) else f"  (replaces {ROLE_LABELS[current]})"
            lines.append(f"  • {serial}: {ROLE_LABELS[role]}{change}")
        msg = f"Import these body roles from\n{path}?\n\n" + "\n".join(lines)
        if QMessageBox.question(self, "Import body roles", msg) != QMessageBox.Yes:
            return
        for serial, role in imported.items():
            set_role(self.cfg, serial, role)
        self.save_cfg(self.cfg)
        self._row_keys = []   # force the role combos to be rebuilt from config
        self.refresh()

    def _on_role_changed(self, serial, role):
        set_role(self.cfg, serial, role)
        self.save_cfg(self.cfg)

    def shutdown(self):
        """Stop the worker thread; safe to call more than once."""
        if not self._worker_thread.isRunning():
            return
        self._worker_thread.quit()
        if not self._worker_thread.wait(3000):
            print("vr-companion: adapter worker still busy at exit")

    def refresh(self):
        """Request a poll; the result is rendered in _on_polled(). Never
        blocks -- skipped while a poll or restart is still in progress."""
        if self._restarting or self._poll_in_flight:
            return
        self._poll_in_flight = True
        self._request_poll.emit(self.adapter)

    def _on_polled(self, adapter, snap, running):
        self._poll_in_flight = False
        if adapter is not self.adapter:
            return  # stale result from before a backend switch
        self.last_snapshot = snap
        self._service_running = running
        self._update_service_button()

        if snap.busy:
            self.status_label.setText(f"⟳ {snap.backend_name}: {snap.busy}")
        elif not snap.connected:
            self.status_label.setText(f"⚠ {snap.backend_name}: {snap.error or 'not connected'}")
        else:
            self.status_label.setText(f"✓ {snap.backend_name} connected")

        row_keys = [dev.serial or dev.id for dev in snap.devices]
        rows_changed = row_keys != self._row_keys
        self._row_keys = row_keys
        self.table.setRowCount(len(snap.devices))
        for row, dev in enumerate(snap.devices):
            if rows_changed:
                self.table.removeCellWidget(row, COL_ROLE)
                if self._can_have_body_role(dev):
                    self.table.setCellWidget(row, COL_ROLE, self._make_role_combo(dev.serial))
            if dev.placeholder:
                kind = KIND_LABELS.get(dev.kind, "?") if dev.kind != DeviceKind.OTHER else "?"
                # Read-only role text: e.g. shows which tracker (Waist, ...) has
                # already been found while discovery is still running.
                role = get_role(self.cfg, dev.serial) if dev.serial and dev.kind == DeviceKind.TRACKER else NO_ROLE
                role_text = ROLE_LABELS[role] if role != NO_ROLE else ""
                cells = [dev.name, dev.serial or "?", kind, role_text, dev.placeholder_status or "", "--"]
            else:
                tracking = "OK" if dev.tracking_ok else ("--" if dev.tracking_ok is None else "Lost")
                if dev.battery_percent is not None:
                    batt = f"{dev.battery_percent:.0f}%" + (" (charging)" if dev.charging else "")
                else:
                    batt = "--"
                # A body-role combo covers the Role cell when the runtime left it empty.
                cells = [dev.name, dev.serial or "--", KIND_LABELS.get(dev.kind, "?"), dev.role or "",
                         tracking, batt]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if dev.placeholder:
                    font = QFont(item.font())
                    font.setItalic(True)
                    item.setFont(font)
                    item.setForeground(QBrush(self.palette().color(QPalette.Disabled, QPalette.Text)))
                    item.setToolTip(dev.note or "")
                self.table.setItem(row, col, item)
        if rows_changed:
            self.table.resizeColumnToContents(COL_ROLE)
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
