"""Driver-level tests against the stub VISA session."""

from agilent_8164b import Agilent8164B


def test_set_wavelength_does_not_wait_by_default(visa):
    laser = Agilent8164B("GPIB0::20::INSTR")
    visa.writes.clear()
    laser.set_wavelength_nm(1550.5)
    assert visa.writes == [":SOUR0:CHAN1:WAV 1550.5NM"]


def test_set_wavelength_wait_blocks_on_opc(visa):
    laser = Agilent8164B("GPIB0::20::INSTR")
    visa.writes.clear()
    laser.set_wavelength_nm(1550.5, wait=True)
    assert visa.writes == [":SOUR0:CHAN1:WAV 1550.5NM", "*OPC?"]
