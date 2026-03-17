"""Entry point: python -m receiver"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from .dispatcher import Dispatcher, EventLogger
from .queue import WorkQueue
from .server import Config, create_server

log = logging.getLogger("receiver")


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

    # Initialize components
    events = EventLogger(config.events_file)
    queue = WorkQueue(config.queue_dir)
    dispatcher = Dispatcher(queue, events, config)

    # Create server
    server = create_server(config, queue, dispatcher, events)

    # Graceful shutdown
    def shutdown_handler(signum: int, frame: object) -> None:
        log.info("Received signal %d, shutting down...", signum)
        server.shutdown()
        dispatcher.stop(timeout=30.0)
        events.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Start heartbeat
    dispatcher.start_heartbeat()

    # Run server
    log.info(
        "Receiver started on %s:%d (daily budget: $%.2f)",
        config.bind_address,
        config.port,
        config.daily_budget_usd,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        dispatcher.stop()
        events.close()
        log.info("Receiver stopped")


if __name__ == "__main__":
    main()
