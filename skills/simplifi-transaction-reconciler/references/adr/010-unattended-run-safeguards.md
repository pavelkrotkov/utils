# ADR-010: Make unattended runs fail closed and never report a false clean

- Status: Accepted
- Scope: periodic read-only execution — configuration, visibility, and what a
  report has to say about itself

## Context

Everything the earlier decisions established — cursor advancement bound to the
server's own coverage claim (ADR-006), cursors scoped by identity, an explicit
run lifecycle, protected artifact storage (ADR-008), a declared egress policy
(ADR-009) — was built for a person sitting in front of the command. A schedule
changes one thing, and it is the thing that matters: when the run is wrong,
nobody is there to notice.

Every convenience that makes an interactive command forgiving becomes a way for
a periodic job to be quietly useless:

- The data directory defaults if unstated. Interactively that saves typing. For
  a job it means the location was never chosen by anyone, so two schedules that
  meant different databases can silently share one.
- `--allow-unsafe-paths` downgrades refusals to warnings. A warning nobody
  reads is not a control.
- Failures were recorded in the database but only visible by reading logs — and
  by the time anyone looks, logs have rotated.
- A report with no findings said "Nothing flagged." A clean week and a broken
  ingest produce exactly that, and they are not the same thing.

That last one is the real hazard. A crash is loud and gets fixed. **A false
clean is silent and gets trusted.** A report that says nothing needs attention
because the ingest failed, or because the cursor pointed at an empty window, or
because every row was discarded as ineligible, looks precisely like a healthy
week — for as many weeks as it takes for someone to check by hand.

## Decision

**`--unattended` refuses configurations a scheduled job should not have.** An
implicit data directory, relaxed path checks, and model egress are each fine
interactively and wrong on a timer. The check runs at startup, before any work,
because failing at startup is the one failure mode that is safe when nobody is
watching. All problems are reported together rather than one per run, so fixing
a schedule takes one iteration and not three.

Model egress is refused specifically because `--send` rests on someone having
reviewed the payload (ADR-009). That is not a step a timer can perform, so the
permission it grants does not transfer to one.

**`status` makes failure visible without reading logs.** It reports the latest
run *per schedule*, where a schedule is identified by source **and cursor
scope** — the same pair the cursor itself is keyed by. Grouping by source alone
would let a later success for one API profile bury a failure in another, which
is the same silence in a smaller box. Each line carries state, run ID, cursor
scope, cursor movement, row count, and any recorded error. The exit code
carries the same information for a monitor that parses nothing: `0` when every
schedule's latest run succeeded, `1` when any did not, `2` when there is
nothing to report. That last case is deliberately not success: a schedule that
has never run looks identical to a healthy one if you only check for errors.

**A schedule that stops firing is a failure with no failed run.** If cron is
removed or the host retired, the last run stays `succeeded` forever and every
state check passes while no ingest happens for weeks. `--max-age-hours` states
the expected cadence and makes an older latest run unhealthy. It has no default
because there is no cadence this runtime can infer, and inventing one would
fail every interactive database that simply has not been touched today — so
when it is absent, `status` says so rather than implying a coverage it does not
have.

**Reports identify what they are reports of.** Run ID, source, dataset, the
cursor window covered, and whether the run was a complete snapshot or
incremental. A periodic report that does not name its own inputs cannot be
compared with last week's, and cannot be told apart from one produced against a
different dataset.

The dataset field describes the *analyzed rows*, not the latest run. Those
differ: `transaction_version` is isolated by source alone, so a database
holding two cursor scopes produces a report containing both while only one run
supplied the scope. Naming that run's scope would be a confident lie, so a
multi-scope source is reported as a composite and the scopes are listed. Issue
#136 would make the state itself scoped and this honest hedge unnecessary.

**A zero result carries its own diagnosis.** The `Funnel` records how many rows
entered, how many were eligible for review, how many survived the date bound,
how many were actually *scored*, and how many findings resulted — measured
where rows are lost rather than recomputed from the output, because a count
derived from the survivors cannot describe what is missing.

The distinction between review-eligible and scored is load-bearing, and getting
it wrong is how the first version of this funnel produced the very failure it
was written to prevent. `assess_eligibility` marks a row eligible even when
settlement is unknown — that is correct, since the row is still visible for
review. But `prioritize.analyse`, merchant memory, staleness, and
recurring-charge detection all require `is_statistics_eligible`, which requires
a confirmed `CLEARED` state. A CSV export carries no settlement state at all,
so every row is review-eligible and *none* is ever scored. Counting review
eligibility as "analyzed" made the report announce that seven rows were
examined and nothing was found when no analyzer had looked at any of them.
`scored` therefore comes from the analyzers' own predicate.

`findings` counts every analyzer's output, not just prioritization's.
Recurring-charge findings appear in the same report, and counting one and not
the other would let a report list a price hike above a sentence saying nothing
was found.

With no findings, the report says which of five things happened: nothing was
read at all, everything was ineligible, everything fell outside the window,
rows were visible but none could be scored, or rows were genuinely examined and
nothing met a threshold. Only the last is a clean bill. When some rows were
scored and others were not, the clean statement is qualified with what it does
not cover.

`discarded` is defined against what was analyzed rather than against
eligibility alone, so a row lost to the date bound counts too, and rows failing
both filters are counted once. An operator asking "how many of my transactions
did this actually look at" is owed one number rather than a subtraction.

**Cursor advancement and idempotency were already correct and are now
pinned.** ADR-006 bound the cursor to a succeeded run; this adds tests that
name that property directly — a failed run does not advance it, and neither
does one left at `started` by a killed process. Re-ingesting the same input
adds no versions while still recording the attempt: idempotent in data, not
silent in provenance, because the attempt is evidence that the schedule fired.

**Unattended runs perform no mutations,** which holds because the runtime has
no mutating operation at all. That is asserted as an interface property — no
subcommand offers a write-shaped option — rather than left as a habit that a
future flag could break unnoticed.

## Consequences

A scheduled job now needs an explicit `--data-dir` and gains `--unattended`.
Monitoring is `status`'s exit code. The report grows two sections: an
identification block and, when there are no findings, an explanation.

The `Funnel` has to be threaded from `analyze` into the renderer, which makes
`report.render` depend on `unattended`. That is the right direction — the
report is the artifact that has to be honest about its own coverage.

## Alternatives considered

**Emit metrics and let a monitoring system decide.** Requires infrastructure
this skill does not have and cannot assume. An exit code and a readable report
work with `cron` and a mailbox.

**Fail the run when there are no findings.** Conflates "found nothing" with
"looked at nothing", which is exactly the distinction this decision exists to
draw. A clean week must be able to exit zero.

**Infer the reasons after the fact from the filtered rows.** What is needed is
the rows that are *absent*, and no amount of inspecting the survivors recovers
them. Hence counting at the filter rather than after it.
