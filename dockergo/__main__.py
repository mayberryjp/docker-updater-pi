"""Entry point: wire the display thread + orchestrator loop together."""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from .config import ConfigError, load_config
from .discord import DiscordNotifier
from .display import DisplayLoop
from .logging_setup import configure_logging
from .orchestrator import Orchestrator
from .status import State, Status
from .wifi import WifiManager


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dockergo",
        description="Sneakernet Docker updater for Raspberry Pi 4 + 3.5\" LCD",
    )
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--interval", type=int, help="seconds between scans")
    parser.add_argument("--mock-display", action="store_true",
                        help="render frames to dockergo-frame.png instead of the panel")
    parser.add_argument("--fb", help="framebuffer device (default /dev/fb1 or $DOCKERGO_FB)")
    parser.add_argument("--log-file", default="/var/log/dockergo/dockergo.log",
                        help="rotating diagnostic log (default /var/log/dockergo/dockergo.log)")
    args = parser.parse_args(argv)

    try:
        logger = configure_logging(args.log_file)
    except OSError as exc:
        print(f"log setup error: {exc}", file=sys.stderr)
        return 2

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        logger.error("Config error: %s", exc)
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    status = Status(cfg.device_name)
    display = DisplayLoop(status, device=args.fb, mock=args.mock_display)
    display.start()

    status.set(state=State.BOOT, headline="DockerGo", detail="starting")
    status.log(f"{cfg.device_name} booting")

    notifier = DiscordNotifier(cfg.discord_webhook, username=cfg.device_name)
    orchestrator = Orchestrator(cfg, status, notifier, WifiManager())

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    interval = args.interval or cfg.poll_interval
    try:
        if args.once:
            orchestrator.cycle()
        else:
            while not stop.is_set():
                try:
                    orchestrator.cycle()
                except Exception as exc:  # appliance: a failed cycle must not kill the loop
                    logger.exception("Cycle failed")
                    status.set(state=State.ERROR, headline="Error", detail=str(exc)[:80])
                    status.log(f"ERROR: {exc}")
                if status.snapshot().state != State.ERROR:
                    status.set(state=State.IDLE, headline="Idle",
                               detail=f"next scan in {interval}s", progress=None)
                else:
                    status.log(f"Retrying failed work after {interval}s")
                    status.set(progress=None, transfer=None)
                stop.wait(interval)
    finally:
        notifier.close()
        display.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
