#!/usr/bin/env bash
set -euo pipefail

MAX_MB=25
MAX_BYTES=$((MAX_MB * 1024 * 1024))
TMP_FILE=""
RESPONSE_FILE=""
OUTPUT_TMP=""

cleanup() {
  for file in "$TMP_FILE" "$RESPONSE_FILE" "$OUTPUT_TMP"; do
    if [ -n "$file" ]; then rm -f "$file"; fi
  done
}

trap cleanup EXIT

# Models supported by /v1/audio/transcriptions
SUPPORTED_MODELS=(
  "whisper-1"
  "gpt-4o-transcribe"
  "gpt-4o-mini-transcribe"
)

# Default model
MODEL="${SUPPORTED_MODELS[0]}"

show_help() {
  cat <<EOF
Usage: $0 [--model MODEL] input.m4a [output.txt]

Transcribe an audio file using the OpenAI Audio API (/v1/audio/transcriptions).

Options:
  -m, --model MODEL   Transcription model to use (default: ${MODEL})
  -h, --help          Show this help and list supported models.

Supported models for /v1/audio/transcriptions:
$(for m in "${SUPPORTED_MODELS[@]}"; do echo "  - $m"; done)

Examples:
  $0 recording.m4a
  $0 --model gpt-4o-mini-transcribe recording.m4a transcript.txt
EOF
}

# --- Argument parsing ---

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

# Optional: warn if model is not in known list (but still try)
if ! printf '%s\n' "${SUPPORTED_MODELS[@]}" | grep -qx "$MODEL"; then
  echo "Warning: '$MODEL' is not in the known list of speech-to-text models for /v1/audio/transcriptions." >&2
  echo "Known models: ${SUPPORTED_MODELS[*]}" >&2
fi

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

# Get file size (portable: macOS + Linux)
if stat -f%z "$INPUT" >/dev/null 2>&1; then
  SIZE_BYTES=$(stat -f%z "$INPUT")
else
  SIZE_BYTES=$(stat -c%s "$INPUT")
fi

FILE_TO_SEND="$INPUT"

if [ "$SIZE_BYTES" -gt "$MAX_BYTES" ]; then
  echo "Input file is larger than ${MAX_MB}MB, downsampling with ffmpeg..."
  TMP_FILE=$(mktemp /tmp/transcribe-XXXXXX.m4a)
  ffmpeg -y -i "$INPUT" -ac 1 -ar 16000 -b:a 32k "$TMP_FILE" >/dev/null 2>&1

  if [ ! -s "$TMP_FILE" ]; then
    echo "Error: ffmpeg produced an empty downsampled file." >&2
    exit 1
  fi

  if stat -f%z "$TMP_FILE" >/dev/null 2>&1; then
    NEW_SIZE=$(stat -f%z "$TMP_FILE")
  else
    NEW_SIZE=$(stat -c%s "$TMP_FILE")
  fi
  REDUCTION_PCT=$((100 - (NEW_SIZE * 100 / SIZE_BYTES)))
  echo "Downsampled size: ${SIZE_BYTES} -> ${NEW_SIZE} bytes (${REDUCTION_PCT}% smaller)."
  if [ "$NEW_SIZE" -gt "$MAX_BYTES" ]; then
    echo "Downsampled file is still larger than ${MAX_MB}MB. Consider splitting the audio." >&2
    rm -f "$TMP_FILE"
    exit 1
  fi

  FILE_TO_SEND="$TMP_FILE"
fi

echo "Transcribing with OpenAI model: ${MODEL}..."

RESPONSE_FILE=$(mktemp /tmp/transcribe-response-XXXXXX.json)
if ! HTTP_STATUS=$(curl -sS -o "$RESPONSE_FILE" -w '%{http_code}' \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F "file=@${FILE_TO_SEND}" \
  -F "model=${MODEL}" \
  https://api.openai.com/v1/audio/transcriptions); then
  echo "Error: OpenAI API request failed (network error)." >&2
  exit 1
fi

if [ "$HTTP_STATUS" -lt 200 ] || [ "$HTTP_STATUS" -ge 300 ]; then
  echo "Error: OpenAI API request failed (HTTP ${HTTP_STATUS})." >&2
  ERROR_MESSAGE=$(jq -r '.error.message? // empty' "$RESPONSE_FILE" 2>/dev/null || true)
  ERROR_TYPE=$(jq -r '.error.type? // empty' "$RESPONSE_FILE" 2>/dev/null || true)
  if [ -n "$ERROR_MESSAGE" ]; then
    ERROR_MESSAGE="${ERROR_MESSAGE//"$OPENAI_API_KEY"/[REDACTED]}"
    ERROR_TYPE="${ERROR_TYPE//"$OPENAI_API_KEY"/[REDACTED]}"
    if [ -n "$ERROR_TYPE" ]; then
      echo "API error (${ERROR_TYPE}): ${ERROR_MESSAGE}" >&2
    else
      echo "API error: ${ERROR_MESSAGE}" >&2
    fi
  fi
  exit 1
fi

if ! jq -se 'length == 1' "$RESPONSE_FILE" >/dev/null 2>&1; then
  echo "Error: OpenAI API returned invalid JSON." >&2
  exit 1
fi

OUTPUT_TMP=$(mktemp "${OUTPUT}.XXXXXX")
if ! jq -er '.text | strings | select(test("\\S"))' "$RESPONSE_FILE" > "$OUTPUT_TMP"; then
  echo "Error: API response did not contain a non-empty '.text' string." >&2
  exit 1
fi
mv -f "$OUTPUT_TMP" "$OUTPUT"
OUTPUT_TMP=""

echo "Saved transcript to: $OUTPUT"
