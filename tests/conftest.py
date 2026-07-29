"""Test fixtures: a stub VISA session so the GUI can be driven without hardware.

The stub sits at the lowest I/O boundary — it replaces the object PyVISA's
``open_resource()`` hands back, so the real :class:`Agilent8164B` builds and
parses every SCPI string under test. Nothing in the shipped package knows the
instrument is missing.
"""

import re

import pytest


class StubVisaResource:
    """Answers the subset of the 8164B SCPI set that the GUI exercises.

    Deliberately literal: it records the exact strings written to it (see
    :attr:`writes`) and only recognises commands the mainframe would, so a
    typo in the driver shows up as an unanswered query rather than passing.
    """

    #: Number of ``:WAV:SWE:STAT?`` polls a started sweep stays "running" for.
    #: A fixed count keeps sweep tests deterministic — no wall-clock waiting.
    SWEEP_POLLS = 3

    def __init__(self):
        self.timeout = None
        self.read_termination = None
        self.write_termination = None
        self.closed = False

        self.writes: list[str] = []
        self._wavelength_m = 1550e-9
        self._power_dbm = 0.0
        self._output_on = False
        self._path = "HIGH"
        self._power_unit_code = 0          # 0 = dBm, 1 = W
        self._sweep_start_m = 1520e-9
        self._sweep_stop_m = 1580e-9
        self._sweeping = False
        self._sweep_polls_left = 0
        self._input_trigger = "IGN"
        self._trigger_config = "DIS"

    # -- PyVISA surface ------------------------------------------------
    def write(self, command: str) -> None:
        self.writes.append(command)
        self._apply(command)

    def query(self, command: str) -> str:
        self.writes.append(command)
        response = self._respond(command)
        if response is None:
            raise AssertionError(f"stub received an unrecognised query: {command!r}")
        return response

    def close(self) -> None:
        self.closed = True

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _value_nm(command: str) -> float:
        """Pull the numeric argument off a '...:WAV 1550.0NM' style command."""
        match = re.search(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*NM", command, re.I)
        return float(match.group(1)) if match else 0.0

    def _apply(self, command: str) -> None:
        upper = command.upper()
        if ":OUTP" in upper and ":STAT" in upper:
            self._output_on = upper.rstrip().endswith("1")
        elif ":OUTP" in upper and ":PATH" in upper:
            self._path = upper.rsplit(" ", 1)[-1]
        elif ":WAV:SWE:STAT" in upper:
            action = upper.rsplit(" ", 1)[-1]
            if action == "START":
                self._sweeping = True
                self._sweep_polls_left = self.SWEEP_POLLS
            elif action == "STOP":
                self._sweeping = False
                self._sweep_polls_left = 0
        elif ":WAV:SWE:STAR" in upper:
            self._sweep_start_m = self._value_nm(command) * 1e-9
        elif ":WAV:SWE:STOP" in upper:
            self._sweep_stop_m = self._value_nm(command) * 1e-9
        elif ":WAV:SWE" in upper:
            pass  # MODE/STEP/DWEL/SPE/CYCL/REP are recorded, not modelled
        elif ":WAV" in upper:
            self._wavelength_m = self._value_nm(command) * 1e-9
        elif ":POW:UNIT" in upper:
            self._power_unit_code = 0 if upper.rsplit(" ", 1)[-1] == "DBM" else 1
        elif ":POW" in upper:
            match = re.search(r"([-+]?\d*\.?\d+)\s*(DBM|MW|UW|NW)", command, re.I)
            if match:
                self._power_dbm = float(match.group(1))  # dBm assumed in tests
        elif ":CONF" in upper and upper.startswith(":TRIG"):
            self._trigger_config = upper.rsplit(" ", 1)[-1]
        elif ":INP" in upper and upper.startswith(":TRIG"):
            self._input_trigger = upper.rsplit(" ", 1)[-1]
        elif upper.startswith("*RST"):
            self.__init__()
        elif upper.startswith("*CLS"):
            pass

    def _respond(self, command: str):
        upper = command.upper()
        if upper.startswith("*IDN?"):
            return "HEWLETT-PACKARD,8164B,MY12345678,1.0"
        if upper.startswith(":SYST:ERR?"):
            return '+0,"No error"'
        if ":OUTP" in upper and ":STAT?" in upper:
            return "+1" if self._output_on else "+0"
        if ":OUTP" in upper and ":PATH?" in upper:
            return self._path
        if ":WAV:SWE:STAT?" in upper:
            if self._sweeping:
                self._sweep_polls_left -= 1
                if self._sweep_polls_left <= 0:
                    self._sweeping = False
            return "+1" if self._sweeping else "+0"
        if ":WAV:SWE:CHEC?" in upper:
            if self._sweep_stop_m <= self._sweep_start_m:
                return "+257,End wavelength must be greater than start wavelength"
            return "+0,OK"
        if upper.startswith(":TRIG") and ":CONF?" in upper:
            return self._trigger_config
        if upper.startswith(":TRIG") and ":INP?" in upper:
            return self._input_trigger
        if ":WAV?" in upper:
            return f"{self._wavelength_m:.12E}"
        if ":POW:UNIT?" in upper:
            return f"+{self._power_unit_code}"
        if ":POW?" in upper:
            return f"{self._power_dbm:+.3E}"
        return None


@pytest.fixture
def visa(monkeypatch):
    """Patch PyVISA so Agilent8164B opens a StubVisaResource. Returns the stub."""
    import pyvisa

    resource = StubVisaResource()

    class StubResourceManager:
        def open_resource(self, resource_name, *args, **kwargs):
            return resource

        def list_resources(self):
            return ("GPIB0::20::INSTR",)

    monkeypatch.setattr(pyvisa, "ResourceManager", StubResourceManager)
    return resource
