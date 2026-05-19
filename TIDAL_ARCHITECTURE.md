# TIDAL Pipeline Architecture

This document describes the current implementation of the TIDAL matching
pipeline in this repository.

## Module Map

- `tidal_pipeline.parse`
  Source-file parsing and canonical album-record extraction.
  Key functions:
  - `parse_file_to_entries()`
  - `extract_candidates()`
  - `candidate_to_entry()`
  - `parse_heading_metadata()`
  - `parse_performer_metadata()`

- `tidal_pipeline.match`
  Structured-input loading, query generation, candidate retrieval, scoring,
  match-scoring weights, review/truth record construction, and coverage
  training.
  Key functions:
  - `load_album_inputs()`
  - `build_query_candidates()`
  - `select_query_candidates()`
  - `search_candidates_for_album()`
  - `sort_candidates()`
  - `score_candidate()`
  - `score_manual_candidate()`
  - `apply_details()`
  - `ensure_details()`
  - `choose_auto_candidate()`
  - `build_record()`
  - `load_truth_records()`
  - `save_truth_records()`
  - `train_coverage()`

- `tidal_pipeline.albums`
  Album and match-candidate dataclasses shared by parsing, matching, and
  evaluation.
  Key objects:
  - `AlbumInput`
  - `Candidate`
  - `QueryCandidate`

- `tidal_pipeline.truth`
  Persisted truth/review record dataclasses and JSON round-trip behavior.
  Key objects:
  - `Choice`
  - `TruthRecord`

- `tidal_pipeline.client`
  TIDAL OAuth, token caching, and API access.
  Key functions and classes:
  - `SearchBackend`
  - `TidalClient`
  - `CachedSearchBackend`
  - `get_valid_token()`
  - `resolve_country_code()`

- `tidal_pipeline.links`
  Truth-to-markdown link insertion.
  Key functions:
  - `load_updates()`
  - `apply_updates()`

- `tidal_pipeline.normalize`
  Normalization lexicons, tokenization, and low-level text helpers.

## Script Wrappers

- `tidal_parse_to_json.py`
  CLI wrapper around `tidal_pipeline.parse.parse_file_to_entries()`.

- `tidal_match_from_json.py`
  CLI wrapper around `tidal_pipeline.match`, plus interactive prompt and
  user-facing display logic.

- `tidal_apply_links_to_markdown.py`
  CLI wrapper around `tidal_pipeline.links`.

- `tidal_eval.py`
  Offline evaluation harness. Replays cached candidates through the shared
  `search_candidates_for_album()` driver via `CachedSearchBackend`, replays
  auto-selection, and reports precision@1, MRR, auto-select coverage/accuracy/
  recall, score distribution, and regressions.

## Formal Pipeline

Let $x_i$ be the source review block for album $i$.

The parser is now canonical and enriched, so there is a single parse stage:

$$
x_i \xrightarrow{P} a_i.
$$

Here $a_i$ is the match-ready album record emitted by
`tidal_parse_to_json.py` through `tidal_pipeline.parse`.

Conceptually,

$$
a_i =
(
\text{title}_i,
\text{composers}_i,
\text{performers}_i,
\text{ensembles}_i,
\text{conductor}_i,
\text{label}_i,
\text{year}_i,
\text{works}_i,
\text{instruments}_i,
\text{source}_i
).
$$

The parser owns:

- source block extraction
- heading parsing
- performer / ensemble / conductor parsing
- label parsing
- review-slug extraction
- italicized work-hint extraction
- canonical work-list construction

The matcher does not reopen the original source file to repair these values.

From $a_i$, the matcher generates a family of candidate queries:

$$
Q_i^{\mathrm{all}} = G(a_i) = \{q_{ik}\}_{k=1}^{K_i}.
$$

These are built from templates such as `title`, `work`, `composer_title`,
`performer_work`, `ensemble_title`, and `label_title`.

The matcher then selects a capped, diversified subset:

$$
\widetilde{Q}_i = S\!\left(Q_i^{\mathrm{all}}\right),
\qquad
\left|\widetilde{Q}_i\right| \le K_{\max}.
$$

For each selected query $q_{ik} \in \widetilde{Q}_i$, the TIDAL API returns a
small hit list:

$$
H_{ik} = \{r_{ikl}\}_{l=1}^{L_{ik}}.
$$

In code, retrieval is represented by the `SearchBackend` protocol. The live CLI
uses `TidalClient`, which satisfies the protocol with real TIDAL API calls.
Offline evaluation uses `CachedSearchBackend`, which satisfies the same
protocol from truth-record candidate caches.

The raw hit set is

$$
H_i = \bigcup_k H_{ik}.
$$

Those raw hits are deduplicated by TIDAL album id:

$$
C_i = D(H_i).
$$

Scoring is applied to each unique candidate $c \in C_i$, not to each raw hit
independently.

For each candidate $c \in C_i$, the matcher computes feature similarities

$$
\phi_f(a_i, c) \in [0, 1]
$$

for

$$
f \in \{
\text{title},
\text{composer},
\text{performer},
\text{ensemble},
\text{conductor},
\text{instrument},
\text{label},
\text{year}
\}.
$$

The base score is

$$
s_i^{\mathrm{base}}(c) = \sum_f w_f \, \phi_f(a_i, c).
$$

The final score adds multiplicative penalties for mismatched numeric tokens,
generic-title cases with weak composer support, and weak artist support:

$$
s_i(c) = p_i(c) \cdot s_i^{\mathrm{base}}(c),
\qquad 0 < p_i(c) \le 1.
$$

Candidates are ranked primarily by $s_i(c)$, then by query support and a stable
title tiebreak:

$$
\hat{c}_i = \arg\max_{c \in C_i} \operatorname{rank}_i(c).
$$

The decision layer is

$$
y_i =
\begin{cases}
\hat{c}_i.\mathrm{id}, & \text{if auto-selection accepts the top candidate} \\
\text{manual review}, & \text{otherwise.}
\end{cases}
$$

Auto-selection accepts the top candidate when either:

- $s_i(\hat{c}_i)$ exceeds the main threshold, or
- a recent-release rule is satisfied with sufficient score and supporting
  title or artist evidence

Otherwise the record is marked for manual review.

The persisted truth set is

$$
T = \{(a_i, \widetilde{Q}_i, C_i, y_i)\}_{i=1}^{N}.
$$

In memory, each persisted item is represented by `TruthRecord`, with its label
represented by `Choice`. `TruthRecord.from_dict()` and `TruthRecord.to_dict()`
mirror the on-disk JSON schema, including the existing field order, so
round-tripping a truth file does not rewrite the persisted format.

If desired, `--train-coverage` uses a prefix of $T$ to update both the feature
weights $w_f$ and the query-template weights used by $S$.

## Markdown Link Application

If $y_i$ is a selected TIDAL album id, `tidal_pipeline.links` maps that choice
back to the source line and inserts

$$
\texttt{https://tidal.com/browse/album/<id>}
$$

at the end of the corresponding markdown subsection.

## Evaluation

The evaluation harness (`tidal_eval.py`) operates on the persisted truth set $T$
without making any API calls.

For each $(a_i, C_i, y_i) \in T$, it builds a `CachedSearchBackend` from the
record's cached candidates, calls `search_candidates_for_album()` with the
record's persisted selected queries, and replays auto-selection to obtain a
predicted choice $\hat{y}_i$. This keeps offline evaluation on the same ranking
key used by live matching: `(score, len(queries), title.lower())`, descending.

Reported metrics:

- **Precision@1** — fraction of albums where the top-ranked candidate matches
  the ground-truth label $y_i$.
- **MRR** — mean reciprocal rank of the ground-truth candidate across all
  albums.
- **Auto-select accuracy** — fraction of auto-selected albums where
  $\hat{y}_i = y_i$.
- **Auto-select coverage** — fraction of albums that pass the auto-select
  threshold.
- **Auto-select recall** — fraction of correct albums that are also
  auto-selected.
- **Score distribution** and per-album regressions.

Quality gates: passing `--min-precision`, `--min-mrr`, or similar flags causes
the harness to exit non-zero when metrics breach the specified thresholds,
suitable for CI or pre-commit checks.

## Implementation Invariants

- Parsing is single-pass and canonical.
- Matching consumes structured JSON; it does not reparse the original source
  block for enrichment.
- Review records and truth persistence live in the shared library.
- CLI scripts remain stable entrypoints.
- Evaluation is offline and deterministic — it uses `CachedSearchBackend` to
  score cached candidates without API calls through the same driver as live
  matching.
