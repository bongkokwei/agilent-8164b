"""Instrument worker that keeps all VISA traffic off the GUI thread.

Every call into the driver blocks for as long as the mainframe takes to
answer (milliseconds on GPIB, occasionally much longer over LAN), so the
driver instance lives entirely inside a :class:`QThread`. The window talks to
it only through queued signals and slots, which means no widget ever waits on
I/O and the interface stays responsive during a long sweep.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)


@dataclass
class SweepConfig:
    """Parameters for the module's built-in wavelength sweep engine."""

    start_nm: float = 1520.0
    stop_nm: float = 1580.0
    step_nm: float = 1.0
    speed_nm_s: float = 10.0
    dwell_s: float = 0.1
    cycles: int = 1
    mode: str = "step"       # 'step' or 'continuous'
    repeat: str = "oneway"   # 'oneway' or 'twoway'

    def as_kwargs(self) -> dict:
        return asdict(self)


class LaserWorker(QObject):
    """Owns the driver instance and executes every instrument call.

    Move an instance onto a :class:`QThread` and drive it through its slots;
    it reports back through the signals below. Errors never propagate as
    exceptions — they arrive as :attr:`error` with a human-readable message,
    so the GUI decides how loudly to complain.
    """

    # -- outbound signals ---------------------------------------------
    resources_listed = pyqtSignal(list)
    connected = pyqtSignal(str)              # *IDN? response
    disconnected = pyqtSignal()
    error = pyqtSignal(str)
    status = pyqtSignal(str)
    state_polled = pyqtSignal(dict)          # see _poll() for the keys
    sweep_running_changed = pyqtSignal(bool)
    sweep_checked = pyqtSignal(str)          # "OK" or a problem description

    def __init__(self, poll_interval_ms: int = 200):
        super().__init__()
        self._inst = None
        self._poll_interval_ms = poll_interval_ms
        self._poll_timer: Optional[QTimer] = None
        self._was_sweeping = False
        self._power_unit = "dBm"
        self._poll_tick = 0

    # -- lifecycle -----------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._inst is not None

    @pyqtSlot()
    def start(self) -> None:
        """Create the poll timer. Runs once, inside the worker thread."""
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self._poll_interval_ms)
        self._poll_timer.timeout.connect(self._poll)

    @pyqtSlot()
    def list_resources(self) -> None:
        """Enumerate VISA resources (blocking, hence worker-side)."""
        try:
            import pyvisa

            resources = list(pyvisa.ResourceManager().list_resources())
        except Exception as exc:  # noqa: BLE001 - any backend failure is reportable
            self.error.emit(f"Could not list VISA resources: {exc}")
            self.resources_listed.emit([])
            return
        self.resources_listed.emit(resources)

    @pyqtSlot(str, int, int, int)
    def connect_to(self, resource_name: str, slot: int, channel: int, timeout_ms: int) -> None:
        if self._inst is not None:
            self.error.emit("Already connected — disconnect first.")
            return
        try:
            from ..instrument import Agilent8164B

            self._inst = Agilent8164B(
                resource_name, slot=slot, channel=channel, timeout_ms=timeout_ms
            )
            idn = self._inst.identify()
            self._power_unit = self._inst.get_power_unit()
        except Exception as exc:  # noqa: BLE001
            self._inst = None
            self.error.emit(f"Connection to {resource_name} failed: {exc}")
            return

        self._was_sweeping = False
        self._poll_tick = 0
        self.connected.emit(idn)
        self.status.emit(f"Connected to {resource_name}")
        if self._poll_timer is not None:
            self._poll_timer.start()

    @pyqtSlot()
    def disconnect_from(self) -> None:
        """Close the session, leaving the output off behind us."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
        if self._inst is None:
            self.disconnected.emit()
            return
        try:
            self._inst.stop_sweep()
            self._inst.laser_off()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not safe the output before closing: %s", exc)
        try:
            self._inst.close()
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Error while closing the session: {exc}")
        finally:
            self._inst = None
            self._was_sweeping = False
            self.disconnected.emit()
            self.status.emit("Disconnected")

    # -- helper --------------------------------------------------------
    #: Returned by :meth:`_call` when the instrument call did not go through.
    #: A distinct sentinel is needed because every command method returns
    #: ``None`` on success, so ``None`` cannot double as the failure marker.
    FAILED = object()

    def _call(self, description: str, func, *args, **kwargs):
        """Run one instrument call, reporting failures instead of raising."""
        if self._inst is None:
            self.error.emit(f"Not connected — cannot {description}.")
            return self.FAILED
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - VISA errors vary by backend
            logger.exception("Failed to %s", description)
            self.error.emit(f"Failed to {description}: {exc}")
            return self.FAILED

    def _failed(self, result) -> bool:
        return result is self.FAILED

    # -- output control ------------------------------------------------
    @pyqtSlot(bool)
    def set_laser_on(self, on: bool) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot switch the laser.")
            return
        action = self._inst.laser_on if on else self._inst.laser_off
        if self._failed(self._call(f"switch the laser {'on' if on else 'off'}", action)):
            return
        state = self._call("read the output state", self._inst.is_laser_on)
        self._laser_on = bool(state) if not self._failed(state) else on
        self.status.emit(f"Laser output {'ON' if self._laser_on else 'OFF'}")

    @pyqtSlot(float)
    def set_wavelength_nm(self, wavelength_nm: float) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot set the wavelength.")
            return
        self._call("set the wavelength", self._inst.set_wavelength_nm, wavelength_nm)

    @pyqtSlot(float, str)
    def set_power(self, value: float, unit: str) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot set the power.")
            return
        self._call("set the output power", self._inst.set_power, value, unit=unit)

    @pyqtSlot(str)
    def set_power_unit(self, unit: str) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot set the power unit.")
            return
        if self._failed(self._call("set the power unit", self._inst.set_power_unit, unit)):
            return
        self._power_unit = unit

    @pyqtSlot(str)
    def set_output_path(self, path: str) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot set the output path.")
            return
        self._call("set the output path", self._inst.set_output_path, path)

    # -- sweep ---------------------------------------------------------
    @pyqtSlot(object)
    def configure_sweep(self, config: SweepConfig) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot configure the sweep.")
            return
        if self._failed(self._call("configure the sweep", self._inst.configure_sweep,
                                   **config.as_kwargs())):
            return
        self.status.emit(
            f"Sweep configured: {config.start_nm:g}–{config.stop_nm:g} nm ({config.mode})"
        )

    @pyqtSlot()
    def check_sweep(self) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot check the sweep.")
            return
        result = self._call("check the sweep parameters", self._inst.check_sweep_params)
        if not self._failed(result):
            self.sweep_checked.emit(result)

    @pyqtSlot(object)
    def start_sweep(self, config: Optional[SweepConfig] = None) -> None:
        """Configure (if given), validate, then start the sweep."""
        if self._inst is None:
            self.error.emit("Not connected — cannot start a sweep.")
            return
        if config is not None:
            if self._failed(self._call("configure the sweep", self._inst.configure_sweep,
                                       **config.as_kwargs())):
                return
        result = self._call("check the sweep parameters", self._inst.check_sweep_params)
        if self._failed(result):
            return
        self.sweep_checked.emit(result)
        if result != "OK":
            self.error.emit(f"Sweep not started — {result}")
            return
        if self._failed(self._call("start the sweep", self._inst.start_sweep)):
            return
        self._was_sweeping = True
        self.sweep_running_changed.emit(True)
        self.status.emit("Sweep started")

    @pyqtSlot()
    def stop_sweep(self) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot stop the sweep.")
            return
        self._call("stop the sweep", self._inst.stop_sweep)
        self._was_sweeping = False
        self.sweep_running_changed.emit(False)
        self.status.emit("Sweep stopped")

    @pyqtSlot()
    def pause_sweep(self) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot pause the sweep.")
            return
        if not self._failed(self._call("pause the sweep", self._inst.pause_sweep)):
            self.status.emit("Sweep paused")

    @pyqtSlot()
    def continue_sweep(self) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot continue the sweep.")
            return
        if not self._failed(self._call("continue the sweep", self._inst.continue_sweep)):
            self.status.emit("Sweep continued")

    # -- misc instrument actions --------------------------------------
    @pyqtSlot()
    def reset(self) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot reset.")
            return
        if not self._failed(self._call("reset the instrument", self._inst.reset)):
            self.status.emit("Instrument reset (*RST)")

    @pyqtSlot()
    def flush_errors(self) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot read the error queue.")
            return
        errors = self._call("read the error queue", self._inst.flush_errors)
        if not self._failed(errors):
            self.status.emit("Error queue: " + "; ".join(errors))

    # -- polling -------------------------------------------------------
    @pyqtSlot()
    def _poll(self) -> None:
        """Sample the instrument state for the readouts and the live plot."""
        if self._inst is None:
            return
        try:
            wavelength_nm = self._inst.get_wavelength_nm()
            power = self._inst.get_power()
            sweeping = self._inst.is_sweeping()
            # The output state and path change rarely, so ask less often and
            # keep the polling loop cheap while a sweep is running.
            if self._poll_tick % 5 == 0:
                self._laser_on = self._inst.is_laser_on()
                self._output_path = self._inst.get_output_path()
        except Exception as exc:  # noqa: BLE001
            self._poll_timer.stop()
            self.error.emit(f"Polling stopped after a communication error: {exc}")
            return

        self._poll_tick += 1
        self.state_polled.emit({
            "wavelength_nm": wavelength_nm,
            "power": power,
            "power_unit": self._power_unit,
            "laser_on": getattr(self, "_laser_on", False),
            "output_path": getattr(self, "_output_path", ""),
            "sweeping": sweeping,
        })

        if sweeping != self._was_sweeping:
            self._was_sweeping = sweeping
            self.sweep_running_changed.emit(sweeping)
            if not sweeping:
                self.status.emit("Sweep finished")
