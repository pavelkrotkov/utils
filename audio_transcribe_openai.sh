#!/usr/bin/env bash
set -euo pipefail

MAX_MB=25
MAX_BYTES=$((24 * 1000 * 1000))
TMP_FILE=""
RESPONSE_FILE=""
OUTPUT_TMP=""
CHUNK_DIR=""

cleanup() {
  for file in "$TMP_FILE" "$RESPONSE_FILE" "$OUTPUT_TMP"; do
    if [ -n "$file" ]; then rm -f "$file"; fi
  done
  if [ -n "$CHUNK_DIR" ]; then rm -rf "$CHUNK_DIR"; fi
}

trap cleanup EXIT

MODEL="gpt-transcribe"
CHUNK=false
ASSUME_YES=false

show_help() {
  cat <<EOF
Usage: $0 [--model MODEL] [--chunk [--yes]] input.m4a [output.txt]

Transcribe an audio file using the OpenAI Audio API (/v1/audio/transcriptions).
Audio is uploaded to OpenAI; API usage is billed. Output is plain transcript text.

Options:
  -m, --model MODEL   Model to use (default: ${MODEL}); requires JSON .text output.
  --chunk             For over-limit files, split near silences and request each part.
  -y, --yes           Confirm paid multi-part uploads without prompting (with --chunk).
  -h, --help          Show this help.

Long files are compressed first. If still over the 25 MB API limit, --chunk
prepares <= 30-minute parts locally and asks before any paid uploads. Completed
parts are saved beside the output as output.txt.parts for safe retry/resume.
Parts are labeled in the final transcript; hard-cut overlaps may repeat words.

Recommended model: ${MODEL}
Legacy models whisper-1, gpt-4o-transcribe, and gpt-4o-mini-transcribe
are deprecated and scheduled for removal on February 26, 2027.

Examples:
  $0 recording.m4a
  $0 --chunk recording.m4a transcript.txt
  $0 --chunk --yes long-recording.m4a transcript.txt
EOF
}

file_size() {
  stat -f%z "$1" 2>/dev/null || stat -c%s "$1"
}

transcribe_file() {
  local source="$1" destination="$2" http_status error_message error_type
  RESPONSE_FILE=$(mktemp /tmp/transcribe-response-XXXXXX.json)
  if ! http_status=$(curl -sS -o "$RESPONSE_FILE" -w '%{http_code}' \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -F "file=@${source}" \
    -F "model=${MODEL}" \
    -F "response_format=json" \
    https://api.openai.com/v1/audio/transcriptions); then
    echo "Error: OpenAI API request failed (network error)." >&2
    return 1
  fi

  if [ "$http_status" -lt 200 ] || [ "$http_status" -ge 300 ]; then
    echo "Error: OpenAI API request failed (HTTP ${http_status})." >&2
    error_message=$(jq -r '.error.message? // empty' "$RESPONSE_FILE" 2>/dev/null || true)
    error_type=$(jq -r '.error.type? // empty' "$RESPONSE_FILE" 2>/dev/null || true)
    if [ -n "$error_message" ]; then
      error_message="${error_message//"$OPENAI_API_KEY"/[REDACTED]}"
      error_type="${error_type//"$OPENAI_API_KEY"/[REDACTED]}"
      if [ -n "$error_type" ]; then
        echo "API error (${error_type}): ${error_message}" >&2
      else
        echo "API error: ${error_message}" >&2
      fi
    fi
    return 1
  fi

  if ! jq -se 'length == 1' "$RESPONSE_FILE" >/dev/null 2>&1; then
    echo "Error: OpenAI API returned invalid JSON." >&2
    return 1
  fi

  OUTPUT_TMP=$(mktemp "${destination}.XXXXXX")
  if ! jq -er '.text | strings | select(test("\\S"))' "$RESPONSE_FILE" > "$OUTPUT_TMP"; then
    echo "Error: API response did not contain a non-empty '.text' string." >&2
    return 1
  fi
  mv -f "$OUTPUT_TMP" "$destination"
  OUTPUT_TMP=""
  rm -f "$RESPONSE_FILE"
  RESPONSE_FILE=""
}

prepare_chunks() {
  local script_dir start end duration overlap chunk size line index=0
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  CHUNK_DIR=$(mktemp -d /tmp/transcribe-chunks-XXXXXX)
  PLAN_FILE="$CHUNK_DIR/plan.tsv"
  if ! PYTHONPATH="$script_dir${PYTHONPATH:+:$PYTHONPATH}" python3 - "$FILE_TO_SEND" > "$PLAN_FILE" <<'PY'
import sys
from pathlib import Path
from audio_common import probe_media_duration
from audio_transcribe_vibevoice import detect_silences, plan_chunks

path = Path(sys.argv[1])
duration = probe_media_duration(path, "ffprobe")
if not duration:
    raise SystemExit("Error: cannot determine audio duration for chunking")
silences = detect_silences(path, noise_db=-30.0, min_silence=0.5)
for chunk in plan_chunks(duration, 1800.0, silences, overlap=2.5):
    print(f"{chunk.start:.3f}\t{chunk.end:.3f}\t{chunk.end - chunk.start:.3f}\t{int(chunk.overlaps_previous)}")
PY
  then
    echo "Error: unable to plan chunks. Check python3, ffmpeg/ffprobe and the local audio_common.py/audio_transcribe_vibevoice.py modules." >&2
    return 1
  fi

  PLANS=()
  CHUNK_FILES=()
  while IFS= read -r line; do
    if [ -n "$line" ]; then PLANS+=("$line"); fi
  done < "$PLAN_FILE"
  if [ "${#PLANS[@]}" -eq 0 ]; then
    echo "Error: no audio chunks could be planned." >&2
    return 1
  fi
  for line in "${PLANS[@]}"; do
    IFS=$'\t' read -r start end duration overlap <<< "$line"
    index=$((index + 1))
    chunk=$(printf '%s/chunk-%04d.m4a' "$CHUNK_DIR" "$index")
    if ! ffmpeg -v error -y -ss "$start" -t "$duration" -i "$FILE_TO_SEND" -c copy "$chunk"; then
      echo "Error: could not extract chunk $index." >&2
      return 1
    fi
    size=$(file_size "$chunk")
    if [ "$size" -eq 0 ] || [ "$size" -ge "$MAX_BYTES" ]; then
      echo "Error: chunk $index is empty or exceeds the safe 24 MB upload threshold." >&2
      return 1
    fi
    CHUNK_FILES+=("$chunk")
    printf 'Chunk %d: %s-%ss (%s bytes)%s\n' "$index" "$start" "$end" "$size" \
      "$(if [ "$overlap" = 1 ]; then printf ' [overlap]'; fi)"
  done
}

INPUT=""
OUTPUT=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    -m|--model)
      if [ "$#" -lt 2 ]; then
        echo "Error: --model requires an argument" >&2
        exit 1
      fi
      MODEL="$2"
      shift 2
      ;;
    --chunk) CHUNK=true; shift ;;
    -y|--yes) ASSUME_YES=true; shift ;;
    -h|--help) show_help; exit 0 ;;
    --) shift; break ;;
    -*) echo "Error: unknown option: $1" >&2; show_help >&2; exit 1 ;;
    *)
      if [ -z "$INPUT" ]; then
        INPUT="$1"
      elif [ -z "${OUTPUT:-}" ]; then
        OUTPUT="$1"
      else
        echo "Error: too many positional arguments" >&2
        show_help >&2
        exit 1
      fi
      shift
      ;;
  esac
done

if [ -z "$INPUT" ]; then
  echo "Error: missing input file." >&2
  show_help >&2
  exit 1
fi
OUTPUT="${OUTPUT:-"${INPUT%.*}.txt"}"

case "$MODEL" in
  whisper-1|gpt-4o-transcribe|gpt-4o-mini-transcribe|gpt-4o-transcribe-diarize)
    echo "Warning: '$MODEL' is deprecated and scheduled for removal on 2027-02-26; use gpt-transcribe." >&2
    ;;
  gpt-transcribe) ;;
  *) echo "Warning: '$MODEL' is not the recommended gpt-transcribe model; it must support JSON .text output." >&2 ;;
esac

if [ ! -f "$INPUT" ]; then
  echo "Error: file not found: $INPUT" >&2
  exit 1
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "Error: OPENAI_API_KEY is not set." >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required to parse OpenAI transcription responses." >&2
  exit 1
fi

SIZE_BYTES=$(file_size "$INPUT")
FILE_TO_SEND="$INPUT"
if [ "$SIZE_BYTES" -ge "$MAX_BYTES" ]; then
  echo "Input exceeds the safe 24 MB upload threshold; downsampling with ffmpeg..."
  TMP_FILE=$(mktemp /tmp/transcribe-XXXXXX.m4a)
  ffmpeg -y -i "$INPUT" -ac 1 -ar 16000 -b:a 32k "$TMP_FILE" >/dev/null 2>&1
  if [ ! -s "$TMP_FILE" ]; then
    echo "Error: ffmpeg produced an empty downsampled file." >&2
    exit 1
  fi
  NEW_SIZE=$(file_size "$TMP_FILE")
  echo "Downsampled size: ${SIZE_BYTES} -> ${NEW_SIZE} bytes."
  FILE_TO_SEND="$TMP_FILE"
fi

if [ "$(file_size "$FILE_TO_SEND")" -lt "$MAX_BYTES" ]; then
  echo "Transcribing with OpenAI model: ${MODEL}..."
  transcribe_file "$FILE_TO_SEND" "$OUTPUT"
else
  if [ "$CHUNK" != true ]; then
    echo "Error: still over the 25 MB API limit. Rerun with --chunk to review and consent to paid multi-part uploads." >&2
    exit 1
  fi
  prepare_chunks
  TOTAL=${#CHUNK_FILES[@]}
  echo "$TOTAL parts will each be uploaded to OpenAI's paid external API. No audio has been uploaded yet."
  if [ "$ASSUME_YES" != true ]; then
    printf 'Upload all parts? [y/N] ' >&2
    if ! read -r confirmation || [[ ! "$confirmation" =~ ^([yY]|[yY][eE][sS])$ ]]; then
      echo "Cancelled; no audio uploaded." >&2
      exit 1
    fi
  fi
  CACHE_DIR="${OUTPUT}.parts"
  mkdir -p "$CACHE_DIR"
  PART_FILES=()
  for chunk in "${CHUNK_FILES[@]}"; do
    key=$({ printf '%s\0' "$MODEL"; cat "$chunk"; } | shasum -a 256 | cut -d' ' -f1)
    cached="$CACHE_DIR/${key}.txt"
    if [ ! -s "$cached" ]; then
      echo "Transcribing chunk $((${#PART_FILES[@]} + 1))/${TOTAL}..."
      transcribe_file "$chunk" "$cached"
    else
      echo "Reusing completed chunk $((${#PART_FILES[@]} + 1))/${TOTAL}."
    fi
    PART_FILES+=("$cached")
  done
  OUTPUT_TMP=$(mktemp "${OUTPUT}.XXXXXX")
  index=0
  for line in "${PLANS[@]}"; do
    IFS=$'\t' read -r start end duration overlap <<< "$line"
    seam=""
    if [ "$overlap" = 1 ]; then seam="; overlapping audio, review repeated words"; fi
    printf '[Chunk %d/%d: %s-%s seconds%s]\n' "$((index + 1))" "$TOTAL" "$start" "$end" "$seam" >> "$OUTPUT_TMP"
    cat "${PART_FILES[$index]}" >> "$OUTPUT_TMP"
    printf '\n' >> "$OUTPUT_TMP"
    index=$((index + 1))
  done
  mv -f "$OUTPUT_TMP" "$OUTPUT"
  OUTPUT_TMP=""
fi

echo "Saved transcript to: $OUTPUT"
