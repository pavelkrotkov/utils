#!/usr/bin/env bash
set -euo pipefail

MAX_MB=25
MAX_BYTES=$((MAX_MB * 1024 * 1024))

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

# Get file size (macOS)
SIZE_BYTES=$(stat -f%z "$INPUT")

FILE_TO_SEND="$INPUT"
TMP_FILE=""

if [ "$SIZE_BYTES" -gt "$MAX_BYTES" ]; then
  echo "Input file is larger than ${MAX_MB}MB, downsampling with ffmpeg..."
  TMP_FILE=$(mktemp /tmp/transcribe-XXXXXX.m4a)
  ffmpeg -y -i "$INPUT" -ac 1 -ar 16000 -b:a 32k "$TMP_FILE" >/dev/null 2>&1

  NEW_SIZE=$(stat -f%z "$TMP_FILE")
  if [ "$NEW_SIZE" -gt "$MAX_BYTES" ]; then
    echo "Downsampled file is still larger than ${MAX_MB}MB. Consider splitting the audio." >&2
    rm -f "$TMP_FILE"
    exit 1
  fi

  FILE_TO_SEND="$TMP_FILE"
fi

echo "Transcribing with OpenAI model: ${MODEL}..."

RESPONSE=$(curl -sS \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -F "file=@${FILE_TO_SEND}" \
  -F "model=${MODEL}" \
  https://api.openai.com/v1/audio/transcriptions)

# Clean up temp file if used
if [ -n "${TMP_FILE:-}" ]; then
  rm -f "$TMP_FILE"
fi

# If jq is available, extract just the text field
if command -v jq >/dev/null 2>&1; then
  echo "$RESPONSE" | jq -r '.text' > "$OUTPUT"
else
  # Fallback: save raw JSON
  echo "$RESPONSE" > "$OUTPUT"
fi

echo "Saved transcript to: $OUTPUT"
