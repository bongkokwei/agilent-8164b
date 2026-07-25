"""Tests for the PyQt6 laser GUI.

Runs entirely against the simulated instrument, so no mainframe, VISA
backend or display is required (set QT_QPA_PLATFORM=offscreen in CI).
"""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QDeadlineTimer, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from agilent_8164b.gui.main_window import LaserMainWindow  # noqa: E402
from agilent_8164b.gui.simulator import SimulatedAgilent8164B  # noqa: E402
from agilent_8164b.gui.worker import SweepConfig  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    """Keep QSettings out of the developer's real config directory."""
    from PyQt6.QtCore import QSettings

    monkeypatch.setattr(
        LaserMainWindow, "_settings",
        lambda self: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
    )


@pytest.fixture
def window(qapp, monkeypatch, isolated_settings):
    """A window in simulation mode, with dialogs auto-accepted."""
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    window = LaserMainWindow(simulate=True, poll_interval_ms=20)
    window.show()
    yield window
    window.close()


def _spin(qapp, ms: int) -> None:
    """Pump the event loop so queued cross-thread signals get delivered."""
    deadline = QDeadlineTimer(ms)
    while not deadline.hasExpired():
        qapp.processEvents()


def _connect(qapp, window) -> None:
    window.connection_panel.resource_combo.setCurrentText("SIM::INSTR")
    window.connection_panel.connect_button.click()
    for _ in range(100):
        _spin(qapp, 20)
        if window._connected:
            return
    raise AssertionError("window never reported a connection")


# -- simulator ---------------------------------------------------------
def test_simulator_sweep_advances_wavelength():
    sim = SimulatedAgilent8164B()
    sim.configure_sweep(1550.0, 1552.0, step_nm=0.5, dwell_s=0.01, mode="step")
    assert sim.check_sweep_params() == "OK"
    sim.start_sweep()
    assert sim.is_sweeping()
    assert 1550.0 <= sim.get_wavelength_nm() <= 1552.0


def test_simulator_rejects_inverted_range():
    sim = SimulatedAgilent8164B()
    sim.configure_sweep(1580.0, 1520.0)
    assert sim.check_sweep_params() != "OK"


def test_simulator_power_is_a_setpoint_readback():
    """There is no detector, so get_power() must not vary with the sweep."""
    sim = SimulatedAgilent8164B()
    sim.set_power(3.0, unit="dBm")
    sim.laser_on()
    sim.configure_sweep(1530.0, 1565.0, step_nm=0.5, dwell_s=0.001)
    sim.start_sweep()
    readings = []
    for _ in range(5):
        sim.get_wavelength_nm()
        readings.append(sim.get_power())
    assert readings == [3.0] * 5


# -- panels (no instrument involved) -----------------------------------
def test_sweep_panel_builds_config(qapp):
    from agilent_8164b.gui.widgets import SweepPanel

    panel = SweepPanel()
    panel.start_spin.setValue(1530.0)
    panel.stop_spin.setValue(1565.0)
    panel.mode_combo.setCurrentText("Continuous")
    panel.speed_spin.setValue(5.0)
    panel.repeat_combo.setCurrentText("Two way")

    config = panel.config()
    assert isinstance(config, SweepConfig)
    assert (config.start_nm, config.stop_nm) == (1530.0, 1565.0)
    assert config.mode == "continuous"
    assert config.repeat == "twoway"
    # Continuous mode drives speed, so the stepped fields are greyed out.
    assert panel.speed_spin.isEnabled()
    assert not panel.step_spin.isEnabled()


def test_controls_disabled_until_connected(qapp, window):
    assert not window.output_panel.isEnabled()
    assert not window.sweep_panel.isEnabled()


# -- end-to-end through the worker thread ------------------------------
def test_connect_enables_controls_and_reports_idn(qapp, window):
    _connect(qapp, window)
    assert window.output_panel.isEnabled()
    assert window.sweep_panel.isEnabled()
    assert "SIMULATED" in window.connection_panel.idn_label.text()


def test_laser_toggle_round_trips(qapp, window):
    _connect(qapp, window)
    window.output_panel.laser_button.setChecked(True)
    _spin(qapp, 200)
    assert window.worker._inst.is_laser_on()
    assert "OUTPUT ON" in window.readout_panel.state_label.text()

    window.output_panel.laser_button.setChecked(False)
    _spin(qapp, 200)
    assert not window.worker._inst.is_laser_on()


def test_setting_wavelength_updates_readout(qapp, window):
    _connect(qapp, window)
    window.output_panel.wavelength_spin.setValue(1543.21)
    window.output_panel.wavelength_button.click()
    _spin(qapp, 300)
    assert window.worker._inst.get_wavelength_nm() == pytest.approx(1543.21)
    assert "1543.21" in window.readout_panel.wavelength_value.text()


def test_sweep_runs_then_returns_to_idle(qapp, window):
    _connect(qapp, window)
    window.output_panel.laser_button.setChecked(True)
    window.sweep_panel.start_spin.setValue(1550.0)
    window.sweep_panel.stop_spin.setValue(1551.0)
    window.sweep_panel.step_spin.setValue(0.1)
    window.sweep_panel.dwell_spin.setValue(0.02)
    window.sweep_panel.start_button.click()

    _spin(qapp, 200)
    assert window.sweep_panel.check_label.text() == "Parameters OK"
    assert window.worker._inst.is_sweeping()
    # While running, only the transport controls are live.
    assert not window.sweep_panel.start_button.isEnabled()
    assert window.sweep_panel.stop_button.isEnabled()

    for _ in range(100):
        _spin(qapp, 50)
        if not window.worker._inst.is_sweeping():
            break
    _spin(qapp, 200)

    wavelength_nm = window.worker._inst.get_wavelength_nm()
    assert 1549.9 <= wavelength_nm <= 1551.1
    assert window.sweep_panel.start_button.isEnabled()
    assert not window.sweep_panel.stop_button.isEnabled()


def test_bad_sweep_range_is_reported_and_not_started(qapp, window):
    _connect(qapp, window)
    window.sweep_panel.start_spin.setValue(1580.0)
    window.sweep_panel.stop_spin.setValue(1520.0)
    window.sweep_panel.start_button.click()
    _spin(qapp, 300)

    assert "Problem" in window.sweep_panel.check_label.text()
    assert not window.worker._inst.is_sweeping()


def test_emergency_off_stops_sweep_and_output(qapp, window):
    _connect(qapp, window)
    window.output_panel.laser_button.setChecked(True)
    window.sweep_panel.start_button.click()
    _spin(qapp, 200)

    window.laser_off_action.trigger()
    _spin(qapp, 300)

    assert not window.worker._inst.is_laser_on()
    assert not window.worker._inst.is_sweeping()


def test_closing_disables_the_output(qapp, monkeypatch, isolated_settings):
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    window = LaserMainWindow(simulate=True, poll_interval_ms=20)
    window.show()
    _connect(qapp, window)
    window.output_panel.laser_button.setChecked(True)
    _spin(qapp, 200)

    instrument = window.worker._inst
    assert instrument.is_laser_on()

    window.close()
    _spin(qapp, 100)

    assert not instrument.is_laser_on()
    assert not window.thread.isRunning()


def test_log_panel_receives_driver_messages(qapp, window):
    _connect(qapp, window)
    _spin(qapp, 100)
    assert "Opening simulated instrument" in window.log_panel.view.toPlainText()
