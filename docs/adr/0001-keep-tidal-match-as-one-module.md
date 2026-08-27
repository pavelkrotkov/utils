# ADR-0001: Keep `tidal_pipeline.match` as one module

- Status: Accepted
- Date: 2026-08-27
- Context: architecture review of `utils` (epic #165), issue #169

## Context

`tidal_pipeline/match.py` is 33 KB with 36 top-level functions spanning five
responsibilities: query planning, candidate retrieval, scoring, truth-record
persistence, and coverage training. An architecture review proposed splitting
it into `query_plan`, `truth_store`, and a slimmer `match`, with
`train_coverage` becoming a caller of the three.

The review made the split conditional: a seam is only worth introducing if
something varies across it, and the module is deep rather than shallow, so the
usual deepening argument does not apply.

## Decision

Keep `match` as one module. Do not extract `query_plan` or `truth_store`.

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

**The stated testability problem is false.** The proposal claimed scoring could
not be tested without dragging persistence and training along. It can, and it
already is: `test_tidal_match_scoring.py` imports `extract_features`,
`base_score`, `apply_penalties` and `score_candidate` from `match` and
exercises them directly. Python imports do not drag unrelated functions into a
test; only a shared *object* would, and there is none.

**The seam that matters already exists.** Retrieval already varies, and already
has real adapters: `SearchBackend` and `CachedSearchBackend` in
`tidal_pipeline.client`, with a fake used by `test_tidal_search_backend.py`.
The part of this area that genuinely needed a seam has one.

**The deletion test says relocation, not concentration.** Deleting a
hypothetical `truth_store` or `query_plan` module would not make complexity
reappear across callers; the same functions would simply be called from
`match` again. That is moving code between files, not deepening a module.

## Consequences

- `match` stays large, and `TIDAL_ARCHITECTURE.md` continues to list many key
  functions for it. Module size alone is not the signal we act on; interface
  width relative to behaviour is, and callers use a small part of that surface.
- If a second storage medium ever appears — a database, a remote store, a
  different on-disk format — this decision should be revisited, and the store
  extraction is the right first move at that point.
- If `train_coverage` grows enough to need its own tests against fabricated
  weights, extracting *it* alone is a smaller and better-justified change than
  the three-way split.

## Alternatives considered

- **Split all three as proposed.** Rejected: introduces a seam nothing varies
  across, for a testability problem that does not exist.
- **Extract only `query_plan`.** Rejected: no adapters, no seam — purely a
  file-organisation change, and the query-planning functions are already
  reachable and testable by direct import.
