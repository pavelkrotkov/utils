# Utils

Standalone utility scripts for PDF conversion, Markdown splitting, audio transcription,
and TIDAL import.

## Overview

- PDF to Markdown conversion (Mathpix API, Docling, LlamaParse, PyMuPDF4LLM, or local marker).
- Markdown splitting into per-section or per-subsection files.
- Audio transcription (OpenAI API) and local diarization (whisper-cpp + pyannote).
- TIDAL playlist import from Gramophone-style MHTML/Markdown pages.

For deeper context, refer to the script headers and inline help.

## Setup

Python dependencies:

```bash
pip install mpxpy marker-pdf docling requests torch pyannote.audio beautifulsoup4 lxml numpy pandas
pip install mpxpy marker-pdf pymupdf4llm[layout] llama-cloud pypdf requests torch pyannote.audio beautifulsoup4 lxml numpy pandas
```

System tools (macOS via Homebrew):

```bash
brew install ffmpeg whisper-cpp jq
```

Environment variables:

- `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` for Mathpix tools.
- `LLAMA_CLOUD_API_KEY` for LlamaParse (LlamaCloud).
- `OPENAI_API_KEY` for OpenAI transcription.
- `TIDAL_CLIENT_ID` for TIDAL import (Required).
- `TIDAL_CLIENT_SECRET` for TIDAL import (Optional for some app types).
- `TIDAL_REDIRECT_URI` (Optional, defaults to `http://127.0.0.1:8765/callback`).
- `TIDAL_SCOPES` (Optional, defaults to playlist and search read/write).
- `TIDAL_COUNTRY_CODE` (Optional, defaults to `US`).
- `HF_TOKEN` for pyannote diarization.
- `GGML_METAL_PATH_RESOURCES` optional for whisper-cpp Metal support.

## PDF Conversion

Mathpix (best for math-heavy PDFs):

```bash
pipx run ./pdf_convert_mathpix_sdk.py input.pdf -o output.md
pipx run ./pdf_convert_mathpix_api.py input.pdf -o output.md
```

Docling (local, structured parsing):

```bash
pipx run ./pdf_convert_docling.py input.pdf -o output.md
pipx run ./pdf_convert_docling.py input.pdf --page-range 1-5
```

Note: If you omit `--page-range`, the script uses Docling defaults. Provide a contiguous range like `1-5` when you want a subset of pages.

LlamaParse (LlamaCloud, hosted):

```bash
pipx run ./pdf_convert_llamaparse.py input.pdf -o output.md
pipx run ./pdf_convert_llamaparse.py input.pdf --page-range 1-5
pipx run ./pdf_convert_llamaparse.py --fetch-job job_id -o output-3.md
```

Create an API key at https://cloud.llamaindex.ai via API Key -> Generate New Key, then set `LLAMA_CLOUD_API_KEY`.

The script always chunks PDFs, saves `output-<i>.md` partials, and skips existing chunks on rerun to resume.

PyMuPDF4LLM (local, fast layout-aware parsing):

```bash
pipx run ./pdf_convert_pymupdf4llm.py input.pdf -o output.md
pipx run ./pdf_convert_pymupdf4llm.py input.pdf --page-range 0-4
```

Layout mode requires `pymupdf4llm[layout]` (or `pymupdf4llm[ocr,layout]` for OCR support).

Marker (best for simpler PDFs, local):

```bash
pipx run ./pdf_convert_marker.py input.pdf -o output.md
```

## Markdown Tools

Split a Markdown file into smaller Markdown files in the same folder at each `##` or
`###` heading.

```bash
./markdown_split.sh notes.md '##'
./markdown_split.sh notes.md '###'
```

Behavior:

- Uses the full heading text as the output filename.
- Appends ` 2`, ` 3`, etc. only when duplicate heading names collide.
- Writes content before the first matching heading to `<input> Preamble.md`.
- Ignores matching headings inside fenced code blocks.

## Audio & Music Tools

### TIDAL Import

Import classical album tracklists from Gramophone-style MHTML or Markdown files into TIDAL playlists. Uses OAuth 2.1 with PKCE for secure authentication.

```bash
# Preview matches (Dry Run)
pipx run ./tidal_import_page_to_playlist.py test.md --dry-run

# Import to TIDAL (Creates playlist)
pipx run ./tidal_import_page_to_playlist.py test.mhtml
```

#### Matching Strategy

The importer uses a multi-stage matching strategy optimized for classical music:

1. **Performer extraction** - Parses complex performer strings (e.g., "Orchestra / Conductor (Label)") to extract conductor and soloist names, stripping instrument abbreviations (pf, vn, vc, hpd, etc.)

2. **Query construction** - Generates multiple search queries combining:
   - Extracted performer surnames with title tokens
   - Cleaned performer names with composer
   - Review slugs and label hints
   - Fallback composer + work type queries

3. **Scoring** - Matches are scored based on:
   - Title token overlap (up to 0.5)
   - Performer match in artists or title (+0.4 if found)
   - Label match in copyright (+0.2)
   - Reduced penalty when album likely not in catalog

4. **Fallback** - Track-based search when album search fails

#### Testing Matches

Use the built-in test report to validate and compare matching results:

```bash
# Quick report on a file
pipx run ./tidal_import_page_to_playlist.py input.md --dry-run --test-report

# Save baseline and compare after changes
pipx run ./tidal_import_page_to_playlist.py input.mhtml --dry-run --test-report-save baseline.json
pipx run ./tidal_import_page_to_playlist.py input.mhtml --dry-run --test-report-compare baseline.json
```

#### JSON-First Matching (Experimental)

Split parsing and matching for controlled tuning. Use `tidal_parse_prompt.md` to produce structured
JSON, then interactively label ground-truth matches with `tidal_match_from_json.py`.

```bash
# Label matches and save a truth file alongside the input JSON
pipx run ./tidal_match_from_json.py albums.json --resume
```

Train a query/matching model from the first 80 truth records:

```bash
pipx run ./tidal_match_from_json.py albums.json --train-coverage --training-out tidal_match_training.json
```

#### Formal Matching Pipeline

The matcher can be described as a pipeline over album records indexed by $i$.

Let:

- $x_i$ be the source review block for album $i$
- $a_i^{(0)}$ be the raw parsed album record emitted by `tidal_parse_to_json.py`
- $a_i$ be the enriched album record actually used for matching

The end-to-end flow is

$$
x_i \xrightarrow{P} a_i^{(0)}
\xrightarrow{E} a_i
\xrightarrow{G} Q_i^{\mathrm{all}}
\xrightarrow{S} \widetilde{Q}_i
\xrightarrow{\text{TIDAL}} H_i
\xrightarrow{D} C_i
\xrightarrow{\text{score}} \hat{c}_i
\xrightarrow{\text{decision}} y_i.
$$

Here:

- $P$ is the raw parser
- $E$ is the enrichment step
- $G$ generates candidate queries
- $S$ selects a capped subset of those queries
- $D$ deduplicates repeated TIDAL hits by album id
- $\hat{c}_i$ is the top-ranked candidate
- $y_i$ is the final decision recorded in the truth file

The raw parse is

$$
a_i^{(0)} = P(x_i).
$$

The matcher then reparses the original source block and enriches the record:

$$
a_i = E(a_i^{(0)}, x_i).
$$

Conceptually, $a_i$ is a structured object with fields

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
\text{instruments}_i
).
$$

The enrichment step matters because it can recover cleaner composer, performer,
ensemble, conductor, label, and work hints from the original source text.

From $a_i$, the matcher generates many candidate queries

$$
Q_i^{\mathrm{all}} = G(a_i) = \{q_{ik}\}_{k=1}^{K_i}.
$$

These queries come from templates such as `composer_title`, `performer_work`,
`ensemble_title`, `conductor_composer`, and `label_title`. The code does not send
all generated queries to TIDAL. It first selects a diversified subset:

$$
\widetilde{Q}_i = S\!\left(Q_i^{\mathrm{all}}\right),
\qquad
\left|\widetilde{Q}_i\right| \le K_{\max}.
$$

For each selected query $q_{ik} \in \widetilde{Q}_i$, TIDAL returns a small hit list

$$
H_{ik} = \{r_{ikl}\}_{l=1}^{L_{ik}}.
$$

The full raw hit set is

$$
H_i = \bigcup_k H_{ik}.
$$

Those raw hits are then deduplicated by TIDAL album id:

$$
C_i = D(H_i).
$$

Scoring is applied to each unique candidate $c \in C_i$, not to each raw search hit
independently.

For each candidate $c \in C_i$, the matcher computes feature similarities

$$
\phi_f(a_i, c) \in [0,1]
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

The base score is a weighted sum

$$
s_i^{\mathrm{base}}(c) = \sum_f w_f \, \phi_f(a_i, c),
$$

and the final score applies multiplicative penalties for cases such as weak artist
support, missing composer evidence on generic titles, or numeric mismatches:

$$
s_i(c) = p_i(c) \cdot s_i^{\mathrm{base}}(c),
\qquad 0 < p_i(c) \le 1.
$$

Candidates are ranked by score, then by supporting evidence such as how many
selected queries surfaced the same album:

$$
\hat{c}_i = \arg\max_{c \in C_i} \operatorname{rank}_i(c).
$$

In practice, the ranking key is based primarily on $s_i(c)$, then on query support,
then on a stable title tiebreak.

The final decision $y_i$ is:

$$
y_i =
\begin{cases}
\hat{c}_i.\mathrm{id}, & \text{if the top candidate passes the auto-selection rule} \\
\text{manual review}, & \text{otherwise.}
\end{cases}
$$

The auto-selection rule accepts $\hat{c}_i$ when its score exceeds the main threshold,
or when a recent-release rule is satisfied. Otherwise the record is marked
`needs_review`, and the user chooses a ranked candidate, `none`, `skip`, or an
explicit TIDAL album id.

The resulting truth set is

$$
T = \{(a_i, \widetilde{Q}_i, C_i, y_i)\}_{i=1}^{N}.
$$

If desired, `--train-coverage` uses a prefix of $T$ to update both the feature
weights $w_f$ and the query-template weights used by $S$.

Finally, if $y_i$ is a selected TIDAL album id, `tidal_apply_links_to_markdown.py`
maps it back to the source line and inserts

$$
\texttt{https://tidal.com/browse/album/<id>}
$$

at the end of the corresponding markdown subsection.

## Audio Transcription

OpenAI API (simple transcription):

```bash
./audio_transcribe_openai.sh recording.m4a output.txt
```

Local whisper-cpp + pyannote (with diarization):

```bash
./audio_transcribe_whisper.py interview.m4a --num-speakers 2
```

## Notes

- Scripts are standalone and run directly; no central build or test harness.
- Prefer small sample files for quick validation.
- Outputs default next to the input file unless overridden.

## Linting

```bash
ruff check .
ruff format --check .
```
