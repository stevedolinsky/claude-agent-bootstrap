"""Entry point: python -m receiver"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from pathlib import Path

from .dispatcher import Dispatcher, EventLogger
from . import metrics as prom
from .queue import WorkQueue
from .server import Config, create_server

log = logging.getLogger("receiver")

_shutdown_event = threading.Event()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude Agent Fleet — Sequential Work Queue Receiver",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("~/.claude/agent-receiver.toml"),
        help="Path to TOML config file (default: ~/.claude/agent-receiver.toml)",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        default=None,
        help="Override port (default: from config or 9876)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # Load config
    config_path = args.config.expanduser()
    if config_path.exists():
        log.info("Loading config from %s", config_path)
        config = Config.from_file(config_path)
    else:
        log.info("No config file found, using defaults")
        config = Config()

    # Override port if specified
    if args.port is not None:
        config = Config(**{
            **{f.name: getattr(config, f.name) for f in config.__dataclass_fields__.values()},
            "port": args.port,
        })

    # Setup
    config.ensure_dirs()
    config.validate_permissions()

    # Initialize Prometheus metrics (restore persisted counters)
    prom.load_state()
    prom.setup_persistence()

    # Initialize components
    events = EventLogger(config.events_file)
    queue = WorkQueue(config.queue_dir)
    dispatcher = Dispatcher(queue, events, config)

    # Create server
    server = create_server(config, queue, dispatcher, events)

    # Signal handler: only set event (async-signal-safe — no locks, no I/O)
    def shutdown_handler(signum: int, frame: object) -> None:
        _shutdown_event.set()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Start heartbeat
    dispatcher.start_heartbeat()

    # Run server in daemon thread (required by socketserver threading contract)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    log.info(
        "Receiver started on %s:%d (daily budget: $%.2f)",
        config.bind_address,
        config.port,
        config.daily_budget_usd,
    )

    # Main thread blocks until signal
    _shutdown_event.wait()

    # All cleanup in normal code flow — locks are safe here
    log.info("Shutting down...")
    server.shutdown()
    server.server_close()
    dispatcher.stop(timeout=30.0)
    prom.save_state()
    events.close()
    log.info("Receiver stopped")


if __name__ == "__main__":
    main()
