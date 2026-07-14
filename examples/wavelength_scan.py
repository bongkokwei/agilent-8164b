"""
Wavelength scan example for the Agilent/Keysight 8164B.

Drives the laser module's built-in wavelength sweep engine (rather than
stepping the wavelength from the host) and samples wavelength/power while
the sweep runs. Edit the parameters in the CONFIG section below to match
your setup, then run:

    python wavelength_scan.py
"""

import csv
import time

from agilent_8164b import Agilent8164B

# ----------------------------- CONFIG -------------------------------------
RESOURCE_NAME = "GPIB0::21::INSTR"  # VISA resource string for the mainframe
SLOT = 0                            # Mainframe slot the laser module sits in
CHANNEL = 1                         # Channel number (dual-wavelength sources)

START_NM = 1520.0                   # Sweep start wavelength (nm)
STOP_NM = 1580.0                    # Sweep stop wavelength (nm)

SWEEP_MODE = "step"                 # 'step' (stepped) or 'continuous'
STEP_NM = 1.0                       # Step size (nm), used when SWEEP_MODE='step'
DWELL_S = 0.1                       # Dwell time per step (s), used when SWEEP_MODE='step'
SPEED_NM_S = 10.0                   # Sweep speed (nm/s), used when SWEEP_MODE='continuous'
CYCLES = 1                          # Number of sweep repeats
REPEAT_MODE = "oneway"              # 'oneway' or 'twoway'

OUTPUT_POWER = 1.5                  # Laser output power
OUTPUT_POWER_UNIT = "dBm"           # 'dBm', 'mW', 'uW', or 'nW'
OUTPUT_PATH = "high"                # 'high', 'lowsse', 'both_high', 'both_low'

POLL_INTERVAL_S = 0.2               # How often to sample wavelength/power while sweeping
SWEEP_TIMEOUT_S = 120.0             # Safety cutoff in case the sweep never reports "stopped"

CSV_PATH = "wavelength_scan.csv"    # Set to None to skip writing a CSV
# ---------------------------------------------------------------------------


def main():
    results = []

    with Agilent8164B(RESOURCE_NAME, slot=SLOT, channel=CHANNEL) as laser:
        print("Connected to:", laser.identify())

        laser.set_power_unit(OUTPUT_POWER_UNIT)
        laser.set_power(OUTPUT_POWER, unit=OUTPUT_POWER_UNIT)
        laser.set_output_path(OUTPUT_PATH)

        laser.configure_sweep(
            START_NM,
            STOP_NM,
            step_nm=STEP_NM,
            speed_nm_s=SPEED_NM_S,
            dwell_s=DWELL_S,
            cycles=CYCLES,
            mode=SWEEP_MODE,
            repeat=REPEAT_MODE,
        )

        params_ok = laser.check_sweep_params()
        if params_ok != "OK":
            raise RuntimeError(f"Sweep configuration problem: {params_ok}")

        laser.laser_on()
        laser.start_sweep()

        start_time = time.monotonic()
        try:
            while laser.is_sweeping():
                if time.monotonic() - start_time > SWEEP_TIMEOUT_S:
                    print("Sweep timeout exceeded, stopping.")
                    break

                wavelength_nm = laser.get_wavelength_nm()
                power = laser.get_power()
                print(f"wavelength={wavelength_nm:.3f} nm  "
                      f"power={power:.3f} {OUTPUT_POWER_UNIT}")
                results.append((wavelength_nm, power))

                time.sleep(POLL_INTERVAL_S)

            print("Error queue:", laser.flush_errors())
        finally:
            laser.stop_sweep()
            laser.laser_off()

    if CSV_PATH:
        with open(CSV_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["wavelength_nm", f"power_{OUTPUT_POWER_UNIT}"])
            writer.writerows(results)
        print(f"Wrote {len(results)} rows to {CSV_PATH}")


if __name__ == "__main__":
    main()
