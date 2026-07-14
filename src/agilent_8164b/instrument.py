"""
Control class for the Keysight/Agilent 8164B Lightwave Measurement System
(tunable laser source module), based on the SCPI command set in the
mainframe programming guide.

Requires PyVISA (`pip install pyvisa`) and a VISA backend (NI-VISA,
Keysight IO Libraries, or pyvisa-py) plus the appropriate interface driver
(GPIB, LAN/VXI-11, or LAN/socket).
"""

import pyvisa


class Agilent8164B:
    """Interface to a laser source module hosted in an 8164B mainframe.

    Parameters
    ----------
    resource_name : str
        VISA resource string, e.g. "GPIB0::20::INSTR" or
        "TCPIP0::192.168.1.10::inst0::INSTR".
    slot : int
        Mainframe slot number the laser module is installed in.
    channel : int
        Channel number (for dual-wavelength sources); usually 1.
    timeout_ms : int
        VISA timeout in milliseconds.
    """

    def __init__(
        self,
        resource_name: str,
        slot: int = 0,
        channel: int = 1,
        timeout_ms: int = 5000,
    ):
        self.slot = slot
        self.channel = channel
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(resource_name)
        self._inst.timeout = timeout_ms
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"
        self._inst.write("*CLS")  # clear status/error queue on connect

    # -- low-level helpers --------------------------------------------
    def _prefix(self) -> str:
        return f":SOUR{self.slot}:CHAN{self.channel}"

    def _outp_prefix(self) -> str:
        return f":OUTP{self.slot}:CHAN{self.channel}"

    def _write(self, cmd: str) -> None:
        self._inst.write(cmd)

    def _query(self, cmd: str) -> str:
        return self._inst.query(cmd).strip()

    def check_errors(self) -> str:
        """Return the next entry from the SCPI error queue."""
        return self._query(":SYST:ERR?")

    def flush_errors(self) -> list:
        """Drain and return all entries currently in the error queue."""
        errors = []
        while True:
            err = self.check_errors()
            errors.append(err)
            if err.startswith("+0") or err.startswith("0,"):
                break
        return errors

    def identify(self) -> str:
        return self._query("*IDN?")

    def reset(self) -> None:
        self._write("*RST")

    # -- laser on/off ----------------------------------------------------
    def laser_on(self) -> None:
        self._write(f"{self._outp_prefix()}:STAT 1")

    def laser_off(self) -> None:
        self._write(f"{self._outp_prefix()}:STAT 0")

    def is_laser_on(self) -> bool:
        return bool(int(self._query(f"{self._outp_prefix()}:STAT?")))

    # -- wavelength (metres, as per SCPI default) -------------------------
    def set_wavelength_nm(self, wavelength_nm: float) -> None:
        self._write(f"{self._prefix()}:WAV {wavelength_nm}NM")

    def get_wavelength_nm(self) -> float:
        metres = float(self._query(f"{self._prefix()}:WAV?"))
        return metres * 1e9

    # -- native wavelength sweep --------------------------------------
    # Uses the module's built-in sweep engine (:WAV:SWE subsystem) rather
    # than stepping the wavelength from the host.
    _SWEEP_MODE = {
        "step": "STEP",
        "stepped": "STEP",
        "continuous": "CONT",
        "cont": "CONT",
        "manual": "MAN",
    }

    _SWEEP_REPEAT = {
        "oneway": "ONEW",
        "one_way": "ONEW",
        "twoway": "TWOW",
        "two_way": "TWOW",
    }

    def _sweep_prefix(self) -> str:
        return f"{self._prefix()}:WAV:SWE"

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
        """Configure (but do not start) a native wavelength sweep.

        mode: 'step' (stepped sweep, uses step_nm/dwell_s) or 'continuous'
        (uses speed_nm_s). repeat: 'oneway' or 'twoway'.
        """
        prefix = self._sweep_prefix()
        self._write(f"{prefix}:MODE {self._SWEEP_MODE[mode.lower()]}")
        self._write(f"{prefix}:STAR {start_nm}NM")
        self._write(f"{prefix}:STOP {stop_nm}NM")
        self._write(f"{prefix}:REP {self._SWEEP_REPEAT[repeat.lower()]}")
        self._write(f"{prefix}:CYCL {cycles}")
        if mode.lower() in ("step", "stepped"):
            self._write(f"{prefix}:STEP {step_nm}NM")
            if dwell_s is not None:
                self._write(f"{prefix}:DWEL {dwell_s}S")
        elif mode.lower() in ("continuous", "cont"):
            if speed_nm_s is not None:
                self._write(f"{prefix}:SPE {speed_nm_s}NM/S")

    def start_sweep(self) -> None:
        self._write(f"{self._sweep_prefix()}:STAT START")

    def stop_sweep(self) -> None:
        self._write(f"{self._sweep_prefix()}:STAT STOP")

    def pause_sweep(self) -> None:
        self._write(f"{self._sweep_prefix()}:STAT PAUS")

    def continue_sweep(self) -> None:
        self._write(f"{self._sweep_prefix()}:STAT CONT")

    def is_sweeping(self) -> bool:
        """Returns True while a sweep is running (the instrument only
        reports a running/not-running state, not paused/waiting)."""
        return bool(int(float(self._query(f"{self._sweep_prefix()}:STAT?"))))

    def check_sweep_params(self) -> str:
        """Validate the currently configured sweep parameters.

        Returns "OK" if the sweep is configured correctly, otherwise a
        string describing the configuration problem (e.g. start/stop
        wavelength, step size, trigger frequency, or cycle time issues).
        """
        return self._query(f"{self._sweep_prefix()}:CHEC?")

    # -- dual output path (High Power / Low SSE) ----------------------
    # Applies to tunable laser modules with two physical outputs.
    # This is distinct from dual-WAVELENGTH sources ([l] parameter above).
    _PATH_SET = {
        "high": "HIGH",
        "highpower": "HIGH",
        "lowsse": "LOWS",
        "low": "LOWS",
        "bhr": "BHR",
        "both_high": "BHR",
        "blr": "BLR",
        "both_low": "BLR",
    }

    def set_output_path(self, path: str) -> None:
        """path: 'high' (High Power regulated), 'lowsse' (Low SSE regulated),
        'both_high' (both active, High Power regulated), or
        'both_low' (both active, Low SSE regulated)."""
        code = self._PATH_SET[path.lower()]
        self._write(f"{self._outp_prefix()}:PATH {code}")

    def get_output_path(self) -> str:
        return self._query(f"{self._outp_prefix()}:PATH?").strip()

    # -- power -------------------------------------------------------
    def set_power_unit(self, unit: str = "dBm") -> None:
        """unit: 'dBm' or 'W'."""
        code = "DBM" if unit.lower() == "dbm" else "W"
        self._write(f"{self._prefix()}:POW:UNIT {code}")

    def get_power_unit(self) -> str:
        code = self._query(f"{self._prefix()}:POW:UNIT?")
        return "dBm" if code.strip() == "+0" or code.strip() == "0" else "W"

    def set_power(self, value: float, unit: str = "dBm") -> None:
        """Set output power. unit: 'dBm', 'mW', 'uW', or 'nW'."""
        suffix = {"dbm": "DBM", "mw": "MW", "uw": "UW", "nw": "NW"}[unit.lower()]
        self._write(f"{self._prefix()}:POW {value}{suffix}")

    def get_power(self) -> float:
        """Returns power in the currently selected unit (dBm or W)."""
        return float(self._query(f"{self._prefix()}:POW?"))

    # -- cleanup -------------------------------------------------------
    def close(self) -> None:
        self._inst.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
