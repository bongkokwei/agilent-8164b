"""A software stand-in for :class:`~agilent_8164b.instrument.Agilent8164B`.

Lets the GUI be developed, demonstrated and tested on a machine with no
mainframe attached (and no VISA backend installed). It mimics the driver's
public API closely enough that the worker thread cannot tell the difference,
including a sweep engine that advances the wavelength in real time. It
invents no measurements: like the real module it only ever reports back what
it was told to do.
"""

import logging
import math
import time

logger = logging.getLogger(__name__)


class SimulatedAgilent8164B:
    """Duck-typed replacement for the real driver. No hardware required."""

    def __init__(
        self,
        resource_name: str = "SIM::INSTR",
        slot: int = 0,
        channel: int = 1,
        timeout_ms: int = 5000,
    ):
        logger.info("Opening simulated instrument %s", resource_name)
        self.slot = slot
        self.channel = channel
        self._resource_name = resource_name

        self._laser_on = False
        self._wavelength_nm = 1550.0
        self._power_setpoint_dbm = 0.0
        self._power_unit = "dBm"
        self._output_path = "HIGH"

        # Sweep engine state.
        self._sweep = {
            "start_nm": 1520.0,
            "stop_nm": 1580.0,
            "step_nm": 1.0,
            "speed_nm_s": 10.0,
            "dwell_s": 0.1,
            "cycles": 1,
            "mode": "step",
            "repeat": "oneway",
        }
        self._sweeping = False
        self._paused = False
        self._sweep_started_at = 0.0
        self._sweep_elapsed_s = 0.0

    # -- housekeeping --------------------------------------------------
    def identify(self) -> str:
        return "SIMULATED,8164B,0,1.0 (no hardware attached)"

    def reset(self) -> None:
        logger.info("Simulated reset")
        self.__init__(self._resource_name, self.slot, self.channel)

    def check_errors(self) -> str:
        return '+0,"No error"'

    def flush_errors(self) -> list:
        return [self.check_errors()]

    def close(self) -> None:
        logger.info("Closing simulated instrument")
        self._sweeping = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # -- laser on/off --------------------------------------------------
    def laser_on(self) -> None:
        logger.info("Laser output ON")
        self._laser_on = True

    def laser_off(self) -> None:
        logger.info("Laser output OFF")
        self._laser_on = False

    def is_laser_on(self) -> bool:
        return self._laser_on

    # -- wavelength ----------------------------------------------------
    def set_wavelength_nm(self, wavelength_nm: float) -> None:
        logger.info("Setting wavelength to %g nm", wavelength_nm)
        self._wavelength_nm = float(wavelength_nm)

    def get_wavelength_nm(self) -> float:
        self._advance_sweep()
        return self._wavelength_nm

    # -- power ---------------------------------------------------------
    def set_power_unit(self, unit: str = "dBm") -> None:
        self._power_unit = "dBm" if unit.lower() == "dbm" else "W"

    def get_power_unit(self) -> str:
        return self._power_unit

    def set_power(self, value: float, unit: str = "dBm") -> None:
        logger.info("Setting power to %g %s", value, unit)
        factor_mw = {"dbm": None, "mw": 1.0, "uw": 1e-3, "nw": 1e-6}[unit.lower()]
        if factor_mw is None:
            self._power_setpoint_dbm = float(value)
        else:
            milliwatts = max(float(value) * factor_mw, 1e-12)
            self._power_setpoint_dbm = 10.0 * math.log10(milliwatts)

    def get_power(self) -> float:
        """Read back the source power setting.

        The real ``:SOUR:POW?`` returns the commanded output power, not a
        measurement — a tunable laser source module has no detector. So this
        returns the setpoint unchanged, whatever the wavelength and whether
        or not the output is enabled.
        """
        if self._power_unit == "dBm":
            return self._power_setpoint_dbm
        return 10 ** (self._power_setpoint_dbm / 10.0) * 1e-3  # watts

    # -- output path ---------------------------------------------------
    def set_output_path(self, path: str) -> None:
        self._output_path = {
            "high": "HIGH",
            "highpower": "HIGH",
            "lowsse": "LOWS",
            "low": "LOWS",
            "both_high": "BHR",
            "bhr": "BHR",
            "both_low": "BLR",
            "blr": "BLR",
        }[path.lower()]

    def get_output_path(self) -> str:
        return self._output_path

    # -- sweep ---------------------------------------------------------
    def configure_sweep(
        self,
        start_nm: float,
        stop_nm: float,
        step_nm: float = 1.0,
        speed_nm_s: float = None,
        dwell_s: float = None,
        cycles: int = 1,
        mode: str = "step",
        repeat: str = "oneway",
    ) -> None:
        logger.info(
            "Configuring %s sweep: %g-%g nm, step=%g nm, cycles=%d, repeat=%s",
            mode, start_nm, stop_nm, step_nm, cycles, repeat,
        )
        self._sweep.update(
            start_nm=float(start_nm),
            stop_nm=float(stop_nm),
            step_nm=float(step_nm),
            speed_nm_s=speed_nm_s if speed_nm_s is not None else 10.0,
            dwell_s=dwell_s if dwell_s is not None else 0.1,
            cycles=int(cycles),
            mode=mode.lower(),
            repeat=repeat.lower(),
        )

    def check_sweep_params(self) -> str:
        sweep = self._sweep
        if sweep["stop_nm"] <= sweep["start_nm"]:
            return "stop wavelength must be greater than start wavelength"
        if sweep["step_nm"] <= 0:
            return "step size must be positive"
        if sweep["step_nm"] > (sweep["stop_nm"] - sweep["start_nm"]):
            return "step size larger than the sweep span"
        return "OK"

    def start_sweep(self) -> None:
        logger.info("Starting sweep")
        self._sweeping = True
        self._paused = False
        self._sweep_elapsed_s = 0.0
        self._sweep_started_at = time.monotonic()
        self._wavelength_nm = self._sweep["start_nm"]

    def stop_sweep(self) -> None:
        logger.info("Stopping sweep")
        self._sweeping = False
        self._paused = False

    def pause_sweep(self) -> None:
        if self._sweeping:
            self._advance_sweep()
            self._paused = True

    def continue_sweep(self) -> None:
        if self._sweeping and self._paused:
            self._paused = False
            self._sweep_started_at = time.monotonic()

    def is_sweeping(self) -> bool:
        self._advance_sweep()
        return self._sweeping

    def _advance_sweep(self) -> None:
        """Move the simulated wavelength along according to elapsed time."""
        if not self._sweeping:
            return

        now = time.monotonic()
        if not self._paused:
            self._sweep_elapsed_s += now - self._sweep_started_at
        self._sweep_started_at = now
        if self._paused:
            return

        sweep = self._sweep
        span_nm = sweep["stop_nm"] - sweep["start_nm"]
        if span_nm <= 0:
            self._sweeping = False
            return

        if sweep["mode"].startswith("cont"):
            speed_nm_s = max(sweep["speed_nm_s"], 1e-6)
            leg_duration_s = span_nm / speed_nm_s
        else:  # stepped: dwell at each point
            n_steps = max(int(span_nm / max(sweep["step_nm"], 1e-9)), 1)
            leg_duration_s = n_steps * max(sweep["dwell_s"], 1e-3)

        legs_per_cycle = 2 if sweep["repeat"].startswith("two") else 1
        total_s = leg_duration_s * legs_per_cycle * max(sweep["cycles"], 1)

        if self._sweep_elapsed_s >= total_s:
            self._sweeping = False
            self._wavelength_nm = sweep["stop_nm"] if legs_per_cycle == 1 else sweep["start_nm"]
            return

        leg_index = int(self._sweep_elapsed_s / leg_duration_s)
        fraction = (self._sweep_elapsed_s % leg_duration_s) / leg_duration_s
        if legs_per_cycle == 2 and leg_index % 2 == 1:
            fraction = 1.0 - fraction  # returning leg of a two-way sweep

        wavelength_nm = sweep["start_nm"] + fraction * span_nm
        if not sweep["mode"].startswith("cont"):
            step_nm = max(sweep["step_nm"], 1e-9)
            wavelength_nm = sweep["start_nm"] + round(
                (wavelength_nm - sweep["start_nm"]) / step_nm
            ) * step_nm
        self._wavelength_nm = wavelength_nm
