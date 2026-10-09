#!/usr/bin/env bash
set -euo pipefail

MAX_MB=25
MAX_BYTES=$((MAX_MB * 1000 * 1000))
TMP_FILE=""
RESPONSE_FILE=""
OUTPUT_TMP=""
CHUNK_TMP_DIR=""

cleanup() {
  for file in "$TMP_FILE" "$RESPONSE_FILE" "$OUTPUT_TMP"; do
    if [ -n "$file" ]; then rm -f "$file"; fi
  done
  if [ -n "$CHUNK_TMP_DIR" ]; then rm -rf "$CHUNK_TMP_DIR"; fi
}

trap cleanup EXIT

# Recommended model for file transcription
MODEL="gpt-transcribe"

show_help() {
  cat <<EOF
Usage: $0 [--model MODEL] [--chunk] input.m4a [output.txt]

Transcribe an audio file using the OpenAI Audio API (/v1/audio/transcriptions).
Audio is uploaded to OpenAI; API usage is billed. Output is plain transcript text.
Files still above 25 MB after compression show a chunk plan without uploading.
Rerun with --chunk to consent to paid uploads of all planned chunks.

Options:
  -m, --model MODEL   Model to use (default: ${MODEL}); requires JSON .text output.
  --chunk             Upload oversized recordings as separate, silence-aligned chunks.
  -h, --help          Show this help.

Recommended model: ${MODEL}
Legacy models whisper-1, gpt-4o-transcribe, and gpt-4o-mini-transcribe
are deprecated and scheduled for removal on February 26, 2027.

Examples:
  $0 recording.m4a
  $0 --chunk long_recording.m4a transcript.txt
EOF
}

# --- Argument parsing ---

INPUT=""
OUTPUT=""
ALLOW_CHUNKS=false

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
    --chunk)
      ALLOW_CHUNKS=true
      shift
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Error: unknown option: $1" >&2
      show_help >&2
      exit 1
      ;;
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

# If output not provided, derive from input
OUTPUT="${OUTPUT:-"${INPUT%.*}.txt"}"

case "$MODEL" in
  whisper-1|gpt-4o-transcribe|gpt-4o-mini-transcribe|gpt-4o-transcribe-diarize)
    echo "Warning: '$MODEL' is deprecated and scheduled for removal on 2027-02-26; use gpt-transcribe." >&2
    ;;
  gpt-transcribe) ;;
  *)
    echo "Warning: '$MODEL' is not the recommended gpt-transcribe model; it must support JSON .text output." >&2
    ;;
esac

if [ ! -f "$INPUT" ]; then
  echo "Error: file not found: $INPUT" >&2
  exit 1
fi
if [ "$OUTPUT" -ef "$INPUT" ]; then
  echo "Error: output must not replace the original input." >&2
  exit 1
fi

SIZE_BYTES=$(wc -c < "$INPUT")
FILE_TO_SEND="$INPUT"
CHUNK_PLAN=""

if [ "$SIZE_BYTES" -gt "$MAX_BYTES" ]; then
  echo "Input file is larger than ${MAX_MB}MB, downsampling with ffmpeg..."
  TMP_FILE=$(mktemp /tmp/transcribe-XXXXXX.m4a)
  ffmpeg -y -i "$INPUT" -ac 1 -ar 16000 -b:a 32k "$TMP_FILE" >/dev/null 2>&1

  if [ ! -s "$TMP_FILE" ]; then
    echo "Error: ffmpeg produced an empty downsampled file." >&2
    exit 1
  fi

  NEW_SIZE=$(wc -c < "$TMP_FILE")
  REDUCTION_PCT=$((100 - (NEW_SIZE * 100 / SIZE_BYTES)))
  echo "Downsampled size: ${SIZE_BYTES} -> ${NEW_SIZE} bytes (${REDUCTION_PCT}% smaller)."
  FILE_TO_SEND="$TMP_FILE"

  if [ "$NEW_SIZE" -gt "$MAX_BYTES" ]; then
    CHUNK_TMP_DIR=$(mktemp -d /tmp/transcribe-chunks-XXXXXX)
    CHUNK_PLAN="$CHUNK_TMP_DIR/plan.tsv"
    if [ "$ALLOW_CHUNKS" = true ]; then
      python3 "$(dirname "$0")/audio_cloud_chunks.py" "$FILE_TO_SEND" "$CHUNK_TMP_DIR" "$MODEL" --extract > "$CHUNK_PLAN"
    else
      python3 "$(dirname "$0")/audio_cloud_chunks.py" "$FILE_TO_SEND" "$CHUNK_TMP_DIR" "$MODEL" > "$CHUNK_PLAN"
    fi
    echo "Oversized recording: independent OpenAI recognition passes planned (start-end seconds):"
    while IFS=$'\t' read -r index start end; do
      printf '  Chunk %s: %s-%s\n' "$index" "$start" "$end"
    done < "$CHUNK_PLAN"
    echo "Each chunk is sent separately to OpenAI, a paid external service."
    if [ "$ALLOW_CHUNKS" = false ]; then
      echo "No audio uploaded. Rerun with --chunk to consent to these uploads and resume failed chunks." >&2
      exit 1
    fi
    while IFS=$'\t' read -r index start end; do
      chunk="$CHUNK_TMP_DIR/chunk-${index}.m4a"
      if [ ! -s "$chunk" ] || [ "$(wc -c < "$chunk")" -gt "$MAX_BYTES" ]; then
        echo "Error: chunk $index is empty or exceeds ${MAX_MB}MB; nothing uploaded." >&2
        exit 1
      fi
    done < "$CHUNK_PLAN"
  fi
fi

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "Error: OPENAI_API_KEY is not set." >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required to parse OpenAI transcription responses." >&2
  exit 1
fi

transcribe_file() {
  local source="$1" destination="$2" status message type
  RESPONSE_FILE=$(mktemp /tmp/transcribe-response-XXXXXX.json)
  if ! status=$(curl -sS -o "$RESPONSE_FILE" -w '%{http_code}' \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -F "file=@${source}" \
    -F "model=${MODEL}" \
    -F "response_format=json" \
    https://api.openai.com/v1/audio/transcriptions); then
    echo "Error: OpenAI API request failed (network error)." >&2
    exit 1
  fi
  if [ "$status" -lt 200 ] || [ "$status" -ge 300 ]; then
    echo "Error: OpenAI API request failed (HTTP ${status})." >&2
    message=$(jq -r '.error.message? // empty' "$RESPONSE_FILE" 2>/dev/null || true)
    type=$(jq -r '.error.type? // empty' "$RESPONSE_FILE" 2>/dev/null || true)
    if [ -n "$message" ]; then
      message="${message//"$OPENAI_API_KEY"/[REDACTED]}"
      type="${type//"$OPENAI_API_KEY"/[REDACTED]}"
      if [ -n "$type" ]; then
        echo "API error (${type}): ${message}" >&2
      else
        echo "API error: ${message}" >&2
      fi
    fi
    exit 1
  fi
  if ! jq -se 'length == 1' "$RESPONSE_FILE" >/dev/null 2>&1; then
    echo "Error: OpenAI API returned invalid JSON." >&2
    exit 1
  fi
  OUTPUT_TMP=$(mktemp "${destination}.XXXXXX")
  if ! jq -er '.text | strings | select(test("\\S"))' "$RESPONSE_FILE" > "$OUTPUT_TMP"; then
    echo "Error: API response did not contain a non-empty '.text' string." >&2
    exit 1
  fi
  mv -f "$OUTPUT_TMP" "$destination"
  OUTPUT_TMP=""
  rm -f "$RESPONSE_FILE"
  RESPONSE_FILE=""
}

if [ -z "$CHUNK_PLAN" ]; then
  echo "Transcribing with OpenAI model: ${MODEL}..."
  transcribe_file "$FILE_TO_SEND" "$OUTPUT"
else
  CACHE_DIR="${OUTPUT}.openai-chunks/$(cat "$CHUNK_TMP_DIR/cache-key")"
  mkdir -p "$CACHE_DIR"
  while IFS=$'\t' read -r index start end; do
    transcript="$CACHE_DIR/chunk-${index}.txt"
    if [ -s "$transcript" ] && grep -q '[^[:space:]]' "$transcript"; then
      echo "Reusing completed chunk ${index}."
    else
      echo "Transcribing chunk ${index} with OpenAI model: ${MODEL}..."
      transcribe_file "$CHUNK_TMP_DIR/chunk-${index}.m4a" "$transcript"
    fi
  done < "$CHUNK_PLAN"
  OUTPUT_TMP=$(mktemp "${OUTPUT}.XXXXXX")
  while IFS=$'\t' read -r index start end; do
    printf '[Chunk %s: %ss-%ss]\n' "$index" "$start" "$end" >> "$OUTPUT_TMP"
    cat "$CACHE_DIR/chunk-${index}.txt" >> "$OUTPUT_TMP"
    printf '\n' >> "$OUTPUT_TMP"
  done < "$CHUNK_PLAN"
  mv -f "$OUTPUT_TMP" "$OUTPUT"
  OUTPUT_TMP=""
  echo "Chunk transcripts retained in: $CACHE_DIR"
fi

echo "Saved transcript to: $OUTPUT"
