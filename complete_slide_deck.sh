#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON="${PYTHON:-.venv/bin/python}"
IMAGES_DIR="${IMAGES_DIR:-images}"
PATTERN="${PATTERN:-image*.png}"
OUTPUT_DIR="${OUTPUT_DIR:-output/complete_slide_deck}"
COMBINED="${COMBINED:-$OUTPUT_DIR/complete_slide_deck.pptx}"
SCRATCH_DIR="${SCRATCH_DIR:-/private/tmp/slidegen_complete_deck}"
ASSET_MODE="${ASSET_MODE:-generate}"
ICON_GENERATION_INPUT="${ICON_GENERATION_INPUT:-description}"
SPEC_LAYOUT="${SPEC_LAYOUT:-generic_slide}"
SPEC_REFINE="${SPEC_REFINE:-1}"
REFINE="${REFINE:-2}"
SPEC_MODEL="${SPEC_MODEL:-gpt-5.5}"
REFINE_MODEL="${REFINE_MODEL:-gpt-5.5}"
STYLE_GUIDE_IMAGE="${STYLE_GUIDE_IMAGE:-New Brand Mini Style Guide (1).png}"
TEMPLATE_PPTX="${TEMPLATE_PPTX:-Copy of Obsidian Template Deck 2026.pptx}"

if [[ ! -x "$PYTHON" ]]; then
  echo "Python not found or not executable: $PYTHON" >&2
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [[ ! -d "$IMAGES_DIR" ]]; then
  echo "Images directory not found: $IMAGES_DIR" >&2
  exit 1
fi

shopt -s nullglob
matched_images=("$IMAGES_DIR"/$PATTERN)
shopt -u nullglob
if (( ${#matched_images[@]} == 0 )); then
  echo "No slide images matched: $IMAGES_DIR/$PATTERN" >&2
  exit 1
fi

if [[ -z "${OPENAI_API_KEY:-}" ]] && ! grep -q '^OPENAI_API_KEY=' .env 2>/dev/null; then
  echo "OPENAI_API_KEY is not set in the shell or .env; final-quality generation will fail." >&2
  exit 1
fi

cmd=(
  "$PYTHON" "src/run_batch.py"
  "--images-dir" "$IMAGES_DIR"
  "--pattern" "$PATTERN"
  "--scratch-dir" "$SCRATCH_DIR"
  "--output-dir" "$OUTPUT_DIR"
  "--combined" "$COMBINED"
  "--asset-mode" "$ASSET_MODE"
  "--icon-generation-input" "$ICON_GENERATION_INPUT"
  "--spec-layout" "$SPEC_LAYOUT"
  "--spec-refine" "$SPEC_REFINE"
  "--refine" "$REFINE"
  "--spec-model" "$SPEC_MODEL"
  "--refine-model" "$REFINE_MODEL"
)

if [[ -f "$STYLE_GUIDE_IMAGE" ]]; then
  cmd+=("--style-guide-image" "$STYLE_GUIDE_IMAGE")
else
  echo "Style guide helper not found, continuing without it: $STYLE_GUIDE_IMAGE" >&2
fi

if [[ -f "$TEMPLATE_PPTX" ]]; then
  cmd+=("--template-pptx" "$TEMPLATE_PPTX")
else
  echo "Template helper not found, continuing without it: $TEMPLATE_PPTX" >&2
fi

cmd+=("$@")

dry_run=false
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    dry_run=true
  fi
done

echo "Running complete slide deck conversion for ${#matched_images[@]} image(s)."
echo "Output deck: $COMBINED"
echo "Scratch dir: $SCRATCH_DIR"
printf '+'
printf ' %q' "${cmd[@]}"
printf '\n'

"${cmd[@]}"

echo
if [[ "$dry_run" == true ]]; then
  echo "Dry run complete. No deck was written."
else
  echo "Complete deck written to: $COMBINED"
fi
