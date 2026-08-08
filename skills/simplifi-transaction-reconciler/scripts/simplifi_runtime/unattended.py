"""Safeguards for runs that nobody is watching.

A scheduled run differs from an interactive one in a single way that matters:
when it is wrong, no one is there to notice. Every convenience that makes an
interactive command forgiving — inventing a data directory, warning on stderr
and carrying on, reporting "nothing to review" without saying why — becomes a
way for a periodic job to be quietly useless for weeks.

The worst outcome is not a crash. A crash is loud and gets fixed. The worst
outcome is a *false clean*: a report that says nothing needs attention because
the ingest failed, or the cursor pointed at an empty window, or every row was
discarded as ineligible. It looks exactly like a healthy week.

So this module does two things:

* `assert_unattended_safe` refuses a configuration that a scheduled job should
  not have — an implicit data directory, relaxed path checks, model egress.
  Failing at startup is the one failure mode that is safe when unattended.
* `Funnel` records how many rows entered, how many survived each stage, and
  why the rest did not. A zero result then arrives with its own diagnosis
  attached, so "nothing to review" and "nothing was looked at" stop looking
  alike.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Reason codes `semantics.assess_eligibility` can attach, mapped to what an
#: operator should take from seeing a lot of them. Unknown codes are reported
#: verbatim rather than dropped — a code we do not recognize is still evidence.
REASON_EXPLANATIONS = {
    "missing_required_field": (
        "the source did not supply an ID, date, amount, or account, so the row "
        "cannot be reasoned about at all"
    ),
    "excluded_from_reports": "the row is flagged excluded in the source itself",
    "report_exclusion_unknown": "the source did not say whether the row is excluded",
    "missing_optional_field": (
        "the source carries no settlement state, so settled-only analyses skip the row"
    ),
    "unsupported_state": "the row is not settled (pending, projected, or scheduled)",
    "eligible": "the row was eligible for review",
}


class UnattendedError(Exception):
    """A configuration that is not safe to run without someone watching."""


def assert_unattended_safe(
    *,
    data_dir_is_explicit: bool,
    allow_unsafe_paths: bool,
    sends_to_model: bool,
) -> None:
    """Refuse a scheduled configuration that could fail silently or disclose.

    Each of these is fine interactively and wrong unattended:

    * **An implicit data directory.** Interactively the default is a
      convenience. For a job, it means the location was never stated by
      anybody, so nobody can say where the ledger for a given schedule lives —
      and two jobs that meant different databases can quietly share one.
    * **Relaxed path checks.** `--allow-unsafe-paths` downgrades refusals to
      warnings, and a warning nobody reads is not a control.
    * **Model egress.** `classify --send` is a deliberate disclosure of
      financial data to a third party. A deliberate act cannot be delegated to
      a timer; there is no one present to review the payload, which is the
      whole basis on which sending is permitted at all.
    """
    problems = []
    if not data_dir_is_explicit:
        problems.append(
            "no data directory was stated (pass --data-dir or set SIMPLIFI_DATA_DIR); "
            "an unattended run must not invent where its ledger lives"
        )
    if allow_unsafe_paths:
        problems.append(
            "--allow-unsafe-paths turns location refusals into warnings, and "
            "nobody is reading warnings from a scheduled run"
        )
    if sends_to_model:
        problems.append(
            "--send discloses transactions to a model provider on the strength "
            "of someone having reviewed the payload; a timer cannot review it"
        )
    if problems:
        raise UnattendedError(
            "refusing to run unattended: " + "; ".join(problems),
        )


@dataclass(frozen=True)
class Funnel:
    """How many rows entered, how many survived, and why the rest did not.

    Carried into the report so a zero result can explain itself. The counts are
    deliberately taken at the points where rows are actually lost, not
    recomputed afterwards from a filtered list — a count derived from the
    output cannot describe what the output is missing.
    """

    #: Rows read from the store for this source and run.
    input_rows: int = 0
    #: Rows the eligibility rules admitted, across the whole input.
    eligible_rows: int = 0
    #: Rows the as-of date bound removed, being dated after the analysis date.
    out_of_window_rows: int = 0
    #: Rows that were both eligible and within the window — what was actually
    #: considered. Every other input row is discarded, and `reason_counts` says
    #: on what grounds.
    analyzed_rows: int = 0
    #: Rows flagged for a human.
    findings: int = 0
    #: Eligibility reason code -> count, over the input rows.
    reason_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def discarded_rows(self) -> int:
        """Input that never reached consideration, for whatever reason.

        Defined against `analyzed_rows` rather than against eligibility alone,
        so a row lost to the date bound counts as discarded too. An operator
        asking "how many of my transactions did this report actually look at"
        is owed one number, not a subtraction they have to perform.
        """
        return max(0, self.input_rows - self.analyzed_rows)

    @property
    def ineligible_rows(self) -> int:
        return max(0, self.input_rows - self.eligible_rows)

    def summary(self) -> str:
        """One line, suitable for a log a person skims once a week."""
        return (
            f"input={self.input_rows} eligible={self.eligible_rows} "
            f"analyzed={self.analyzed_rows} discarded={self.discarded_rows} "
            f"(ineligible={self.ineligible_rows} out_of_window={self.out_of_window_rows}) "
            f"findings={self.findings}"
        )

    def diagnosis(self) -> list[str]:
        """Why a zero result is zero, in the order the rows were lost.

        Empty when there are findings — the report speaks for itself then. The
        distinction this draws is the one the whole module exists for: a clean
        week and a broken pipeline both produce no findings, and only one of
        them should be reassuring.
        """
        if self.findings:
            return []
        if not self.input_rows:
            return [
                "No transactions were read at all. The last successful ingest "
                "found nothing for this source, or the cursor window covered a "
                "period with no activity — check `status` before treating this "
                "as a clean result."
            ]
        lines = []
        if not self.eligible_rows:
            lines.append(
                f"All {self.input_rows:,} row(s) were ruled ineligible for review, so "
                f"nothing could be flagged. This is a pipeline result, not a clean bill."
            )
        elif not self.analyzed_rows:
            lines.append(
                f"{self.eligible_rows:,} row(s) were eligible but none survived the "
                f"analysis date bound, so nothing was examined."
            )
        else:
            lines.append(
                f"{self.analyzed_rows:,} row(s) were examined and none met a review "
                f"threshold. Nothing needing attention was found."
            )
        lines.extend(self.reason_lines())
        return lines

    def reason_lines(self) -> list[str]:
        """The eligibility verdicts, largest first, each with what it means."""
        lines = []
        for code, count in sorted(self.reason_counts.items(), key=lambda item: (-item[1], item[0])):
            if not count:
                continue
            explanation = REASON_EXPLANATIONS.get(code)
            suffix = f" — {explanation}" if explanation else ""
            lines.append(f"{count:,} row(s): {code}{suffix}")
        return lines


def build_funnel(
    *,
    rows: Sequence[Mapping[str, Any]],
    analyzed: Sequence[Mapping[str, Any]],
    findings: int,
) -> Funnel:
    """Measure the pipeline from the rows themselves.

    `analyzed` is what the analysis actually looked at, so the difference
    between it and `rows` is real loss rather than an estimate.
    """
    reason_counts = Counter()
    eligible = 0
    for row in rows:
        codes = _reason_codes(row)
        reason_counts.update(codes)
        if "eligible" in codes:
            eligible += 1
    # Eligibility and the date bound are independent filters, and a row can
    # fail both. Counting the intersection rather than subtracting each in turn
    # is what keeps `discarded` from double-counting those rows.
    considered = sum(1 for row in analyzed if "eligible" in _reason_codes(row))
    return Funnel(
        input_rows=len(rows),
        eligible_rows=eligible,
        out_of_window_rows=max(0, len(rows) - len(analyzed)),
        analyzed_rows=considered,
        findings=findings,
        reason_counts=dict(reason_counts),
    )


def _reason_codes(row: Mapping[str, Any]) -> tuple[str, ...]:
    raw = row.get("eligibility_reason_codes") or ""
    return tuple(code.strip() for code in str(raw).split(",") if code.strip())


@dataclass(frozen=True)
class RunIdentity:
    """What a report is a report *of*.

    A periodic report that does not name its own inputs cannot be compared with
    last week's, and cannot be told apart from one produced against a different
    dataset or an older cursor window. These are the fields that answer "which
    run is this, over what".
    """

    run_id: int
    source: str
    cursor_scope: str | None = None
    cursor_before: str | None = None
    cursor_after: str | None = None
    complete_snapshot: bool = False

    @property
    def dataset(self) -> str:
        """The scope fingerprint, or an honest admission that there is none."""
        return self.cursor_scope or "unscoped"

    def items(self) -> list[tuple[str, str]]:
        """Label/value pairs, in the order a reader wants them."""
        return [
            ("Run", str(self.run_id)),
            ("Source", self.source),
            ("Dataset", self.dataset),
            ("Cursor from", self.cursor_before or "beginning"),
            ("Cursor to", self.cursor_after or "not advanced"),
            ("Snapshot", "complete" if self.complete_snapshot else "incremental"),
        ]


def format_status(runs: Iterable[Mapping[str, Any]]) -> list[str]:
    """Render run rows for `status`, newest per source first."""
    lines = []
    for run in runs:
        state = str(run.get("state") or "unknown")
        marker = "OK " if state == "succeeded" else "!! "
        line = (
            f"{marker}{run.get('source', '?')}: run {run.get('id', '?')} {state}"
            f" started={run.get('started_at', '?')}"
            f" finished={run.get('finished_at') or 'never'}"
            f" rows={run.get('row_count') if run.get('row_count') is not None else '?'}"
            f" scope={run.get('cursor_scope') or 'unscoped'}"
            f" cursor={run.get('cursor_before') or 'beginning'}"
            f"→{run.get('cursor_after') or 'unchanged'}"
        )
        lines.append(line)
        if run.get("error_class") or run.get("error_message"):
            lines.append(
                f"   {run.get('error_class') or 'error'}: {run.get('error_message') or ''}".rstrip()
            )
    return lines
