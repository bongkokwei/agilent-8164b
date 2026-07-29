"""Control panels for the laser GUI.

Each panel is a self-contained group box that knows nothing about the
instrument: it validates what the user typed and emits a request signal. The
main window is the only place that maps those requests onto worker slots,
which keeps the panels trivially testable without any hardware.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .worker import SweepConfig

# Wavelength limits are deliberately wide: they cover every tunable module
# the 8164B mainframe accepts, and the instrument rejects anything its own
# module cannot reach.
WAVELENGTH_MIN_NM = 1200.0
WAVELENGTH_MAX_NM = 1700.0


class ConnectionPanel(QGroupBox):
    """VISA resource selection and session control."""

    connect_requested = pyqtSignal(str, int, int, int)
    disconnect_requested = pyqtSignal()
    refresh_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Connection", parent)

        self.resource_combo = QComboBox()
        self.resource_combo.setEditable(True)
        self.resource_combo.setToolTip(
            "VISA resource string, e.g. GPIB0::20::INSTR or "
            "TCPIP0::192.168.1.10::inst0::INSTR"
        )
        self.resource_combo.lineEdit().setPlaceholderText("GPIB0::20::INSTR")

        self.refresh_button = QPushButton("Scan")
        self.refresh_button.setToolTip("List the VISA resources currently visible")

        self.slot_spin = QSpinBox()
        self.slot_spin.setRange(0, 4)
        self.slot_spin.setToolTip("Mainframe slot holding the laser module")

        self.channel_spin = QSpinBox()
        self.channel_spin.setRange(1, 2)
        self.channel_spin.setValue(1)
        self.channel_spin.setToolTip("Channel number (dual-wavelength sources)")

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(500, 60000)
        self.timeout_spin.setSingleStep(500)
        self.timeout_spin.setValue(5000)
        self.timeout_spin.setSuffix(" ms")

        self.connect_button = QPushButton("Connect")
        self.connect_button.setDefault(True)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setEnabled(False)

        self.idn_label = QLabel("Not connected")
        self.idn_label.setWordWrap(True)
        self.idn_label.setStyleSheet("color: palette(mid);")

        resource_row = QHBoxLayout()
        resource_row.addWidget(self.resource_combo, 1)
        resource_row.addWidget(self.refresh_button)

        button_row = QHBoxLayout()
        button_row.addWidget(self.connect_button)
        button_row.addWidget(self.disconnect_button)

        form = QFormLayout(self)
        form.addRow("Resource", resource_row)
        form.addRow("Slot", self.slot_spin)
        form.addRow("Channel", self.channel_spin)
        form.addRow("Timeout", self.timeout_spin)
        form.addRow(button_row)
        form.addRow(self.idn_label)

        self.connect_button.clicked.connect(self._emit_connect)
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        self.refresh_button.clicked.connect(self.refresh_requested)

    def _emit_connect(self) -> None:
        resource = self.resource_combo.currentText().strip()
        if not resource:
            return
        self.connect_requested.emit(
            resource,
            self.slot_spin.value(),
            self.channel_spin.value(),
            self.timeout_spin.value(),
        )

    def set_resources(self, resources: list) -> None:
        current = self.resource_combo.currentText()
        self.resource_combo.clear()
        self.resource_combo.addItems(resources)
        if current:
            self.resource_combo.setCurrentText(current)

    def set_connected(self, connected: bool, idn: str = "") -> None:
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        for widget in (self.resource_combo, self.refresh_button, self.slot_spin,
                       self.channel_spin, self.timeout_spin):
            widget.setEnabled(not connected)
        self.idn_label.setText(idn if connected else "Not connected")
        self.idn_label.setStyleSheet(
            "color: palette(text);" if connected else "color: palette(mid);"
        )


class ReadoutPanel(QGroupBox):
    """Large, glanceable display of what the laser is actually doing."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Readout", parent)

        readout_font = QFont()
        readout_font.setPointSize(20)
        readout_font.setStyleHint(QFont.StyleHint.Monospace)
        readout_font.setFamily("monospace")

        self.wavelength_value = QLabel("—")
        self.power_value = QLabel("—")
        for label in (self.wavelength_value, self.power_value):
            label.setFont(readout_font)
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.state_label = QLabel("OUTPUT OFF")
        state_font = QFont()
        state_font.setPointSize(12)
        state_font.setBold(True)
        self.state_label.setFont(state_font)
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setMinimumHeight(34)

        self.path_label = QLabel("Path: —")
        self.sweep_label = QLabel("Sweep: idle")
        for label in (self.path_label, self.sweep_label):
            label.setStyleSheet("color: palette(mid);")

        grid = QGridLayout(self)
        grid.addWidget(self.state_label, 0, 0, 1, 3)
        grid.addWidget(QLabel("Wavelength"), 1, 0)
        grid.addWidget(self.wavelength_value, 1, 1)
        grid.addWidget(QLabel("nm"), 1, 2)
        # ":SOUR:POW?" reads back the commanded power, not a measurement —
        # the source module has no detector — so the label says so.
        power_caption = QLabel("Power (set)")
        power_caption.setToolTip(
            "Read back from the source, i.e. the power the module has been "
            "told to emit. It is not a measurement."
        )
        grid.addWidget(power_caption, 2, 0)
        grid.addWidget(self.power_value, 2, 1)
        self.power_unit_label = QLabel("dBm")
        grid.addWidget(self.power_unit_label, 2, 2)
        grid.addWidget(self.path_label, 3, 0, 1, 3)
        grid.addWidget(self.sweep_label, 4, 0, 1, 3)
        grid.setColumnStretch(1, 1)

        self.set_output_state(False)

    def update_state(self, state: dict) -> None:
        self.wavelength_value.setText(f"{state['wavelength_nm']:.4f}")
        unit = state.get("power_unit", "dBm")
        power = state["power"]
        self.power_value.setText(f"{power:.3f}" if unit == "dBm" else f"{power:.4e}")
        self.power_unit_label.setText(unit)
        self.path_label.setText(f"Path: {state.get('output_path') or '—'}")
        self.sweep_label.setText(
            "Sweep: running" if state.get("sweeping") else "Sweep: idle"
        )
        self.set_output_state(bool(state.get("laser_on")))

    def set_output_state(self, laser_on: bool) -> None:
        if laser_on:
            self.state_label.setText("⚠  OUTPUT ON — LASER RADIATION")
            self.state_label.setStyleSheet(
                "background-color: #c62828; color: white; border-radius: 4px;"
            )
        else:
            self.state_label.setText("OUTPUT OFF")
            self.state_label.setStyleSheet(
                "background-color: palette(window); color: palette(mid);"
                " border: 1px solid palette(mid); border-radius: 4px;"
            )

    def clear(self) -> None:
        self.wavelength_value.setText("—")
        self.power_value.setText("—")
        self.path_label.setText("Path: —")
        self.sweep_label.setText("Sweep: idle")
        self.set_output_state(False)


class OutputPanel(QGroupBox):
    """Static output settings: on/off, wavelength, power and output path."""

    laser_toggled = pyqtSignal(bool)
    wavelength_requested = pyqtSignal(float)
    power_requested = pyqtSignal(float, str)
    power_unit_requested = pyqtSignal(str)
    output_path_requested = pyqtSignal(str)

    _PATHS = {
        "High power": "high",
        "Low SSE": "lowsse",
        "Both (High power regulated)": "both_high",
        "Both (Low SSE regulated)": "both_low",
    }

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Output", parent)

        self.laser_button = QPushButton("Laser OFF")
        self.laser_button.setCheckable(True)
        self.laser_button.setMinimumHeight(40)
        self.laser_button.setToolTip("Enable or disable the optical output")

        self.wavelength_spin = QDoubleSpinBox()
        self.wavelength_spin.setRange(WAVELENGTH_MIN_NM, WAVELENGTH_MAX_NM)
        self.wavelength_spin.setDecimals(4)
        self.wavelength_spin.setSingleStep(0.1)
        self.wavelength_spin.setValue(1550.0)
        self.wavelength_spin.setSuffix(" nm")
        self.wavelength_spin.setKeyboardTracking(False)
        self.wavelength_button = QPushButton("Set")

        self.power_spin = QDoubleSpinBox()
        self.power_spin.setRange(-100.0, 1000.0)
        self.power_spin.setDecimals(3)
        self.power_spin.setSingleStep(0.1)
        self.power_spin.setValue(0.0)
        self.power_spin.setKeyboardTracking(False)
        self.power_unit_combo = QComboBox()
        self.power_unit_combo.addItems(["dBm", "mW", "uW", "nW"])
        self.power_button = QPushButton("Set")

        self.display_unit_combo = QComboBox()
        self.display_unit_combo.addItems(["dBm", "W"])
        self.display_unit_combo.setToolTip("Unit the instrument reports power in")

        self.path_combo = QComboBox()
        self.path_combo.addItems(self._PATHS.keys())
        self.path_combo.setToolTip("Output port — dual-output modules only")

        wavelength_row = QHBoxLayout()
        wavelength_row.addWidget(self.wavelength_spin, 1)
        wavelength_row.addWidget(self.wavelength_button)

        power_row = QHBoxLayout()
        power_row.addWidget(self.power_spin, 1)
        power_row.addWidget(self.power_unit_combo)
        power_row.addWidget(self.power_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.laser_button)
        form = QFormLayout()
        form.addRow("Wavelength", wavelength_row)
        form.addRow("Power", power_row)
        form.addRow("Display unit", self.display_unit_combo)
        form.addRow("Output path", self.path_combo)
        layout.addLayout(form)

        self.laser_button.toggled.connect(self._on_laser_toggled)
        self.wavelength_button.clicked.connect(
            lambda: self.wavelength_requested.emit(self.wavelength_spin.value())
        )
        self.wavelength_spin.editingFinished.connect(
            lambda: self.wavelength_requested.emit(self.wavelength_spin.value())
        )
        self.power_button.clicked.connect(self._emit_power)
        self.display_unit_combo.currentTextChanged.connect(self.power_unit_requested)
        self.path_combo.currentTextChanged.connect(
            lambda text: self.output_path_requested.emit(self._PATHS[text])
        )

    def _emit_power(self) -> None:
        self.power_requested.emit(
            self.power_spin.value(), self.power_unit_combo.currentText()
        )

    def _on_laser_toggled(self, checked: bool) -> None:
        self._style_laser_button(checked)
        self.laser_toggled.emit(checked)

    def _style_laser_button(self, on: bool) -> None:
        self.laser_button.setText("Laser ON" if on else "Laser OFF")
        self.laser_button.setStyleSheet(
            "QPushButton { background-color: #c62828; color: white; font-weight: bold; }"
            if on else ""
        )

    def set_laser_state_silently(self, on: bool) -> None:
        """Reflect the instrument's actual state without re-emitting a command."""
        if self.laser_button.isChecked() == on:
            return
        self.laser_button.blockSignals(True)
        self.laser_button.setChecked(on)
        self.laser_button.blockSignals(False)
        self._style_laser_button(on)


class TriggerPanel(QGroupBox):
    """What the module does when the rear-panel Input BNC is triggered."""

    input_mode_requested = pyqtSignal(str)
    configuration_requested = pyqtSignal(str)

    _INPUT_MODES = {
        "Ignore": "ignore",
        "Start sweep": "sweep_start",
        "Next step": "next_step",
    }

    _CONFIGURATIONS = {
        "Disabled": "disabled",
        "Default": "default",
        "Pass through": "passthrough",
        "Loopback": "loopback",
    }

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Trigger", parent)

        self.input_combo = QComboBox()
        self.input_combo.addItems(self._INPUT_MODES.keys())
        self.input_combo.setToolTip(
            "Response to an incoming trigger on the Input BNC:\n"
            "Ignore — do nothing\n"
            "Start sweep — begin a configured sweep cycle\n"
            "Next step — advance a stepped sweep by one step"
        )

        self.config_combo = QComboBox()
        self.config_combo.addItems(self._CONFIGURATIONS.keys())
        self.config_combo.setToolTip(
            "Mainframe trigger routing. The Input BNC only reaches the "
            "module when this is anything other than Disabled."
        )

        form = QFormLayout(self)
        form.addRow("Input BNC", self.input_combo)
        form.addRow("Routing", self.config_combo)

        self.input_combo.currentTextChanged.connect(
            lambda text: self.input_mode_requested.emit(self._INPUT_MODES[text])
        )
        self.config_combo.currentTextChanged.connect(
            lambda text: self.configuration_requested.emit(self._CONFIGURATIONS[text])
        )

    def set_state_silently(self, input_mode: str, configuration: str) -> None:
        """Show what the instrument reports without commanding it back."""
        for combo, mapping, value in (
            (self.input_combo, self._INPUT_MODES, input_mode),
            (self.config_combo, self._CONFIGURATIONS, configuration),
        ):
            label = next((k for k, v in mapping.items() if v == value), None)
            if label is None:
                continue
            combo.blockSignals(True)
            combo.setCurrentText(label)
            combo.blockSignals(False)


class SweepPanel(QGroupBox):
    """Configuration and transport controls for the built-in sweep engine."""

    start_requested = pyqtSignal(object)
    stop_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    continue_requested = pyqtSignal()
    check_requested = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Wavelength sweep", parent)

        self.start_spin = self._wavelength_spin(1520.0)
        self.stop_spin = self._wavelength_spin(1580.0)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Stepped", "Continuous"])

        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(0.0001, 100.0)
        self.step_spin.setDecimals(4)
        self.step_spin.setValue(1.0)
        self.step_spin.setSuffix(" nm")

        self.dwell_spin = QDoubleSpinBox()
        self.dwell_spin.setRange(0.0, 100.0)
        self.dwell_spin.setDecimals(3)
        self.dwell_spin.setValue(0.1)
        self.dwell_spin.setSuffix(" s")

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.001, 200.0)
        self.speed_spin.setDecimals(3)
        self.speed_spin.setValue(10.0)
        self.speed_spin.setSuffix(" nm/s")

        self.cycles_spin = QSpinBox()
        self.cycles_spin.setRange(1, 1000)

        self.repeat_combo = QComboBox()
        self.repeat_combo.addItems(["One way", "Two way"])

        self.check_button = QPushButton("Check")
        self.check_button.setToolTip("Validate the parameters without starting")
        self.start_button = QPushButton("Start")
        self.pause_button = QPushButton("Pause")
        self.continue_button = QPushButton("Continue")
        self.stop_button = QPushButton("Stop")

        self.check_label = QLabel("")
        self.check_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Start", self.start_spin)
        form.addRow("Stop", self.stop_spin)
        form.addRow("Mode", self.mode_combo)
        form.addRow("Step size", self.step_spin)
        form.addRow("Dwell", self.dwell_spin)
        form.addRow("Speed", self.speed_spin)
        form.addRow("Cycles", self.cycles_spin)
        form.addRow("Direction", self.repeat_combo)

        button_row = QHBoxLayout()
        for button in (self.check_button, self.start_button, self.pause_button,
                       self.continue_button, self.stop_button):
            button_row.addWidget(button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(button_row)
        layout.addWidget(self.check_label)

        self.mode_combo.currentTextChanged.connect(self._update_mode_fields)
        self._update_mode_fields(self.mode_combo.currentText())

        self.check_button.clicked.connect(
            lambda: self.check_requested.emit(self.config())
        )
        self.start_button.clicked.connect(
            lambda: self.start_requested.emit(self.config())
        )
        self.stop_button.clicked.connect(self.stop_requested)
        self.pause_button.clicked.connect(self.pause_requested)
        self.continue_button.clicked.connect(self.continue_requested)

        self.set_sweep_running(False)

    @staticmethod
    def _wavelength_spin(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(WAVELENGTH_MIN_NM, WAVELENGTH_MAX_NM)
        spin.setDecimals(4)
        spin.setValue(value)
        spin.setSuffix(" nm")
        spin.setKeyboardTracking(False)
        return spin

    def _update_mode_fields(self, mode_text: str) -> None:
        stepped = mode_text.lower().startswith("step")
        self.step_spin.setEnabled(stepped)
        self.dwell_spin.setEnabled(stepped)
        self.speed_spin.setEnabled(not stepped)

    def config(self) -> SweepConfig:
        return SweepConfig(
            start_nm=self.start_spin.value(),
            stop_nm=self.stop_spin.value(),
            step_nm=self.step_spin.value(),
            speed_nm_s=self.speed_spin.value(),
            dwell_s=self.dwell_spin.value(),
            cycles=self.cycles_spin.value(),
            mode="step" if self.mode_combo.currentText().lower().startswith("step")
                 else "continuous",
            repeat="oneway" if self.repeat_combo.currentText() == "One way" else "twoway",
        )

    def show_check_result(self, result: str) -> None:
        ok = result == "OK"
        self.check_label.setText(
            "Parameters OK" if ok else f"Problem: {result}"
        )
        self.check_label.setStyleSheet(
            "color: #2e7d32;" if ok else "color: #c62828;"
        )

    def set_sweep_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.check_button.setEnabled(not running)
        self.pause_button.setEnabled(running)
        self.continue_button.setEnabled(running)
        self.stop_button.setEnabled(running)


class LogPanel(QWidget):
    """Read-only view of the application log."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)  # bound memory during long sessions
        font = QFont()
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFamily("monospace")
        self.view.setFont(font)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.view.clear)

        self.debug_check = QCheckBox("Show raw SCPI (DEBUG)")

        controls = QHBoxLayout()
        controls.addWidget(self.debug_check)
        controls.addStretch(1)
        controls.addWidget(self.clear_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.view, 1)
        layout.addLayout(controls)

    def append(self, message: str) -> None:
        self.view.appendPlainText(message)
