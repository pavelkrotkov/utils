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
run *per source* — not the latest overall, which would hide a dead API schedule
behind a healthy CSV one — with state, run ID, cursor scope, cursor movement,
row count, and any recorded error. The exit code carries the same information
for a monitor that parses nothing: `0` when every source's latest run
succeeded, `1` when any did not, `2` when there is nothing to report. That last
case is deliberately not success: a schedule that has never run looks identical
to a healthy one if you only check for errors.

**Reports identify what they are reports of.** Run ID, source, dataset (the
cursor scope fingerprint), the cursor window covered, and whether the run was a
complete snapshot or incremental. A periodic report that does not name its own
inputs cannot be compared with last week's, and cannot be told apart from one
produced against a different dataset.

**A zero result carries its own diagnosis.** The `Funnel` records how many rows
entered, how many were eligible, how many survived the date bound, how many
were analyzed, and how many findings resulted — measured at the points where
rows are actually lost, not recomputed from the output, because a count derived
from the output cannot describe what the output is missing. When there are no
findings, the report says which of four things happened: nothing was read at
all, everything was ineligible, everything fell outside the window, or the rows
were genuinely examined and nothing met a threshold. Only the last is a clean
bill, and it is the only one phrased as one.

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
