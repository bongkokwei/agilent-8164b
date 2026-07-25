"""Command-line entry point for the laser GUI."""

from __future__ import annotations

import argparse
import logging
import sys

from PyQt6.QtWidgets import QApplication

from .main_window import LaserMainWindow


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agilent-8164b-gui",
        description="PyQt6 control panel for the Agilent/Keysight 8164B tunable laser.",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="run against a simulated instrument (no VISA backend or hardware needed)",
    )
    parser.add_argument(
        "--resource",
        default=None,
        help="pre-fill the VISA resource string, e.g. GPIB0::20::INSTR",
    )
    parser.add_argument(
        "--slot", type=int, default=None, help="mainframe slot holding the laser module"
    )
    parser.add_argument(
        "--channel", type=int, default=None, help="channel number (dual-wavelength sources)"
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=200,
        metavar="MS",
        help="how often to sample wavelength and power, in ms (default: 200)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="log every SCPI command and response"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Agilent 8164B laser control")

    window = LaserMainWindow(
        simulate=args.simulate, poll_interval_ms=args.poll_interval
    )
    if args.resource:
        window.connection_panel.resource_combo.setCurrentText(args.resource)
    elif args.simulate:
        window.connection_panel.resource_combo.setCurrentText("SIM::INSTR")
    if args.slot is not None:
        window.connection_panel.slot_spin.setValue(args.slot)
    if args.channel is not None:
        window.connection_panel.channel_spin.setValue(args.channel)
    if args.debug:
        window.log_panel.debug_check.setChecked(True)

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
