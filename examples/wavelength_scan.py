"""
Wavelength scan example for the Agilent/Keysight 8164B.

Sweeps the laser output over a range of wavelengths, optionally logging the
measured power at each step. Edit the parameters in the CONFIG section below
to match your setup, then run:

    python wavelength_scan.py
"""

import csv
import time

from agilent_8164b import Agilent8164B

# ----------------------------- CONFIG -------------------------------------
RESOURCE_NAME = "GPIB0::21::INSTR"  # VISA resource string for the mainframe
SLOT = 0                            # Mainframe slot the laser module sits in
CHANNEL = 1                         # Channel number (dual-wavelength sources)

START_NM = 1520.0                   # Scan start wavelength (nm)
STOP_NM = 1580.0                    # Scan stop wavelength (nm)
STEP_NM = 1.0                       # Wavelength step size (nm)
DWELL_S = 0.5                       # Settle time at each step (s)

OUTPUT_POWER = 1.5                  # Laser output power
OUTPUT_POWER_UNIT = "dBm"           # 'dBm', 'mW', 'uW', or 'nW'
OUTPUT_PATH = "high"                # 'high', 'lowsse', 'both_high', 'both_low'

CSV_PATH = "wavelength_scan.csv"    # Set to None to skip writing a CSV
# ---------------------------------------------------------------------------


def wavelength_steps(start_nm: float, stop_nm: float, step_nm: float):
    """Yield wavelengths from start_nm to stop_nm (inclusive), in steps of
    step_nm. Works for both increasing and decreasing sweeps."""
    if step_nm == 0:
        raise ValueError("STEP_NM must be non-zero")
    direction = 1 if stop_nm >= start_nm else -1
    step_nm = abs(step_nm) * direction

    n_steps = int(round((stop_nm - start_nm) / step_nm))
    for i in range(n_steps + 1):
        yield start_nm + i * step_nm


def main():
    results = []

    with Agilent8164B(RESOURCE_NAME, slot=SLOT, channel=CHANNEL) as laser:
        print("Connected to:", laser.identify())

        laser.set_power_unit(OUTPUT_POWER_UNIT)
        laser.set_power(OUTPUT_POWER, unit=OUTPUT_POWER_UNIT)
        laser.set_output_path(OUTPUT_PATH)
        laser.laser_on()

        try:
            for target_nm in wavelength_steps(START_NM, STOP_NM, STEP_NM):
                laser.set_wavelength_nm(target_nm)
                time.sleep(DWELL_S)

                actual_nm = laser.get_wavelength_nm()
                power = laser.get_power()
                print(f"target={target_nm:.3f} nm  actual={actual_nm:.3f} nm  "
                      f"power={power:.3f} {OUTPUT_POWER_UNIT}")
                results.append((target_nm, actual_nm, power))

            print("Error queue:", laser.flush_errors())
        finally:
            laser.laser_off()

    if CSV_PATH:
        with open(CSV_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["target_nm", "actual_nm", f"power_{OUTPUT_POWER_UNIT}"])
            writer.writerows(results)
        print(f"Wrote {len(results)} rows to {CSV_PATH}")


if __name__ == "__main__":
    main()
