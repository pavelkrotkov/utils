#!/usr/bin/env python3
"""Tests for shared audio progress helpers."""

from __future__ import annotations

import time
import unittest
from typing import Any

from audio_common import ProgressReporter, run_threaded_with_periodic_progress, run_with_progress


class RecordingReporter(ProgressReporter):
    def __init__(self) -> None:
        super().__init__(enabled=True)
        self.started: list[tuple[str, str | None]] = []
        self.finished: list[tuple[str, str | None]] = []
        self.percentages: list[float] = []
        self.updates: list[tuple[str, float | None, float | None, str | None]] = []

    def start(self, stage: str, detail: str | None = None) -> None:
        self.started.append((stage, detail))

    def update(
        self,
        stage: str,
        completed: float | None = None,
        total: float | None = None,
        *,
        detail: str | None = None,
        force: bool = False,
        show_count: bool = True,
    ) -> None:
        del force, show_count
        self.updates.append((stage, completed, total, detail))
        if completed is not None and total:
            self.percentages.append(100.0 * completed / total)

    def finish(self, stage: str, detail: str | None = None) -> None:
        self.finished.append((stage, detail))


class FakeProcess:
    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


class RunWithProgressTest(unittest.TestCase):
    def test_reports_percentages_from_stubbed_process_output(self) -> None:
        calls: list[tuple[list[str], dict[str, Any]]] = []

        def popen_factory(cmd: list[str], **kwargs: Any) -> FakeProcess:
            calls.append((cmd, kwargs))
            return FakeProcess(
                [
                    "noise before progress\n",
                    "progress: 12.5%\n",
                    "another line\n",
                    "progress: 87.5%\n",
                ]
            )

        def parse_progress(line: str) -> float | None:
            if not line.startswith("progress:"):
                return None
            return float(line.split(":", 1)[1].strip().rstrip("%"))

        reporter = RecordingReporter()
        tail = run_with_progress(
            ["fake-bin", "--work"],
            "fake stage",
            parse_progress,
            reporter=reporter,
            start_detail="input.wav",
            finish_detail="output.json",
            popen_factory=popen_factory,
        )

        self.assertEqual([["fake-bin", "--work"]], [cmd for cmd, _ in calls])
        self.assertEqual(["noise before progress", "another line"], tail)
        self.assertEqual([("fake stage", "input.wav")], reporter.started)
        self.assertEqual([12.5, 87.5], reporter.percentages)
        self.assertEqual([("fake stage", "output.json")], reporter.finished)

    def test_threaded_progress_reports_start_still_running_and_finish(self) -> None:
        reporter = RecordingReporter()

        def delayed_noop() -> None:
            time.sleep(0.55)

        run_threaded_with_periodic_progress(
            delayed_noop,
            reporter=reporter,
            label="threaded stage",
            interval=0.01,
        )

        self.assertEqual([("threaded stage", None)], reporter.started)
        self.assertIn(
            ("threaded stage", 7.5, 100.0, "still running"),
            reporter.updates,
        )
        self.assertEqual(100.0, reporter.percentages[-1])
        self.assertEqual([("threaded stage", None)], reporter.finished)


if __name__ == "__main__":
    unittest.main()
