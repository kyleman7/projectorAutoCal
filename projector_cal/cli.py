"""CLI entry point for projector_cal.

Usage:
    python -m projector_cal [--config path] [--port 8080] [--host 0.0.0.0] [--verbose]

Starts the FastAPI web server. Open the printed URL in any browser on the LAN.
The setup wizard, run dashboard, before/after comparison, and profiles are all in the UI.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys


def _get_local_ip() -> str:
    """Return the machine's outbound LAN IP address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="projector_cal",
        description="Epson 5040UB closed-loop projector calibration system",
    )
    parser.add_argument(
        "--config", metavar="PATH",
        help="Path to JSON config file (merged with built-in defaults)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Interface to bind (default: 0.0.0.0 = all interfaces)",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="HTTP port (default: 8080)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG-level logging",
    )
    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load config (validates it before starting the server)
    try:
        from .config import load_config
        config = load_config(args.config)
        logging.getLogger(__name__).info(
            "Config loaded: projector=%s:%d  pgen=%s:%d",
            config.projector.host, config.projector.port,
            config.pgen.host, config.pgen.port,
        )
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    # Update server state with loaded config
    from .server import _state
    _state.config = config
    if args.config:
        _state.config_path = args.config

    # Print access URLs
    local_ip = _get_local_ip()
    print()
    print("  ProjectorCal is starting...")
    print()
    if args.host == "0.0.0.0":
        print(f"  Open http://{local_ip}:{args.port} in your browser")
        print(f"  Also accessible at http://localhost:{args.port}")
    else:
        print(f"  Open http://{args.host}:{args.port} in your browser")
    print()
    print("  Setup wizard -> Run probe -> Warm-up -> Calibrate -> Profiles")
    print()

    # Start uvicorn
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run: pip install uvicorn", file=sys.stderr)
        sys.exit(1)

    from .server import app
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="debug" if args.verbose else "warning",
        access_log=args.verbose,
    )


if __name__ == "__main__":
    main()
