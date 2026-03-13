# TIDAL Pipeline Refactoring Plan

## Goal

Refactor the current TIDAL matching workflow so that all pipeline logic lives in a
shared library, while the existing scripts remain as thin CLI wrappers.

The main architectural change is:

- the parser becomes the single canonical place that turns source text into a fully
  enriched album record
- the matcher stops reparsing source blocks and becomes a pure
  `album record -> queries -> hits -> scores -> decision` pipeline
- link application logic also moves into the same library

This removes the current split where parsing logic exists partly in
`tidal_parse_to_json.py` and partly in `tidal_match_from_json.py`.

## Current Problems

1. Parsing is split across scripts.
   `tidal_parse_to_json.py` emits a partial album record, but
   `tidal_match_from_json.py` reparses the original source block in
   `enrich_album_from_source()`.

2. The JSON contract is not canonical.
   The matcher does not trust the parser output and therefore mutates the meaning of
   the parsed record before matching.

3. Logic is duplicated or tightly coupled.
   Heading parsing, performer parsing, source-block extraction, and normalization are
   mixed into the matcher even though they are parser responsibilities.

4. Iteration is harder than it should be.
   Improvements to parsing, query generation, and scoring cannot be reasoned about as
   a clean pipeline because the parser/matcher boundary is porous.

5. Script boundaries currently carry too much logic.
   The scripts are functioning as both CLI entrypoints and implementation modules.

## Target Architecture

Create a shared package, for example:

```text
tidal_pipeline/
  __init__.py
  models.py
  normalize.py
  parse.py
  match.py
  client.py
  links.py
```

Six modules is enough for ~3800 lines of extractable logic. More modules can be
split out later if any of these grow unwieldy, but starting with fewer modules
reduces navigation cost and avoids premature boundaries.

The responsibilities should look like this:

- `models.py`
  Canonical dataclasses, typed record structures, and shared configuration
  constants (`DEFAULT_WEIGHTS`, `DEFAULT_TEMPLATE_WEIGHTS`, `STOPWORDS`,
  `ENSEMBLE_HINTS`, `INSTRUMENT_MAP`, `INSTRUMENT_ABBREVS`, etc.).
  Consolidating lookup tables here resolves the current duplication where
  e.g. `ENSEMBLE_KEYWORDS` and `ENSEMBLE_HINTS` are defined separately in the
  parser and matcher with slightly different contents.

- `normalize.py`
  Shared text normalization, tokenization, instrument normalization, phrase overlap,
  pruning, and reusable low-level helpers.

- `parse.py`
  Source-file reading, markdown/MHTML block extraction, source-line resolution,
  provenance helpers, and the canonical parsing pipeline from source block to
  enriched album record.
  This module should own:
  - source block extraction
  - heading parsing
  - performer parsing
  - label parsing
  - review-slug extraction
  - italicized work-hint extraction
  - album-record enrichment

  Source extraction and parsing are combined because source extraction exists
  only to feed the parser — they share data structures and invariants.

- `match.py`
  Query generation, query subset selection, feature extraction, candidate scoring,
  penalties, ranking, and auto-selection.

  Query generation and scoring are combined because they are tightly coupled:
  `score_hit()` uses the same normalization and token logic as
  `build_query_candidates()`, and they share the same weights configuration.

- `client.py`
  TIDAL auth, token management, API access, album search, album detail lookup,
  playlist and collection operations.

- `links.py`
  Truth-to-markdown link application.

Review/truth logic (record construction, truth persistence, training helpers)
can either live in `match.py` or remain in the matcher script, since it is
mostly tied to the interactive CLI flow.

The scripts then become wrappers:

- `tidal_parse_to_json.py`
  Calls library parse functions and writes JSON.

- `tidal_match_from_json.py`
  Calls library matcher/review functions and handles interactive CLI behavior.

- `tidal_apply_links_to_markdown.py`
  Calls library link-application functions.

## Recommended Extraction Architecture

The current JSON-first workflow is worth keeping. The problem is not the existence
of an intermediate JSON artifact; the problem is that the current parser output is
not canonical, so the matcher reparses source blocks to repair it.

The recommended architecture is:

1. extract one source block per album review item
2. convert that block into one canonical enriched album record
3. serialize that canonical record to JSON when needed
4. run matching only against the canonical record

In other words, JSON should be a durable pipeline artifact, not a partially parsed
object that later stages reinterpret.

### Preferred default: deterministic parsing

The default implementation should remain deterministic and local for:

- source block extraction
- markdown parsing
- MHTML / HTML extraction
- normalization
- query generation
- candidate scoring
- truth and link application

This keeps the core system inspectable, reproducible, and cheap to iterate on.

Recommended building blocks:

- `BeautifulSoup` + `lxml` for MHTML / HTML extraction (already used)

### Dependency upgrades (deferred)

The following dependency introductions are worth doing but should happen as
follow-up passes **after** the structural refactor is complete. Each one changes
matching behavior and must be validated independently against the truth set.

Bundling them into the structural refactor creates two risks:

1. Behavior changes from new dependencies get tangled with behavior changes from
   code moves, making regressions harder to attribute.
2. The refactor scope grows from "move code" to "move code and rewrite
   similarity functions," which increases the chance it stalls.

The structural refactor should use plain dataclasses and the existing helpers.
Once the library structure is stable, introduce these one at a time:

#### `markdown-it-py` (follow-up pass 1)

Replace regex-based markdown block extraction with a proper token-driven parser.

The current regexes work. Switching parsers risks subtle differences in block
boundaries, which would change which albums get extracted. Validate by diffing
parser output on `best2025.md` before and after.

Key APIs: `MarkdownIt("commonmark").parse(text)`, `Token.map`, `Token.children`.

Current functions to replace or simplify: `is_separator_line()`,
`is_review_line()`, `is_markdown_image_heading()`, `clean_markdown_heading()`,
most of `candidate_from_markdown_block()` and `parse_markdown_blocks()`.

#### `pydantic` (follow-up pass 2)

Replace ad hoc JSON loading and dict assembly with validated models.

Adding pydantic validation can cause behavior changes — stricter parsing,
field coercion differences, rejection of currently-accepted input. Validate by
round-tripping existing truth files and parser output through the new models.

Key APIs: `BaseModel`, `ConfigDict(extra="forbid")`, `Field(default_factory=list)`,
`field_validator`, `model_validator`, `model_dump(mode="json", exclude_none=True)`.

Current functions to replace or reduce: `parse_list()`, ad hoc JSON loading in
`load_album_inputs()` and `load_truth_records()`, hand-managed dict assembly in
`candidate_to_entry()`, `album_to_dict()`, `candidate_to_dict()`,
`query_candidate_to_dict()`, and `build_record()`.

#### `RapidFuzz` (follow-up pass 3)

Replace `overlap_score()` and `phrase_overlap_score()` with proper fuzzy
similarity functions.

`token_set_ratio` and the current `overlap_score` will produce different numbers
for the same inputs. This directly affects matching quality and must be validated
against the full truth set, not just spot-checked.

Key APIs: `rapidfuzz.fuzz.token_set_ratio`, `token_ratio`, `partial_ratio`,
`WRatio`, `process.extractOne`.

Keep `score_hit()` as the orchestration layer; use RapidFuzz only for the
similarity primitives underneath.

### Optional branch: schema-constrained LLM extraction

An LLM can be a good fit for the narrow step

`source block -> canonical AlbumRecord`

especially for difficult mixed-format cases where handwritten rules become brittle.

If adopted, the LLM should be used only for structured extraction, not for the full
matching pipeline.

Recommended constraints:

1. deterministic source block extraction still happens first
2. exactly one album block is sent per request
3. the response must conform to the canonical `AlbumRecord` schema
4. the output is validated locally before it is accepted
5. downstream query generation, retrieval, scoring, and review remain deterministic

This means the parser module should support two interchangeable implementations:

- deterministic extractor
- LLM-backed extractor

both returning the same canonical record type.

### What should not be delegated to an LLM

Even if LLM extraction is added, these stages should remain in the shared library:

- TIDAL query generation
- TIDAL retrieval
- candidate deduplication
- feature scoring and penalties
- auto-selection thresholds
- truth recording
- markdown link application

Those stages benefit from explicit, inspectable logic and from easy regression
comparison against the truth set.

## Canonical Data Contract

The canonical parser output should already be enriched and match-ready.

That means the parser output JSON should contain the final normalized album record:

- `title`
- `composers`
- `performers`
- `ensembles`
- `conductor`
- `label`
- `year`
- `works`
- `instruments`
- source provenance fields

The matcher should not reopen the source block to improve those values.

The only reason for the matcher to touch source provenance should be:

- displaying source context during manual review
- building output payloads
- writing links back to markdown

## Refactoring Strategy

### Phase 0: Extract TidalClient and OAuth

Extract the `TidalClient` class (~600 lines) and the OAuth flow (`OAuthHandler`,
`save_tokens`, `load_tokens`, `get_valid_token`, etc.) into `client.py`.

This is the most self-contained extraction: the client has no dependency on
parsing, scoring, or normalization logic. It immediately shrinks the 3053-line
matcher file by ~20% and validates that the package structure works.

Objective:

- no behavior change
- matcher imports `TidalClient` from the library
- client logic is testable in isolation

### Phase 1: Establish the library skeleton

Create the shared package and move only pure helpers first.

Move these kinds of functions out of scripts and into the library:

- normalization/token helpers
- instrument normalization
- overlap helpers
- separator detection helpers

Also consolidate configuration constants that are currently scattered and
duplicated across scripts:

- `ENSEMBLE_KEYWORDS` (parser) and `ENSEMBLE_HINTS` (matcher) → single set
- `INSTRUMENT_ABBREVS` (parser) and `INSTRUMENT_MAP` (matcher) → single mapping
- `DEFAULT_WEIGHTS`, `DEFAULT_TEMPLATE_WEIGHTS`, `STOPWORDS` → `models.py`
- `SEPARATOR_RE` → single definition

Objective:

- no behavior change
- scripts still work
- imports now come from the shared package
- no more duplicated constants across files

### Phase 2: Move source parsing into the library

Move raw source parsing from `tidal_parse_to_json.py` into library code.

Target library functions:

- `parse_source_file(path) -> list[SourceBlock]`
- `parse_block_to_album(block) -> AlbumRecord`
- `parse_file_to_albums(path) -> list[AlbumRecord]`

At this stage, keep behavior compatible with the current JSON schema.

Objective:

- `tidal_parse_to_json.py` becomes a thin wrapper
- parsing logic no longer lives in the script body

### Phase 3: Move enrichment into the parser

This is the key architectural change and the hardest phase.

Take the logic currently embedded in these matcher-side functions:

- `parse_heading_metadata()`
- `parse_performer_metadata()`
- `extract_review_slug()`
- `extract_italicized_phrases()`
- `build_slug_phrases()`
- `prune_artist_values()`
- `prune_composer_values()`
- `enrich_album_from_source()`

and relocate it into the parser library so that the canonical parse output is
already enriched.

The parser should consume the full source block once and emit the final
match-ready record.

#### Performer parsing convergence

The central difficulty in this phase is that performer parsing is implemented
twice with different capabilities:

- `split_performer_hint()` (parser, line 512) — straightforward splitting by
  `/`, `;`, `,` delimiters with instrument token detection
- `parse_performer_metadata()` (matcher, line 1364) — richer implementation
  that handles markdown formatting (bold, underscores), instrument tokens
  wrapped in underscores, `&` for multiple performers, ensemble detection
  via keyword matching, and conductor extraction from `/` splits

The matcher version is strictly more capable. The migration must converge on
a single implementation — almost certainly the matcher's version, moved into
the parser.

Before merging, diff the output of both functions on the truth set to understand
exactly where they diverge. Any case where `split_performer_hint()` currently
produces different output from `parse_performer_metadata()` is a case where
the parser's enriched output will change.

#### What makes this phase hard

`enrich_album_from_source()` does not just re-extract fields. It:

1. reads the original markdown file from disk
2. finds the source line and extracts surrounding lines
3. parses heading, performer, and label from those lines
4. merges results with the existing (partial) record

For the parser to do this, it needs the full block context at parse time. The
parser already has this via `candidate_from_markdown_block()`, but the enrichment
logic is intertwined with matcher-specific concerns that need to be untangled.

Objective:

- delete matcher-side reparsing of source blocks
- matcher consumes canonical records only

Acceptance condition for this phase:

- `tidal_match_from_json.py` no longer calls `enrich_album_from_source()`
- equivalent enrichment now happens before JSON is written
- performer parsing uses a single converged implementation

### Phase 4: Move matching logic into the library

Move query generation and scoring out of `tidal_match_from_json.py` into
`match.py`.

Target library functions:

- `build_query_candidates(album, rng, shuffle_count)`
- `select_query_candidates(candidates, template_weights, max_queries, rng)`
- `search_candidates_for_album(client, album, weights, selected_queries, limit, sleep)`
- `score_candidate(album, hit, weights)`
- `choose_auto_candidate(ordered, score_threshold, recent_year, recent_threshold)`

Objective:

- matcher script becomes mostly CLI interaction and file IO
- query/scoring logic is unit-like and reusable

### Phase 5: Move review/truth logic into the library

Move record construction and truth persistence into reusable library functions.

Target library functions:

- `build_record()`
- `load_truth_records()`
- `save_truth_records()`
- `summarize_review_records()`
- `collect_selected_album_ids()`
- training helpers used by `--train-coverage`

Objective:

- review behavior is not buried in one large script
- truth generation has a clear API

### Phase 6: Move markdown link application into the library

Lift `load_updates()`, `find_block_end()`, and `apply_updates()` out of
`tidal_apply_links_to_markdown.py` and into `links.py`.

Objective:

- all post-processing stages are colocated with the rest of the pipeline

### Phase 7: Reduce scripts to wrappers

After the above moves, the scripts should each contain only:

- argument parsing
- invocation of library functions
- user-facing printing
- exit-code handling

Anything reusable should already live in the shared package.

## Recommended Implementation Order

Implement in this order to minimize breakage:

1. TidalClient + OAuth extraction (most self-contained, zero behavioral risk)
2. shared models + constants consolidation + normalize helpers
3. source + parser migration
4. performer parsing convergence + parser-side enrichment migration
5. query/scoring migration
6. review/truth migration
7. links migration
8. cleanup and dead-code deletion

This order front-loads the lowest-risk extraction (TidalClient) to validate
the package structure, then tackles the most important architectural fix
(canonical enriched parser output) while the momentum is high.

## Behavior-Preservation Strategy

Before deleting old paths, keep behavior stable with fixture-based comparisons.

Use at least these checks:

- parser output on a small markdown fixture
- parser output on `best2025.md`
- matcher output on `test_2.md`
- matcher output against the existing truth set in `album-debug/best2025.truth.json`
- link insertion dry-run on `best2025.md`

Recommended validation commands during the refactor:

```bash
pipx run ./tidal_parse_to_json.py best2025.md --output /tmp/best2025.albums.json
pipx run ./tidal_match_from_json.py /tmp/best2025.albums.json --batch-review
pipx run ./tidal_apply_links_to_markdown.py best2025.md album-debug/best2025.truth.json --dry-run
```

For focused matching regression checks:

```bash
pipx run ./test_matching.py test_2.md
```

## Compatibility Requirements

The refactor should preserve these interfaces:

- existing script filenames
- existing primary CLI flags
- existing truth JSON format, or else provide a deliberate migration path
- existing markdown link format

If the parser output schema is improved, prefer backward-compatible additions over a
breaking schema rewrite unless the break materially simplifies the system.

## Specific Code Moves

### Move out of `tidal_match_from_json.py` into `client.py` (Phase 0)

- `OAuthHandler` class and all methods
- `save_tokens()`, `load_tokens()`, `token_country_code()`,
  `resolve_country_code()`, `get_valid_token()`
- `TidalClient` class and all methods

### Move out of `tidal_parse_to_json.py` into `parse.py`

- `Candidate` replacement or equivalent model
- `should_skip_candidate()`
- `add_candidate()`
- `parse_mhtml()`
- `parse_markdown_blocks()`
- `parse_markdown_legacy()`
- `parse_markdown()`
- `extract_candidates()`
- `extract_instruments()`
- `is_ensemble()`
- `split_performer_hint()` (to be converged with `parse_performer_metadata()`)
- `candidate_to_entry()`

### Move out of both scripts into `normalize.py`

- `normalize()` (converge the two implementations)
- `normalize_with_symbols()`
- `tokenize()`, `split_tokens()`, `tokens_from_list()`, `artist_tokens_from_list()`
- `normalize_instruments()`
- `phrase_overlap_score()`, `overlap_score()`
- `extract_year()`, `extract_numeric_tokens()`
- `merge_unique()`
- `looks_like_ensemble()`, `clean_markdown_inline()`, `strip_generic_prefixes()`

### Move out of `tidal_match_from_json.py` into `match.py`

- query generation: `build_query_candidates()`, `weighted_sample()`,
  `query_family()`, `select_query_candidates()`
- scoring: `score_hit()`
- search orchestration: `search_candidates_for_album()`,
  `choose_auto_candidate()`
- record building: `build_record()`, `build_record_id()`
- truth/training: `load_truth_records()`, `save_truth_records()`,
  `update_scoring_weights()`, `update_template_weights()`, `train_coverage()`

### Move out of `tidal_match_from_json.py` into `parse.py` (Phase 3)

- `parse_heading_metadata()`, `parse_performer_metadata()`
- `extract_review_slug()`, `extract_italicized_phrases()`, `build_slug_phrases()`
- `prune_artist_values()`, `prune_composer_values()`
- `enrich_album_from_source()`
- `resolve_source_path()`, `extract_source_subsection()`

### Move out of `tidal_apply_links_to_markdown.py` into `links.py`

- `LinkUpdate` model or equivalent
- `load_updates()` (truth-to-link extraction)
- `find_block_end()`, `apply_updates()` (markdown block update logic)

### Keep in scripts

- `tidal_parse_to_json.py`: argument parsing, JSON output writing
- `tidal_match_from_json.py`: argument parsing, interactive prompt loop
  (`prompt_for_choice()`, `show_help()`), top-level orchestration, user-facing
  display formatting
- `tidal_apply_links_to_markdown.py`: argument parsing, file read/write

## Cleanup Targets

Once the refactor is complete:

1. delete matcher-side reparsing of source blocks
2. remove duplicated normalize helpers across scripts
3. remove duplicated constant definitions across scripts
4. remove stale parser code paths that were only needed because parsing was split
5. update README so the documented pipeline matches the code exactly
6. update `test_matching.py` to import from the library (if it exists and is in scope)

## Acceptance Criteria

The refactor is complete when all of the following are true:

1. All reusable TIDAL pipeline logic lives in a shared library package.
2. `tidal_parse_to_json.py` emits the canonical enriched album record.
3. `tidal_match_from_json.py` does not reparse source blocks to repair parser output.
4. Existing scripts still work as CLI entrypoints.
5. The existing truth workflow still works end-to-end.
6. Matching quality on the current truth set is unchanged or improved.
7. `TIDAL_ARCHITECTURE.md` exists, maps each pipeline stage to its implementing
   module and key functions, and is accurate against the code.
8. README links to `TIDAL_ARCHITECTURE.md` instead of inlining the formal
   pipeline description.

## Architecture Documentation

The formal pipeline math currently in the README
(`$x_i \xrightarrow{P} a_i^{(0)} \ldots$`) is a valuable asset — most projects
of this size never write down what they're actually doing. But it will drift
after the refactor unless there is a deliberate place and convention for it.

Move the formal pipeline description out of the README and into a dedicated
`TIDAL_ARCHITECTURE.md` file. The README should link to it but not duplicate it.

`TIDAL_ARCHITECTURE.md` should contain:

- the formal pipeline stages and their notation
- a mapping from each stage to the module and key function(s) that implement it
- the canonical data contract (fields of `AlbumRecord`, what the matcher may
  and may not do with source provenance)
- the scoring model (features, weights, penalties)

To keep it in sync: any PR that changes a pipeline stage's module location,
renames a key function, or alters the scoring model should update
`TIDAL_ARCHITECTURE.md` in the same commit. Add this as an item in the
acceptance criteria and note it in `CLAUDE.md` or equivalent project conventions
so that future contributors (human or AI) are aware of the rule.

## Non-Goals

This plan does not require:

- replacing the current JSON-first workflow
- introducing a large test framework
- changing the user-facing scripts into a packaged application
- redesigning the TIDAL matching algorithm itself
- introducing new dependencies (`markdown-it-py`, `pydantic`, `RapidFuzz`) —
  these are deferred to follow-up passes after the structural refactor

The goal is structural: make the current pipeline coherent, canonical, and easy to
iterate on.
