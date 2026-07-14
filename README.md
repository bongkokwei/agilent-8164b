# agilent-8164b

Python driver for the Keysight/Agilent 8164B Lightwave Measurement System
(tunable laser source module), built on [PyVISA](https://pyvisa.readthedocs.io/)
and the SCPI command set from the mainframe programming guide.

## Requirements

- Python >= 3.8
- [PyVISA](https://pyvisa.readthedocs.io/) plus a VISA backend:
  NI-VISA, Keysight IO Libraries Suite, or [pyvisa-py](https://pyvisa-py.readthedocs.io/)
- The appropriate interface driver for how you talk to the instrument
  (GPIB, LAN/VXI-11, or LAN/socket)

## Installation

Install from source in editable mode:

```bash
git clone https://github.com/bongkokwei/agilent-8164b.git
cd agilent-8164b
pip install -e .
```

To also pull in the extra dependencies used by the example scripts:

```bash
pip install -e ".[examples]"
```

## Usage

```python
from agilent_8164b import Agilent8164B

with Agilent8164B("GPIB0::21::INSTR", slot=0, channel=1) as laser:
    print(laser.identify())
    laser.set_wavelength_nm(1550.0)
    laser.set_power(1.5, unit="dBm")
    laser.set_output_path("high")  # or "lowsse", "both_high", "both_low"
    laser.laser_on()
    print("Wavelength (nm):", laser.get_wavelength_nm())
    print("Power:", laser.get_power(), laser.get_power_unit())
    print("Output path:", laser.get_output_path())
    print("Error queue:", laser.flush_errors())
```

### API overview

| Method | Description |
| --- | --- |
| `identify()` | Query `*IDN?` |
| `reset()` | Send `*RST` |
| `laser_on()` / `laser_off()` / `is_laser_on()` | Control/query output state |
| `set_wavelength_nm(nm)` / `get_wavelength_nm()` | Set/get wavelength in nm |
| `set_power(value, unit)` / `get_power()` | Set/get output power (`dBm`, `mW`, `uW`, `nW`) |
| `set_power_unit(unit)` / `get_power_unit()` | Set/get the power display unit |
| `set_output_path(path)` / `get_output_path()` | Set/get output path (`high`, `lowsse`, `both_high`, `both_low`) — for dual-output modules |
| `check_errors()` / `flush_errors()` | Read the SCPI error queue |
| `close()` | Close the VISA session (also called automatically via `with`) |

## Examples

See [`examples/wavelength_scan.py`](examples/wavelength_scan.py) for a
wavelength sweep script. Edit the parameters in the `CONFIG` section at the
top of the file (VISA resource string, scan range/step, dwell time, output
power, and output path), then run:

```bash
python examples/wavelength_scan.py
```

The script steps the laser across the configured wavelength range, prints
the target/actual wavelength and measured power at each step, and (unless
disabled) writes the results to a CSV file.

## Project layout

```
src/agilent_8164b/   # installable package
  __init__.py
  instrument.py       # Agilent8164B driver class
examples/
  wavelength_scan.py  # wavelength scan example with editable parameters
```

## License

MIT
