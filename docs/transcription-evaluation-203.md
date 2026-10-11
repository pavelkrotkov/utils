# Local transcription evaluation (#203)

Evaluation date: 2026-10-10. Baseline code: `65d1bd78fda5fb4378c597a89682eb1ad06efbbe`.
Resource-guard measurements use `efbd94a3a5f1577fe642f344ef3e345fb683963e`.
The later review fixes improve failure reporting without changing that policy.

This is a real-device evaluation, not the stub-based Swift integration matrix.
The available machine is a **16 GB Apple M4 MacBook Air, macOS 27.0.1**.
The user accepted using that hardware and available recordings. No 8 GB or
16 GB M1 result is claimed. The known remote iMac address was unreachable.

The desktop remained in normal use; these are observations, not controlled
hardware benchmarks. Runs used the existing model caches. Each measured
backend ran separately. No audio or transcript was uploaded, no paid API
was called, and private transcripts remain outside this repository.
MacParakeet ran with telemetry disabled, `--no-history`, and a separate database.

## Inputs and scoring

- **Synthetic challenge:** 64.087875 seconds, generated with macOS `say`:
  Samantha and Daniel speaking technical English at 150 words/minute,
  a three-second pause, then Thomas speaking French. It includes domain
  vocabulary, numbers, and voice changes. This is a known-reference accuracy
  check, **not evidence of human lecture accuracy**.
- **Existing full recording:** `How_JAX_powers_massive_AI_models.m4a`,
  2,905.721905 seconds (48:25.72), processed in full. Its provenance and
  verbatim reference transcript are unavailable. This is not a 60–90-minute
  result. The original file and derived transcripts remain local.

Input SHA-256:

```text
short.wav: 0e5e70cdcdaffa307d6655824ccefe110c9a0ce6faa30e8d8656b7f87b48ade5
JAX recording: da0ff896d45f64491a3fce07656692cd3f37653c949b5e74039cdff64ce5ffa8
```

WER/CER use `jiwer==4.0.0`, lowercasing, replacing `%` with `percent`, then
`' '.join(re.findall(r'\w+', text))`. Accented letters are preserved.
The literal reference has 125 words. A second score applies these same,
predeclared number aliases to both reference and hypothesis before tokenization:
`zero point zero one → 0.01`, `sixty four → 64`, `sixteen → 16`,
`three hundred twelve → 312`, `zero point zero five → 0.05`,
`ninety five → 95`, and `vingt trois → 23`. It has 116 reference tokens.
No domain-word aliases hide recognition errors. CER includes normalized spaces.

Synthetic reference, one paragraph per voice:

```text
Today we compare eigenvalues, covariance matrices, Bayesian inference, and stochastic gradient descent. The learning rate is zero point zero one. We process sixty four samples in each batch and use sixteen gigabytes of memory. A confidence interval describes uncertainty, but correlation does not imply causation. After a pause, another speaker checks the result.
The second speaker reports three hundred twelve observations, a p value of zero point zero five, and a ninety five percent confidence interval. We use JAX for automatic differentiation, PyTorch for tensor operations, and NumPy for numerical arrays. The Fourier transform separates frequency components. Do not confuse precision with recall, or variance with standard deviation.
Bonjour. Nous comparons les valeurs propres et la covariance. Le résultat contient vingt trois observations. Merci beaucoup.
```

## Backend versions

- utils Whisper: Homebrew `whisper-cli`, `ggml-large-v3-turbo-q8_0.bin`.
  Installed whisper-cpp version: 1.9.5.
  Model SHA-256: `317eb69c11673c9de1e1f0d459b253999804ec71ac4c23c17ecf5fbe24e259a1`.
- utils VibeVoice: `mlx-audio 0.4.8`, `mlx 0.32.3`, `mlx-lm 0.32.0`,
  `mlx-community/VibeVoice-ASR-4bit` revision
  `a1a15cb6c7b70f76b588af7e12f6fab34d5ab654`, using 30-second chunks.
- Installed off-the-shelf baseline: `macparakeet-cli 3.1.0`, Parakeet v3,
  raw mode, speaker detection off. This identifies the installed binary,
  not the latest upstream release. Its source and CLI contract are documented
  by [MacParakeet](https://github.com/moona3k/macparakeet).

## Results

| Run | Wall time | Peak single-process RSS | Outcome |
| --- | ---: | ---: | --- |
| Whisper synthetic, original defaults | 7.25 s | 1.065 GiB | Complete |
| Whisper synthetic, resource guard / 4 threads | 6.79 s | 1.065 GiB | Complete; identical output |
| Parakeet v3 synthetic, existing cache | 15.36 s | 0.502 GiB | Complete |
| VibeVoice synthetic, 30 s chunks, before guard | Incomplete | Not captured | Cold Python preparation, cached model; terminated with status 143 during first chunk. Cause not established; no transcript. System memory pressure was high. |
| VibeVoice synthetic, same chunks, guard enabled | 3.77 s | 1.148 GiB | Stopped by memory guard during first chunk; no new transcript |
| Whisper complete 48:26 recording, original defaults | 474.26 s | 1.614 GiB | Complete, 8,673 whitespace-delimited words |
| Whisper complete same recording, guarded app bundle / 4 threads | 210.30 s | 1.451 GiB | Complete; byte-for-byte identical output and strict bundle signature retained |
| Parakeet v3 complete same recording, guarded | 25.74 s | 0.500 GiB | Complete, 8,577 whitespace-delimited words |

These are cached-model runs, not clean-install download measurements. The first
VibeVoice attempt installed 40 Python packages. Short/full inputs are different
jobs; their timings do not establish a controlled cold/warm performance ratio.

Synthetic accuracy:

| Backend | Literal WER / CER | Number-normalized WER / CER | Remaining recognition error |
| --- | --- | --- | --- |
| Whisper (both runs) | 15.20% / 11.74% | 0.86% / 0.39% | `JAX` became `jacks` |
| Parakeet v3 | 16.00% / 12.10% | 1.72% / 0.79% | `gigabytes of memory` became `gigab ofytes memory` |

After number normalization neither completed synthetic output had a deletion
or insertion. Both preserved the French passage and numeric values on this
synthetic test; this says nothing about human accents or speaker attribution.
VibeVoice has no quality score because it produced no complete result.

Full-recording comparison lowercases and tokenizes with `\w+`, then computes
`jiwer.wer(whisper, parakeet)`: **5.665% inter-backend disagreement**, not WER
against truth. JAX/PyTorch/NumPy/GPU/TPU occurrence counts are respectively
55/11/12/8/8 in Whisper and 46/10/12/7/8 in Parakeet. Numeric-token counts are
44 versus 12; spelled-out numbers are not counted by that statistic. These
differences identify terms and number formatting to audit against audio;
they do not establish a winner, real omissions, or numeric error rates.

## Resource safety and recommendation

The local scripts now run under an independent supervisor: at most four CPU
threads, no more than half the CPU count, lower CPU scheduling priority, one
model job at a time, aggregate worker/descendant RSS capped by a sampled
half-RAM budget, and a system available-memory reserve of one eighth of RAM.
On this Mac those thresholds are 8 GiB and 2 GiB. Memory is sampled every
200 ms; reaching either threshold fails the job and stops its process group.
ffmpeg decoder, encoder, and filter pools use one thread. VibeVoice's MLX
cache is limited to 256 MiB, with a half-RAM allocation guideline.

The initial quarter-RAM system reserve refused even short Whisper jobs on the
ordinary desktop; one eighth allowed Whisper while still stopping VibeVoice.
This is a safety guard, **not a hard allocation/GPU quota**. RSS excludes
compressed/swapped memory, GPU utilization is uncapped, and an allocation
can overshoot between polls. MLX documents its allocation limit as a
[guideline](https://ml-explore.github.io/mlx/build/html/python/_autosummary/mlx.core.set_memory_limit.html).
No claim that this makes an operating-system crash impossible is justified.

Keep Whisper as the conservative utils default on this machine. Do not
recommend VibeVoice for this desktop load: it could not complete even the
short challenge within the reserve. Installed Parakeet v3 was much faster
on the full file, but accuracy against a human reference and speaker handling
remain unverified. No 8/16 GB M1 or 60–90-minute result is claimed; #203 remains
open for those gaps rather than treating stub CI or synthetic speech as proof.

A repeat of guarded VibeVoice with diagnostics stopped at 0.38 GiB aggregate
RSS and 1.97 GiB system available memory, confirming the system reserve triggered
before the RSS budget. Memory-inspection errors also fail closed and stop the
process group; they are never interpreted as zero usage. MLX initialization
errors stop before inference and report the dependency/resource-limit failure.

## Finder and cancellation

The packaged version 0.1.0 was tested on this same machine and commit:
Finder double-click launch, initial Private local selection, bundled script
readiness, real local transcription, output beside the input, repeated run,
overwrite confirmation, Reveal in Finder, cloud consent/Cancel, and local
selection after restart all passed. Strict ad-hoc signature verification
still passed after Python ran; no bytecode caches appeared in the bundle.
These first app runs used existing uv/model caches; they are not clean-machine
installation or fresh model-download verification. No signed public download
was tested.

Real cancellation stopped the Python/whisper workers and produced no partial
transcript, but **retained the temporary `audio.wav`**. This is tracked in
[#221](https://github.com/pavelkrotkov/utils/issues/221), including the actual
SIGTERM/finally mismatch and the shell-stub cleanup test's coverage gap.

With the resource supervisor, a real rerun cancelled during native Whisper
inference stopped the worker/descendants, removed its temporary directory,
and produced no new transcript. The supervisor translates TERM to Python
interruption and escalates to KILL after two seconds. A native call that
requires KILL can still bypass Python cleanup.

VibeVoice speaker IDs reset per chunk. Plain text omits labels, so this
comparison cannot validate diarization accuracy or identities across seams.

## Reproduction

Use local paths, separate output paths, and sequential runs. Do not include
private audio or derived transcripts in a PR. Capture elapsed seconds and
maximum resident set size with `/usr/bin/time -l` on macOS. That RSS figure
is the maximum reported for a process in the command, **not a sum of the
process tree or a measurement of total unified/GPU memory**. It cannot prove
an 8 GB machine has enough memory.

```bash
/usr/bin/time -l uv run ./audio_transcribe_whisper.py "$AUDIO" \
  --whisper-bin "$(command -v whisper-cli)" -o "$OUT/whisper.txt" \
  > "$OUT/whisper.stdout" 2> "$OUT/whisper.log"

/usr/bin/time -l uv run ./audio_transcribe_vibevoice.py "$AUDIO" \
  --chunk-seconds 30 --format txt -o "$OUT/vibevoice.txt" \
  > "$OUT/vibevoice.stdout" 2> "$OUT/vibevoice.log"

MACPARAKEET_TELEMETRY=0 DO_NOT_TRACK=1 /usr/bin/time -l \
  macparakeet-cli transcribe "$AUDIO" --engine parakeet --parakeet-model v3 \
  --mode raw --speaker-detection off --no-history \
  --database "$OUT/macparakeet.sqlite3" --format transcript \
  > "$OUT/parakeet.txt" 2> "$OUT/parakeet.log"
```

Local evidence directory: `/private/tmp/issue-203-eval/` (transcripts, logs,
reference, scoring script, scores, and fixture). A private input checksum
allows its owner to identify the same file without publishing its content.

To reproduce the synthetic fixture, save the three reference paragraphs above
as `short-reference.txt`, then use the corresponding macOS voices:

```bash
sed -n '1p' short-reference.txt > voice1.txt
sed -n '2p' short-reference.txt > voice2.txt
sed -n '3p' short-reference.txt > voice3.txt
say -v Samantha -r 150 -f voice1.txt -o voice1.aiff
say -v Daniel -r 150 -f voice2.txt -o voice2.aiff
say -v Thomas -r 150 -f voice3.txt -o voice3.aiff
ffmpeg -v error -threads 1 -filter_complex_threads 1 -i voice1.aiff \
  -f lavfi -t 3 -i anullsrc=r=16000:cl=mono -i voice2.aiff -i voice3.aiff \
  -filter_complex '[0:a]aresample=16000[a0];[2:a]aresample=16000[a2];[3:a]aresample=16000[a3];[a0][1:a][a2][a3]concat=n=4:v=0:a=1[out]' \
  -map '[out]' -threads 1 short.wav
```

For scores, use `uv run --no-project --with jiwer==4.0.0 python scorer.py`
with the normalization and aliases specified above; the core comparison is
`jiwer.process_words(reference, hypothesis)` and `jiwer.cer(reference, hypothesis)`.
Measure full-output disagreement separately, never label it reference accuracy.

Validation of the safety patch: 203 Python tests and 90 subtests passed, three
opt-in model/API smoke tests skipped; 115 native Swift tests passed; local
arm64 packaging and strict codesign verification passed. The new supervisor
has MI 51.53 and maximum cyclomatic complexity 7; its process tests have MI
59.00 and maximum complexity 7. Existing large transcription functions
retain their pre-existing complexity; this patch does not claim they meet
those thresholds. Native CI must additionally validate the exact pushed head.
