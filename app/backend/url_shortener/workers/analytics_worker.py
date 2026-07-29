import signal
import time

from url_shortener.api.dependencies import get_analytics_service


def main() -> None:
    analytics = get_analytics_service()
    analytics.start_worker()
    stop_requested = False

    def shutdown_handler(signum, frame) -> None:  # type: ignore[unused-argument]
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        while not stop_requested:
            time.sleep(1)
    finally:
        analytics.stop_worker()


if __name__ == "__main__":
    main()
