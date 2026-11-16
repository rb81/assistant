import logging
from typing import Any, Callable, Optional

from .config import AppConfig
from .worker_health import poll_cycle


def poll_interval_seconds(
    config: AppConfig,
    path: str,
    fallback_path: Optional[str] = "agent.limits.poll_interval_seconds",
    default: int = 60,
) -> int:
    fallback = config.get_int(fallback_path, default) if fallback_path else default
    return max(1, config.get_int(path, fallback))


def run_poll_loop(
    stop_requested: Callable[..., bool],
    poll_once: Callable[[], Any],
    interval_seconds: int,
    *,
    should_sleep: Callable[[Any], bool],
    on_result: Optional[Callable[[Any], None]] = None,
    logger: Optional[logging.Logger] = None,
    error_message: str = "poll loop failed",
) -> None:
    while not stop_requested():
        try:
            with poll_cycle(interval_seconds):
                result = poll_once()
                if on_result:
                    on_result(result)
                sleep = should_sleep(result)
            if sleep:
                if stop_requested(interval_seconds):
                    return
        except Exception:
            if logger:
                logger.exception(error_message)
                if stop_requested(interval_seconds):
                    return
                continue
            raise
