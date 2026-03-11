#!/usr/bin/env bash
set -euo pipefail

show_help() {
  cat <<'EOF'
Usage: ./markdown_split.sh input.md ##|###

Split a Markdown file into smaller Markdown files in the same folder.

Arguments:
  input.md   Markdown file to split
  ##         Split at level-2 headings
  ###        Split at level-3 headings

Examples:
  ./markdown_split.sh notes.md '##'
  ./markdown_split.sh notes.md '###'
EOF
}

trim_whitespace() {
  printf '%s' "$1" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//'
}

sanitize_filename() {
  local value
  value=$(printf '%s' "$1" | sed -E \
    -e 's/[[:space:]]+/ /g' \
    -e 's#/# - #g' \
    -e 's/[][<>:"\\|?*]//g')
  value=$(trim_whitespace "$value")

  if [ -z "$value" ]; then
    value="Untitled"
  fi

  printf '%s' "$value"
}

next_output_path() {
  local label="$1"
  local stem
  local candidate
  local suffix=2

  stem=$(sanitize_filename "$label")
  candidate="$OUTPUT_DIR/$stem.md"

  while [ -e "$candidate" ]; do
    candidate="$OUTPUT_DIR/$stem $suffix.md"
    suffix=$((suffix + 1))
  done

  printf '%s' "$candidate"
}

start_chunk() {
  local label="$1"
  CURRENT_OUTPUT=$(next_output_path "$label")
  : > "$CURRENT_OUTPUT"
  CREATED_FILES=$((CREATED_FILES + 1))
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  show_help
  exit 0
fi

if [ "$#" -ne 2 ]; then
  echo "ERROR: expected 2 arguments." >&2
  show_help >&2
  exit 1
fi

INPUT_PATH="$1"
SPLIT_MARKER="$2"

case "$SPLIT_MARKER" in
  '##'|'###')
    ;;
  *)
    echo "ERROR: split marker must be '##' or '###'." >&2
    show_help >&2
    exit 1
    ;;
esac

if [ ! -f "$INPUT_PATH" ]; then
  echo "ERROR: file not found: $INPUT_PATH" >&2
  exit 1
fi

INPUT_DIRNAME=$(dirname "$INPUT_PATH")
OUTPUT_DIR=$(cd "$INPUT_DIRNAME" && pwd)
INPUT_FILENAME=$(basename "$INPUT_PATH")
INPUT_BASENAME="${INPUT_FILENAME%.*}"
PREAMBLE_NAME="$INPUT_BASENAME Preamble"

CURRENT_OUTPUT=""
CREATED_FILES=0
IN_FENCE=0
FENCE_CHAR=""
FENCE_LEN=0

while IFS= read -r line || [ -n "$line" ]; do
  markdown_line=$(printf '%s' "$line" | sed -E 's/^[[:space:]]{0,3}//')

  if [ "$IN_FENCE" -eq 0 ] && [[ "$markdown_line" =~ ^${SPLIT_MARKER}($|[[:space:]]) ]]; then
    heading_text=${markdown_line#"$SPLIT_MARKER"}
    heading_text=$(trim_whitespace "$heading_text")
    heading_text=$(printf '%s' "$heading_text" | sed -E 's/[[:space:]]+#+[[:space:]]*$//')
    start_chunk "$heading_text"
  elif [ -z "$CURRENT_OUTPUT" ]; then
    start_chunk "$PREAMBLE_NAME"
  fi

  printf '%s\n' "$line" >> "$CURRENT_OUTPUT"

  fence_marker=""

  case "$markdown_line" in
    '```'*)
      fence_marker=$(printf '%s' "$markdown_line" | sed -E 's/^(```+).*$/\1/')
      ;;
    '~~~'*)
      fence_marker=$(printf '%s' "$markdown_line" | sed -E 's/^(~~~+).*$/\1/')
      ;;
  esac

  if [ -n "$fence_marker" ]; then
    fence_char=${fence_marker:0:1}
    fence_len=${#fence_marker}

    if [ "$IN_FENCE" -eq 0 ]; then
      IN_FENCE=1
      FENCE_CHAR="$fence_char"
      FENCE_LEN=$fence_len
    elif [ "$fence_char" = "$FENCE_CHAR" ] && [ "$fence_len" -ge "$FENCE_LEN" ]; then
      IN_FENCE=0
      FENCE_CHAR=""
      FENCE_LEN=0
    fi
  fi
done < "$INPUT_PATH"

if [ "$CREATED_FILES" -eq 0 ]; then
  echo "ERROR: no output files were created." >&2
  exit 1
fi

echo "Created $CREATED_FILES file(s) in: $OUTPUT_DIR"
