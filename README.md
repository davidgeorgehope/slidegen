# slidegen

Convert slide screenshots or PNG exports into editable PowerPoint decks.

The project is intentionally spec-driven:

1. Read a source image.
2. Use Apple Vision OCR on macOS to collect text and bounding boxes.
3. Send the source image plus OCR context to an OpenAI vision model to draft a JSON slide spec.
4. Run a constrained source/spec structural QA pass for missing lines, simple
   shapes, and connectors.
5. Extract real source logos/assets from OCR anchors.
6. Generate or reuse canonical generic, text-free pictograms when allowed.
7. Render editable `.pptx` files with native text, shapes, connectors, and images.
8. Optionally run render-preview-refine loops that critique the PPTX screenshot
   and patch layout issues.
9. Run one bounded post-render structural QA pass when visual QA was enabled,
   so missing or shortened lines/connectors can be patched after seeing the
   rendered preview.

The source image is treated as evidence, not as the slide background.

## What This Publishes

This public repository contains code and prompts only.

It does not include:

- source slide images
- extracted assets or logo crops
- generated decks or previews
- private templates
- `.env` files or API keys
- customer/company-specific specs

## Current Supported Layouts

The default renderer is:

- `generic_slide`

This layout handles arbitrary editable slide rebuilds with native PowerPoint
text, shapes, lines, image crops, and semantic icons. Icon names are free-form
hints like `identity governance`, `database scan`, or `browser isolation`, not
a fixed enum.

Generated `font_size` values are interpreted as source-image pixel sizes. The
renderer converts them into PowerPoint points, caps them by text-box height and
width, and keeps related display-text fragments at a consistent size.

Dense screenshots can return:

- `generic_deck`

That lets one source image split into 2-4 readable output slides when a single
editable slide would be cramped. The child slides still use `generic_slide`
elements and share the same extracted/generated assets.

Legacy layout-specific renderers have been removed from the active pipeline.
Old specs such as `architecture_parallel_layers` are not supported; regenerate
them as `generic_slide` or `generic_deck`.

## Repo Layout

```text
slidegen/
├── prompts/
│   ├── spec_generation/
│   │   ├── system.md
│   │   └── generic_slide.md
│   └── refinement/
│       ├── spec_structure.md
│       └── visual_quality.md
├── src/
│   ├── generate_spec_openai.py
│   ├── refine_structure_openai.py
│   ├── refine_spec_openai.py
│   ├── render_preview.py
│   ├── run_batch.py
│   ├── run_pipeline.py
│   ├── extract_assets_vision.py
│   ├── extract_logos_by_text.py
│   ├── extract_logos_vision.py
│   ├── pptx_utils.py
│   ├── render_generic.py
│   └── verify_spec.py
├── requirements.txt
└── README.md
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

For OpenAI-backed spec generation, create a local `.env` file:

```bash
OPENAI_SPEC_MODEL=gpt-5.5
OPENAI_STRUCTURE_MODEL=gpt-5.5
OPENAI_REFINE_MODEL=gpt-5.5
OPENAI_ICON_DESCRIBE_MODEL=gpt-5.5
```

Set `OPENAI_API_KEY` in that local `.env` file. `.env` is ignored by git.

## Spec Generation Model

`gpt-5.5` is the default model for spec generation. You can override it per run:

```bash
.venv/bin/python src/run_pipeline.py \
  images/source.png \
  specs/source.auto.json \
  output/source.pptx \
  --spec-model gpt-5.5
```

The spec generator is not OpenAI OCR only. By default it uses Apple Vision OCR
locally when running on macOS, then sends both the image and the OCR boxes to
OpenAI. If Apple Vision is unavailable, the OpenAI call still receives the image
and the OCR context says it was unavailable. Use `--no-ocr` on
`src/generate_spec_openai.py` to intentionally skip local OCR.

## One-Command Flow

```bash
.venv/bin/python src/run_pipeline.py \
  images/source.png \
  specs/source.auto.json \
  output/source.pptx
```

This will:

1. Generate a JSON spec from the image and Apple Vision OCR context.
2. Run one source/spec structural QA pass by default to catch material missing
   connectors, lines, and simple shapes before asset extraction.
3. Extract declared logo assets from the source image.
4. Verify the generated spec and print non-fatal warnings.
5. Render an editable `.pptx`.
6. Run two preview/refinement iterations by default for visual-quality runs.

The default `--spec-layout` is `generic_slide`. The model may emit a
`generic_deck` when the source image is too dense for one readable slide.

For fast layout checks, pass `--refine 0`. For visual QA, keep the default
refinement loop or set the desired iteration count explicitly:

```bash
.venv/bin/python src/run_pipeline.py \
  images/source.png \
  specs/source.auto.json \
  output/source.pptx \
  --asset-mode generate \
  --refine 2
```

To skip structural QA during a fast run, pass:

```bash
--spec-refine 0
```

Generic icon generation defaults to description mode:

```bash
--icon-generation-input description
```

That mode first asks the OpenAI model to describe the cropped source icon, then
asks imagegen to create the icon from that description instead of editing the
crop. This has generally produced cleaner icon backgrounds while still treating
the source image as the visual authority. To force source-image edit mode, pass:

```bash
--icon-generation-input source
```

Source mode sends the cropped source icon to imagegen and asks it to recreate
the same visual treatment directly from the crop.

If the description model returns empty text after retries for a specific icon,
the asset step automatically uses source-image generation for that icon. This
still produces a generated icon asset; it does not allow source crops to appear
as final generic icons.

Each refinement iteration renders the PPTX, creates a Quick Look preview PNG,
sends the source image, rendered preview, spec, and deterministic lint hints to
OpenAI, then applies only validated JSON patch operations. The patch policy
favors moving/resizing text boxes and grouped font scaling over one-off tiny
font changes. The same refinement loop can also request a targeted
`regenerate_icon` patch for generic icon artwork problems. When that happens,
the pipeline removes the stale generated icon, passes the visual-QA guidance
back into icon generation, reruns only the affected assets, and renders the
next preview from the updated spec. Visual refinement can also add or adjust
simple connector lines when the screenshot reveals a structural miss that was
not caught before rendering. After visual QA, the pipeline runs one additional
structural pass against the rendered preview when `--spec-refine` is enabled;
that pass is limited to line and simple-shape operations.

Optional style helpers can be passed during spec generation:

```bash
.venv/bin/python src/run_pipeline.py \
  images/source.png \
  specs/source.auto.json \
  output/source.pptx \
  --style-guide-image path/to/brand-style-guide.png \
  --template-pptx path/to/template-deck.pptx
```

Helper images are reference-only. The source slide remains the authority for
copy and meaning; style guides and template previews can influence palette,
typography, spacing, card treatments, and icon treatment.

`run_pipeline.py` always regenerates and overwrites the spec path. Treat that
spec path as an output artifact, not an input dependency.

To rerender an existing spec without rerunning OCR or OpenAI calls:

```bash
.venv/bin/python src/render_generic.py specs/source.auto.json output/source.pptx
```

For clean-checkout-style testing, do not reuse repo-local specs, extracted
assets, or generated icon caches. Use `run_batch.py` with a fresh scratch
directory; it deletes that scratch directory at the start of the run unless
`--keep-scratch` is supplied.

If a long batch run is interrupted, rerun with `--resume` before deleting the
scratch directory. Resume mode preserves the scratch directory and skips slides
that already have both a generated spec and PPTX output:

```bash
./complete_slide_deck.sh --resume
```

## Full Deck Batch Flow

To run every `images/image*.png` through the pipeline and produce one combined
deck:

```bash
.venv/bin/python src/run_batch.py \
  --images-dir images \
  --spec-dir specs/auto \
  --output-dir output/auto \
  --combined output/all_slides_auto.pptx \
  --refine 2
```

For a fast layout-only QA pass without generated generic icons, add
`--refine 0 --skip-generic-assets`. In that mode real logos are still
extracted, but generic icons are drawn with editable placeholder PowerPoint
shapes. Do not use that mode for final visual-quality review.

Useful narrower runs:

```bash
.venv/bin/python src/run_batch.py \
  --start 23 \
  --end 25 \
  --scratch-dir /private/tmp/slidegen_clean_e2e \
  --output-dir output/clean_e2e \
  --combined output/clean_e2e/slides_23_25.pptx
.venv/bin/python src/run_batch.py \
  --limit 3 \
  --scratch-dir /private/tmp/slidegen_clean_e2e \
  --output-dir output/clean_e2e \
  --combined output/clean_e2e/first_3.pptx
.venv/bin/python src/run_batch.py \
  --slides 1,26-28 \
  --output-dir output/helper_tests \
  --combined output/helper_tests/helper_test_1_26_27_28.pptx \
  --scratch-dir /private/tmp/slidegen_helper_test \
  --asset-mode generate \
  --spec-model gpt-5.5 \
  --refine-model gpt-5.5 \
  --style-guide-image "New Brand Mini Style Guide (1).png" \
  --template-pptx "Copy of Obsidian Template Deck 2026.pptx"
```

## Prompts

Prompts are versioned repo artifacts:

- `prompts/spec_generation/system.md`
- `prompts/spec_generation/generic_slide.md`
- `prompts/refinement/spec_structure.md`
- `prompts/refinement/visual_quality.md`

The prompt contract is deliberately strict:

- preserve readable slide copy
- use source-image pixel coordinates
- do not recreate real logos with image generation
- do not generate slide text as raster art
- emit a JSON spec that deterministic renderers can consume
- refine the fresh spec with constrained structural patches for material
  missing lines, connectors, and simple shapes
- refine rendered PPTX previews with constrained JSON patches, not full
  free-form rewrites

## Asset Policy

Use this order:

1. Template/master assets for brand marks and recurring design assets.
2. Source crops or a logo library for real vendor/customer logos.
3. A source-reference generated icon library for generic, non-brand,
   text-free pictograms. The asset pipeline first crops the source icon from
   the image, then imagegen cleans/recreates that exact visual treatment.
   Cached generated icons are keyed by the source reference crop, not by a
   global style enum.
   The source crop also drives palette constraints. If an artificial chroma
   background contaminates the artwork, the asset step retries and may keep a
   source-matched matte when that better preserves the original visual
   treatment. Palette mismatches are warnings by default after retries so a
   single generated icon does not stop a full-deck run; set
   `SLIDEGEN_STRICT_ICON_PALETTE=1` to make those mismatches fatal during
   debugging.
   The refinement loop can request targeted icon regeneration when the preview
   shows a wrong subject, weak source match, unwanted matte/crop artifact, or
   inconsistent icon treatment. This is still source-image driven: guidance
   explains what to fix, while the source reference crop and inferred palette
   remain the visual authority.
   The default generation input is `description`: the asset step describes the
   source crop with an OpenAI vision model and then generates from that
   description. The explicit `source` input remains available for A/B checks
   where direct image editing is preferred.
4. Generic pictograms must not use source crops as final rendered assets. If a
   generated icon cannot be produced, the final-quality run fails before the
   renderer can substitute a native placeholder.
5. Native PowerPoint placeholder icons are only for explicit layout QA via
   `--skip-generic-assets`, not for final visual-quality review.

Image generation is not used to create full slides, charts, tables, real logos, or editable labels.
The asset code does not infer icon meaning from regexes or alias tables. The
spec LLM owns semantic normalization by emitting `icon_id`; if that field is
missing, the asset pipeline uses the asset name as the default identifier.
`icon_style` is free-form source-observed treatment text, not an enum. The
generic icon prompt is centralized in `src/extract_assets_vision.py`, but the
source crop is the visual authority.

## Security Notes

The `.gitignore` is intentionally strict. Before publishing or pushing, run:

```bash
git status --short
git diff --cached --name-only
```

Do not commit local source images, generated outputs, extracted assets, templates, customer decks, or `.env`.
