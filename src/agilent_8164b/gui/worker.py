"""Instrument worker that keeps all VISA traffic off the GUI thread.

Every call into the driver blocks for as long as the mainframe takes to
answer (milliseconds on GPIB, occasionally much longer over LAN), so the
driver instance lives entirely inside a :class:`QThread`. The window talks to
it only through queued signals and slots, which means no widget ever waits on
I/O and the interface stays responsive during a long sweep.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
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
    cycles: int = 1          # 0 sweeps until it is stopped
    mode: str = "step"       # 'step' or 'continuous'
    repeat: str = "oneway"   # 'oneway' or 'twoway'

    def as_kwargs(self) -> dict:
        return asdict(self)


class _OutputGuard:
    """Remembers an output state so a later step can hand it back."""

    def __init__(self, worker: "LaserWorker", was_on: bool):
        self._worker = worker
        self.was_on = was_on

    def restore(self) -> None:
        if self.was_on:
            self._worker._restore_output()


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
    trigger_state_read = pyqtSignal(str, str)  # input mode, mainframe config
    modulation_mode_read = pyqtSignal(str)   # source name, or "off"

    def __init__(self, poll_interval_ms: int = 200):
        super().__init__()
        self._inst = None
        self._poll_interval_ms = poll_interval_ms
        self._poll_timer: Optional[QTimer] = None
        self._was_sweeping = False
        self._power_unit = "dBm"
        self._poll_tick = 0
        self._poll_failures = 0
        self._optional_failures: dict = {}
        self._hold_output_on = False
        self._hold_attempts = 0
        self._hold_saw_sweeping = False
        self._hold_armed_tick = 0
        self._hold_next_attempt_tick = 0

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
        self._poll_failures = 0
        self._optional_failures = {}
        self.connected.emit(idn)
        self.status.emit(f"Connected to {resource_name}")
        self.read_trigger_state()
        self.read_modulation_mode()
        if self._poll_timer is not None:
            self._poll_timer.start()

    @pyqtSlot()
    def disconnect_from(self) -> None:
        """Close the session, leaving the output off behind us."""
        if self._poll_timer is not None:
            self._poll_timer.stop()
        self._release_output_hold()
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

    @contextmanager
    def _output_preserved(self):
        """Leave the optical output in the state it was in on entry.

        Writing the sweep parameters makes the module retune, and it switches
        the output off while it does — so configuring or starting a sweep
        silently kills the beam the user had just enabled. The output is only
        ever restored, never enabled on its own: if it was off on entry it
        stays off.

        Yields a guard the caller can ask to restore early, and whose
        :attr:`~_OutputGuard.was_on` says whether there is anything to restore.
        """
        state = self._call("read the output state", self._inst.is_laser_on)
        guard = _OutputGuard(self, bool(state) if not self._failed(state) else False)
        try:
            yield guard
        finally:
            guard.restore()

    def _release_output_hold(self) -> None:
        """Stop watching the output — the user or the sweep has moved on."""
        self._hold_output_on = False
        self._hold_attempts = 0
        self._hold_saw_sweeping = False
        self._hold_next_attempt_tick = 0

    def _restore_output(self) -> bool:
        """Switch the output back on if the instrument dropped it.

        Returns False only when the module has actually refused: ``:OUTP:STAT
        1`` is accepted silently by a module that then ignores it, so the
        state is read back and, if it is still off, the SCPI error queue is
        asked why. An empty error queue means the module is simply taking its
        time, which is not a refusal — the caller may try again.
        """
        state = self._call("read the output state", self._inst.is_laser_on)
        if self._failed(state) or state:
            return True
        if self._failed(self._call("switch the laser back on", self._inst.laser_on)):
            return True
        state = self._call("read the output state", self._inst.is_laser_on)
        if self._failed(state) or state:
            self._laser_on = True
            self.status.emit("Output switched back on after the sweep turned it off")
            return True

        reason = self._call("read the error queue", self._inst.check_errors)
        if self._failed(reason) or self._no_error(reason):
            return True  # too early to tell; the state may still catch up
        self.error.emit(
            f"The module refused to switch the output back on: {reason}. "
            "The sweep is running with the output off."
        )
        return False

    @staticmethod
    def _no_error(entry: str) -> bool:
        """True for the 'queue empty' reply, whichever way it is spelled."""
        return entry.strip().lstrip("+").startswith("0")

    # -- output control ------------------------------------------------
    @pyqtSlot(bool)
    def set_laser_on(self, on: bool) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot switch the laser.")
            return
        # An explicit command is the last word on the output: it replaces
        # whatever the sweep was holding, in either direction.
        self._release_output_hold()
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

    # -- modulation ----------------------------------------------------
    @pyqtSlot(str)
    def set_modulation_mode(self, mode: str) -> None:
        """Select a modulation source and enable it, or switch modulation off.

        The instrument keeps the source and the on/off state separately, so
        one dropdown maps onto two commands.
        """
        if self._inst is None:
            self.error.emit("Not connected — cannot set the modulation mode.")
            return
        if mode == "off":
            if not self._failed(self._call("switch modulation off",
                                           self._inst.set_modulation_on, False)):
                self.status.emit("Modulation off")
            return
        if self._failed(self._call("set the modulation source",
                                   self._inst.set_modulation_source, mode)):
            return
        if not self._failed(self._call("enable modulation",
                                       self._inst.set_modulation_on, True)):
            self.status.emit(f"Modulation: {mode.replace('_', ' ')}")

    @pyqtSlot()
    def read_modulation_mode(self) -> None:
        """Report the instrument's modulation mode, failing quietly."""
        if self._inst is None:
            return
        try:
            mode = (
                self._inst.get_modulation_source()
                if self._inst.is_modulation_on()
                else "off"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the modulation mode: %s", exc)
            return
        self.modulation_mode_read.emit(mode)

    # -- triggers ------------------------------------------------------
    @pyqtSlot(str)
    def set_input_trigger_mode(self, mode: str) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot set the input trigger mode.")
            return
        if not self._failed(self._call("set the input trigger mode",
                                       self._inst.set_input_trigger_mode, mode)):
            self.status.emit(f"Input BNC: {mode.replace('_', ' ')}")

    @pyqtSlot(str)
    def set_trigger_configuration(self, config: str) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot set the trigger configuration.")
            return
        if not self._failed(self._call("set the trigger configuration",
                                       self._inst.set_trigger_configuration, config)):
            self.status.emit(f"Trigger routing: {config}")

    @pyqtSlot()
    def read_trigger_state(self) -> None:
        """Report what the instrument currently has configured.

        Failures are logged rather than reported: modules that do not
        implement the trigger subsystem should not greet the user with an
        error dialog at connect time.
        """
        if self._inst is None:
            return
        try:
            self.trigger_state_read.emit(
                self._inst.get_input_trigger_mode(),
                self._inst.get_trigger_configuration(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read the trigger configuration: %s", exc)

    # -- sweep ---------------------------------------------------------
    @pyqtSlot(object)
    def configure_sweep(self, config: SweepConfig) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot configure the sweep.")
            return
        with self._output_preserved():
            failed = self._failed(self._call("configure the sweep",
                                             self._inst.configure_sweep,
                                             **config.as_kwargs()))
        if failed:
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
        # Everything from here to the running sweep can make the module drop
        # the output; the guard hands it back at the end, whichever step it is.
        with self._output_preserved() as output:
            if config is not None:
                if self._failed(self._call("configure the sweep",
                                           self._inst.configure_sweep,
                                           **config.as_kwargs())):
                    return
            result = self._call("check the sweep parameters",
                                self._inst.check_sweep_params)
            if self._failed(result):
                return
            self.sweep_checked.emit(result)
            if result != "OK":
                self.error.emit(f"Sweep not started — {result}")
                return
            # The sweep must begin with the output already on: a module that
            # latches the output state at the start command would otherwise
            # sweep dark however often it is switched back on afterwards.
            output.restore()
            if self._failed(self._call("start the sweep", self._inst.start_sweep)):
                return
        if output.was_on:
            # The drop can also come after the start command is acknowledged,
            # while the module retunes to the start wavelength — too late for
            # the check above to see it, so the poll loop keeps watch.
            self._hold_output_on = True
            self._hold_attempts = 0
            self._hold_saw_sweeping = False
            self._hold_armed_tick = self._poll_tick
            # Give the module time to reach the start wavelength before
            # reading anything into an output that is off.
            self._hold_next_attempt_tick = self._poll_tick + self.HOLD_RETRY_POLLS
        self._was_sweeping = True
        self.sweep_running_changed.emit(True)
        self.status.emit("Sweep started")

    @pyqtSlot()
    def stop_sweep(self) -> None:
        if self._inst is None:
            self.error.emit("Not connected — cannot stop the sweep.")
            return
        self._release_output_hold()
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
    #: Consecutive failures of the core readings tolerated before the loop
    #: gives up. A single timeout is normal while the module is busy, and
    #: stopping on it leaves the readout dead for the rest of the session.
    MAX_POLL_FAILURES = 5

    #: How many times the output is switched back on during one sweep before
    #: the hold gives up. Bounded so a module that insists on keeping the
    #: output off is argued with briefly, then left alone.
    MAX_HOLD_ATTEMPTS = 3

    #: Polls the hold waits for the sweep to appear before deciding it never
    #: started. At the default 200 ms tick that is a five second grace.
    HOLD_GRACE_POLLS = 25

    #: Polls between attempts. A module is off while it tunes to the start
    #: wavelength and switches back on by itself when it arrives, so the hold
    #: waits a second before each try instead of hammering it mid-retune.
    HOLD_RETRY_POLLS = 5

    #: Failures of one optional reading before it is dropped for this session.
    #: Not every module implements every query — a single-output module has no
    #: :OUTP:PATH? — so those must not take the readout down with them.
    MAX_OPTIONAL_FAILURES = 3

    @pyqtSlot()
    def _poll(self) -> None:
        """Sample the instrument state for the readouts and the live plot."""
        if self._inst is None:
            return
        try:
            wavelength_nm = self._inst.get_wavelength_nm()
            power = self._inst.get_power()
            sweeping = self._inst.is_sweeping()
        except Exception as exc:  # noqa: BLE001
            self._on_poll_failure(exc)
            return
        self._poll_failures = 0

        # The output state and path change rarely, so ask less often and keep
        # the polling loop cheap while a sweep is running. While the hold is
        # armed the output state is worth every tick: the sooner a drop is
        # seen, the less of the sweep runs dark.
        if self._hold_output_on or self._poll_tick % 5 == 0:
            self._laser_on = self._read_optional(
                "output state", "is_laser_on", getattr(self, "_laser_on", False)
            )
        if self._poll_tick % 5 == 0:
            self._output_path = self._read_optional(
                "output path", "get_output_path", getattr(self, "_output_path", "")
            )
        if self._hold_output_on:
            self._enforce_output_hold(sweeping)

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

    def _enforce_output_hold(self, sweeping: bool) -> None:
        """Switch the output back on if the running sweep dropped it.

        Armed by :meth:`start_sweep`, and only when the output was on before
        the sweep — this hands back a state the user asked for, it never
        enables an output they left off. It is released as soon as the sweep
        ends, and it gives up after :attr:`MAX_HOLD_ATTEMPTS` so it can never
        sit there fighting an instrument, or an operator at the front panel,
        that wants the output off.
        """
        if sweeping:
            self._hold_saw_sweeping = True
        elif (self._hold_saw_sweeping
                or self._poll_tick - self._hold_armed_tick > self.HOLD_GRACE_POLLS):
            # Either the sweep has finished, or it never started.
            self._release_output_hold()
            return

        if getattr(self, "_laser_on", False):
            return
        if self._poll_tick < self._hold_next_attempt_tick:
            return
        if self._hold_attempts >= self.MAX_HOLD_ATTEMPTS:
            self._release_output_hold()
            self.error.emit(
                "The module keeps switching the output off during the sweep, "
                f"over {self.MAX_HOLD_ATTEMPTS} attempts to switch it back on. "
                "Left off — check the error queue and the sweep mode."
            )
            return
        self._hold_attempts += 1
        self._hold_next_attempt_tick = self._poll_tick + self.HOLD_RETRY_POLLS
        if not self._restore_output():
            # The module said why, so there is nothing to be gained by asking
            # it again — the reason is already in front of the user.
            self._release_output_hold()

    def _on_poll_failure(self, exc: Exception) -> None:
        """Absorb a failed poll, giving up only once they keep failing.

        A timeout leaves the session out of step — the late response would be
        read as the answer to the next query — so the interface is cleared
        before the next tick rather than carrying the confusion forward.
        """
        self._poll_failures += 1
        try:
            self._inst.clear_interface()
        except Exception as clear_exc:  # noqa: BLE001
            logger.warning("Could not clear the interface: %s", clear_exc)
        if self._poll_failures < self.MAX_POLL_FAILURES:
            logger.warning(
                "Poll %d of %d failed, retrying: %s",
                self._poll_failures, self.MAX_POLL_FAILURES, exc,
            )
            return
        if self._poll_timer is not None:
            self._poll_timer.stop()
        self.error.emit(
            f"Polling stopped after {self._poll_failures} failed attempts: {exc}"
        )

    def _read_optional(self, description: str, method_name: str, fallback):
        """Read something the readout can do without, keeping the last value.

        Modules differ in what they implement, and a query the module does not
        answer must not stop the wavelength and power from updating. After
        :attr:`MAX_OPTIONAL_FAILURES` tries the reading is dropped for the rest
        of the session instead of costing a timeout on every poll.
        """
        if self._optional_failures.get(description, 0) >= self.MAX_OPTIONAL_FAILURES:
            return fallback
        try:
            value = getattr(self._inst, method_name)()
        except Exception as exc:  # noqa: BLE001
            failures = self._optional_failures.get(description, 0) + 1
            self._optional_failures[description] = failures
            try:
                self._inst.clear_interface()
            except Exception as clear_exc:  # noqa: BLE001
                logger.warning("Could not clear the interface: %s", clear_exc)
            if failures >= self.MAX_OPTIONAL_FAILURES:
                message = (
                    f"Giving up on the {description}: the module did not "
                    f"answer ({exc}). Wavelength and power keep updating."
                )
                logger.warning("%s", message)
                self.status.emit(message)
            else:
                logger.warning("Could not read the %s: %s", description, exc)
            return fallback
        self._optional_failures[description] = 0
        return value
