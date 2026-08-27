# ADR-0001: Do not extract a truth store from `tidal_pipeline.match`

- Status: Accepted
- Date: 2026-08-27
- Context: architecture review of `utils` (epic #165), issue #169

## Context

`tidal_pipeline/match.py` is 33 KB with 36 top-level functions spanning five
responsibilities: query planning, candidate retrieval, scoring, truth-record
persistence, and coverage training. Issue #169 proposed splitting it into
`query_plan`, `truth_store`, and a slimmer `match`, with `train_coverage`
becoming a caller of the three.

The issue made the split conditional on the store seam having a real second
adapter, since the module is deep rather than shallow and the usual deepening
argument does not apply.

## Decision

Do not extract `truth_store`. Do not extract `query_plan` on the grounds
argued in #169.

This ADR does **not** settle whether scoring should be separated from
retrieval; see "Deliberately left open".

## Rationale

**The store seam has one adapter, and no second one is in sight.** The six
persistence functions (`load_weights`, `load_training_model`,
`save_training_model`, `load_truth_records`, `save_truth_records`,
`load_existing_output`) are thin `Path` → JSON wrappers over a single storage
medium: JSON files on disk. Nothing varies across the proposed seam. One
adapter is a hypothetical seam, not a real one.

**An in-memory store would not pay for itself in tests.** The obvious candidate
for a second adapter is a test fake, but the existing tests do not need one:
`test_tidal_truth_records.py` round-trips through `TruthRecord.from_dict` and
`to_dict` directly, which is the behaviour worth pinning. A store interface
would sit between the tests and the thing they are actually testing.

**Scoring is already tested directly.** #169 claimed scoring could not be
tested without dragging persistence and training along. `test_tidal_match_
scoring.py` imports `extract_features`, `base_score`, `apply_penalties` and
`score_candidate` from `match` and exercises them with no persistence or
training involved. The claim as stated is false.

**The deletion test says relocation, not concentration.** Deleting a
hypothetical `truth_store` or `query_plan` module would not make complexity
reappear across callers; the same functions would simply be called from `match`
again. That is moving code between files, not deepening a module.

## Deliberately left open

Two facts surfaced in review that #169 did not raise, and that this ADR does
not resolve.

**The caller surface is wide.** `tidal_match_from_json.py` imports 20 symbols
from `match`, spanning query planning, retrieval, scoring, persistence, record
construction, and training. That is most of the module's public
responsibilities, not a small corner of them. Module size is not our signal,
but interface width is, and by that measure this interface is wide.

**Module-level import coupling is real.** Importing any name from `match`
executes the module and its transitive imports, so importing `base_score` also
imports `tidal_pipeline.client` and requires `requests` — which is why
`test_tidal_match_scoring.py` declares a `requests` dependency to test pure
arithmetic. Scoring is testable, but it is not import-isolated. Separating
scoring from retrieval would remove that coupling.

Neither fact supports the store extraction this ADR rejects. Both are a
different and better argument for a *scoring/retrieval* split than #169 made,
and that question deserves to be decided on its own evidence rather than
foreclosed here.

## Consequences

- The persistence functions stay in `match`. Revisit if a second storage medium
  appears — a database, a remote store, a different on-disk format — at which
  point the store extraction becomes the right first move.
- A future review may reopen the scoring/retrieval boundary. It should, if the
  import coupling above starts costing something concrete.
- `TIDAL_ARCHITECTURE.md` continues to list many key functions for `match`.

## Alternatives considered

- **Split all three as proposed in #169.** Rejected: the store half introduces
  a seam nothing varies across, and the stated testability problem is not the
  real one.
- **Extract only `query_plan`.** Rejected on #169's reasoning: no adapters, no
  seam, and the query-planning functions are already reachable by direct
  import. This says nothing about the scoring/retrieval boundary.
