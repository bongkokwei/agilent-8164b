"""Tests for the PyQt6 laser GUI.

These drive the real window and the real :class:`Agilent8164B` driver; only
the VISA session underneath is stubbed (see ``conftest.py``), so the SCPI the
driver builds is exercised for real. No hardware and no display are needed —
set ``QT_QPA_PLATFORM=offscreen`` in CI.
"""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QDeadlineTimer, QSettings  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from agilent_8164b.gui.main_window import LaserMainWindow  # noqa: E402
from agilent_8164b.gui.worker import SweepConfig  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def isolated_settings(monkeypatch, tmp_path):
    """Keep QSettings out of the developer's real config directory."""
    monkeypatch.setattr(
        LaserMainWindow, "_settings",
        lambda self: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
    )


@pytest.fixture
def window(qapp, monkeypatch, isolated_settings, visa):
    """An unconnected window, with dialogs auto-accepted.

    Depends on ``visa`` so the stub is installed before the window's initial
    resource scan runs, rather than racing it.
    """
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    window = LaserMainWindow(poll_interval_ms=20)
    window.show()
    yield window
    window.close()


def _spin(qapp, ms: int) -> None:
    """Pump the event loop so queued cross-thread signals get delivered."""
    deadline = QDeadlineTimer(ms)
    while not deadline.hasExpired():
        qapp.processEvents()


def _connect(qapp, window) -> None:
    window.connection_panel.resource_combo.setCurrentText("GPIB0::20::INSTR")
    window.connection_panel.connect_button.click()
    for _ in range(100):
        _spin(qapp, 20)
        if window._connected:
            return
    raise AssertionError("window never reported a connection")


def _commands(visa, containing: str) -> list:
    return [w for w in visa.writes if containing in w.upper()]


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


# -- end-to-end: GUI -> worker thread -> driver -> stubbed VISA --------
def test_connect_enables_controls_and_reports_idn(qapp, window, visa):
    _connect(qapp, window)
    assert window.output_panel.isEnabled()
    assert window.sweep_panel.isEnabled()
    assert "8164B" in window.connection_panel.idn_label.text()


def test_connection_failure_leaves_controls_disabled(qapp, window, monkeypatch):
    import pyvisa

    class FailingResourceManager:
        def open_resource(self, *args, **kwargs):
            raise OSError("VI_ERROR_RSRC_NFOUND")

    monkeypatch.setattr(pyvisa, "ResourceManager", FailingResourceManager)
    window.connection_panel.resource_combo.setCurrentText("GPIB0::20::INSTR")
    window.connection_panel.connect_button.click()
    _spin(qapp, 300)

    assert not window._connected
    assert not window.output_panel.isEnabled()
    assert window.connection_panel.connect_button.isEnabled()


def test_laser_toggle_sends_the_right_scpi(qapp, window, visa):
    _connect(qapp, window)
    window.output_panel.laser_button.setChecked(True)
    _spin(qapp, 200)
    assert ":OUTP0:CHAN1:STAT 1" in visa.writes
    assert "OUTPUT ON" in window.readout_panel.state_label.text()

    window.output_panel.laser_button.setChecked(False)
    _spin(qapp, 200)
    assert ":OUTP0:CHAN1:STAT 0" in visa.writes


def test_setting_wavelength_updates_readout(qapp, window, visa):
    _connect(qapp, window)
    window.output_panel.wavelength_spin.setValue(1543.21)
    window.output_panel.wavelength_button.click()
    _spin(qapp, 300)

    assert ":SOUR0:CHAN1:WAV 1543.21NM" in visa.writes
    assert "1543.21" in window.readout_panel.wavelength_value.text()


def test_setting_power_sends_unit_suffix(qapp, window, visa):
    _connect(qapp, window)
    window.output_panel.power_spin.setValue(2.5)
    window.output_panel.power_unit_combo.setCurrentText("dBm")
    window.output_panel.power_button.click()
    _spin(qapp, 200)
    assert ":SOUR0:CHAN1:POW 2.5DBM" in visa.writes


def test_output_path_selection(qapp, window, visa):
    _connect(qapp, window)
    window.output_panel.path_combo.setCurrentText("Low SSE")
    _spin(qapp, 200)
    assert ":OUTP0:CHAN1:PATH LOWS" in visa.writes


def test_sweep_configures_starts_then_returns_to_idle(qapp, window, visa):
    _connect(qapp, window)
    window.sweep_panel.start_spin.setValue(1530.0)
    window.sweep_panel.stop_spin.setValue(1565.0)
    window.sweep_panel.step_spin.setValue(0.5)
    window.sweep_panel.start_button.click()
    _spin(qapp, 200)

    assert window.sweep_panel.check_label.text() == "Parameters OK"
    assert ":SOUR0:CHAN1:WAV:SWE:STAR 1530.0NM" in visa.writes
    assert ":SOUR0:CHAN1:WAV:SWE:STOP 1565.0NM" in visa.writes
    assert ":SOUR0:CHAN1:WAV:SWE:STEP 0.5NM" in visa.writes
    assert ":SOUR0:CHAN1:WAV:SWE:STAT START" in visa.writes
    # The parameter check must happen before the sweep is allowed to start.
    checks = [i for i, w in enumerate(visa.writes) if ":WAV:SWE:CHEC?" in w.upper()]
    starts = [i for i, w in enumerate(visa.writes) if w.endswith(":STAT START")]
    assert checks and starts and checks[0] < starts[0]

    for _ in range(100):
        _spin(qapp, 30)
        if window.sweep_panel.start_button.isEnabled():
            break
    _spin(qapp, 100)

    # Once the instrument stops reporting a running sweep, the transport
    # controls return to their idle arrangement.
    assert window.sweep_panel.start_button.isEnabled()
    assert not window.sweep_panel.stop_button.isEnabled()


def test_bad_sweep_range_is_reported_and_not_started(qapp, window, visa):
    _connect(qapp, window)
    window.sweep_panel.start_spin.setValue(1580.0)
    window.sweep_panel.stop_spin.setValue(1520.0)
    window.sweep_panel.start_button.click()
    _spin(qapp, 300)

    assert "Problem" in window.sweep_panel.check_label.text()
    assert not _commands(visa, ":STAT START")


def test_emergency_off_stops_sweep_and_output(qapp, window, visa):
    _connect(qapp, window)
    window.output_panel.laser_button.setChecked(True)
    window.sweep_panel.start_button.click()
    _spin(qapp, 200)

    window.laser_off_action.trigger()
    _spin(qapp, 300)

    assert ":SOUR0:CHAN1:WAV:SWE:STAT STOP" in visa.writes
    assert visa.writes.index(":OUTP0:CHAN1:STAT 0") > visa.writes.index(":OUTP0:CHAN1:STAT 1")
    assert not visa._output_on


def test_closing_disables_the_output_and_closes_the_session(qapp, monkeypatch,
                                                            isolated_settings, visa):
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    window = LaserMainWindow(poll_interval_ms=20)
    window.show()
    _connect(qapp, window)
    window.output_panel.laser_button.setChecked(True)
    _spin(qapp, 200)
    assert visa._output_on

    window.close()
    _spin(qapp, 100)

    assert not visa._output_on
    assert visa.closed
    assert not window.thread.isRunning()


def test_log_panel_receives_driver_messages(qapp, window, visa):
    _connect(qapp, window)
    _spin(qapp, 100)
    assert "Connecting to GPIB0::20::INSTR" in window.log_panel.view.toPlainText()


def test_debug_tickbox_surfaces_raw_scpi(qapp, window, visa):
    _connect(qapp, window)
    window.log_panel.debug_check.setChecked(True)
    window.output_panel.wavelength_button.click()
    _spin(qapp, 200)
    assert "write: :SOUR0:CHAN1:WAV" in window.log_panel.view.toPlainText()
