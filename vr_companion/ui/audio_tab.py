from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget
)

from ..audio.noise import VOLUME_PRESETS


class AudioTab(QWidget):
    def __init__(self, engine, cfg, save_cfg, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.cfg = cfg
        self.save_cfg = save_cfg

        layout = QVBoxLayout(self)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(cfg["audio"]["enabled"])
        self.enabled_check.toggled.connect(self._on_enabled_toggled)
        layout.addWidget(self.enabled_check)

        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("Volume:"))
        self.vol_combo = QComboBox()
        for p in VOLUME_PRESETS:
            self.vol_combo.addItem(f"{p}%", p)
        current_pct = cfg["audio"]["volume_pct"]
        idx = self.vol_combo.findData(current_pct)
        self.vol_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.vol_combo.currentIndexChanged.connect(self._on_volume_changed)
        vol_row.addWidget(self.vol_combo)
        vol_row.addStretch(1)
        layout.addLayout(vol_row)

        dev_row = QHBoxLayout()
        dev_row.addWidget(QLabel("Output device match:"))
        self.device_edit = QLineEdit(cfg["audio"]["device_match"])
        self.device_edit.editingFinished.connect(self._on_device_edited)
        dev_row.addWidget(self.device_edit, stretch=1)
        layout.addLayout(dev_row)

        self.candidates_combo = QComboBox()
        self.candidates_combo.addItem("(pick a detected device)")
        self.candidates_combo.currentIndexChanged.connect(self._on_candidate_picked)
        layout.addWidget(self.candidates_combo)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        refresh_btn = QPushButton("Refresh device list")
        refresh_btn.clicked.connect(self._refresh_candidates)
        layout.addWidget(refresh_btn)

        layout.addStretch(1)
        self._refresh_candidates()

    def _on_enabled_toggled(self, checked):
        self.engine.enabled = checked
        self.cfg["audio"]["enabled"] = checked
        self.save_cfg(self.cfg)

    def _on_volume_changed(self, idx):
        pct = self.vol_combo.itemData(idx)
        self.engine.volume_pct = pct
        self.cfg["audio"]["volume_pct"] = pct
        self.save_cfg(self.cfg)

    def _on_device_edited(self):
        match = self.device_edit.text()
        self.engine.device_match = match
        self.engine.current_target = None  # force re-check/reopen
        self.cfg["audio"]["device_match"] = match
        self.save_cfg(self.cfg)

    def _on_candidate_picked(self, idx):
        if idx <= 0:
            return
        label = self.candidates_combo.itemData(idx)
        if label:
            self.device_edit.setText(label)
            self._on_device_edited()

    def _refresh_candidates(self):
        self.candidates_combo.blockSignals(True)
        self.candidates_combo.clear()
        self.candidates_combo.addItem("(pick a detected device)")
        candidates = self.engine.list_candidate_devices() or self.engine.list_all_output_devices()
        for key, label in candidates:
            self.candidates_combo.addItem(label, label if isinstance(key, str) else key)
        self.candidates_combo.blockSignals(False)

    def refresh_status(self):
        state = "Active (playing)" if self.engine.is_active else "Waiting for device"
        self.status_label.setText(f"Status: {state}")
