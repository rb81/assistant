import sys
import types
import unittest

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda handle: {}))

from assistant_agent.config import AppConfig
from assistant_agent.polling import poll_interval_seconds, run_poll_loop


class StopAfterSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, sleep_seconds: float = 0) -> bool:
        self.calls.append(sleep_seconds)
        return sleep_seconds > 0


class PollingTest(unittest.TestCase):
    def test_poll_interval_uses_specific_path_before_global_fallback(self) -> None:
        config = AppConfig(
            {
                "agent": {
                    "email": {"imap_poll_interval_seconds": 60},
                    "limits": {"poll_interval_seconds": 2},
                }
            }
        )

        self.assertEqual(poll_interval_seconds(config, "agent.email.imap_poll_interval_seconds"), 60)

    def test_poll_interval_can_skip_global_fallback(self) -> None:
        config = AppConfig({"agent": {"limits": {"poll_interval_seconds": 2}}})
        interval = poll_interval_seconds(
            config,
            "agent.email.imap_poll_interval_seconds",
            fallback_path=None,
            default=60,
        )

        self.assertEqual(interval, 60)

    def test_poll_interval_clamps_to_at_least_one_second(self) -> None:
        config = AppConfig({"agent": {"task_agent": {"poll_interval_seconds": 0}, "limits": {"poll_interval_seconds": 0}}})

        self.assertEqual(poll_interval_seconds(config, "agent.task_agent.poll_interval_seconds"), 1)

    def test_loop_sleeps_after_each_poll_when_requested(self) -> None:
        stopper = StopAfterSleep()
        calls = 0

        def poll_once() -> int:
            nonlocal calls
            calls += 1
            return 3

        run_poll_loop(stopper, poll_once, 60, should_sleep=lambda _result: True)

        self.assertEqual(calls, 1)
        self.assertEqual(stopper.calls, [0, 60])

    def test_loop_drains_work_before_sleeping(self) -> None:
        stopper = StopAfterSleep()
        results = [1, 0]

        def poll_once() -> int:
            return results.pop(0)

        run_poll_loop(stopper, poll_once, 2, should_sleep=lambda result: not result)

        self.assertEqual(results, [])
        self.assertEqual(stopper.calls, [0, 0, 2])

    def test_loop_sleeps_after_logged_error(self) -> None:
        stopper = StopAfterSleep()

        class Logger:
            def __init__(self) -> None:
                self.messages: list[str] = []

            def exception(self, message: str) -> None:
                self.messages.append(message)

        logger = Logger()

        def poll_once() -> int:
            raise RuntimeError("failed")

        run_poll_loop(stopper, poll_once, 5, should_sleep=lambda result: not result, logger=logger, error_message="custom failure")

        self.assertEqual(stopper.calls, [0, 5])
        self.assertEqual(logger.messages, ["custom failure"])


if __name__ == "__main__":
    unittest.main()
