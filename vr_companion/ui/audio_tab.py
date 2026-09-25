from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider, QVBoxLayout, QWidget
)

from ..audio.noise import VOLUME_MAX_PCT


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
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, VOLUME_MAX_PCT)
        self.vol_slider.setSingleStep(1)
        self.vol_slider.setPageStep(5)
        self.vol_slider.setTickPosition(QSlider.TicksBelow)
        self.vol_slider.setTickInterval(5)
        self.vol_slider.setValue(min(int(cfg["audio"]["volume_pct"]), VOLUME_MAX_PCT))
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        vol_row.addWidget(self.vol_slider, stretch=1)
        self.vol_value_label = QLabel()
        self.vol_value_label.setMinimumWidth(40)
        vol_row.addWidget(self.vol_value_label)
        layout.addLayout(vol_row)
        self._update_volume_label(self.vol_slider.value())

        # Apply volume live while dragging, but only write the config file
        # once the value has settled.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(lambda: self.save_cfg(self.cfg))

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

    def _update_volume_label(self, pct):
        self.vol_value_label.setText(f"{pct}%")

    def _on_volume_changed(self, pct):
        self._update_volume_label(pct)
        self.engine.volume_pct = pct
        self.cfg["audio"]["volume_pct"] = pct
        self._save_timer.start()

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
