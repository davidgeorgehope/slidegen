# slidegen

Convert slide screenshots or PNG exports into editable PowerPoint decks.

The project is intentionally spec-driven:

1. Read a source image.
2. Use Apple Vision OCR on macOS to collect text and bounding boxes.
3. Send the source image plus OCR context to an OpenAI vision model to draft a JSON slide spec.
4. Extract real source logos/assets from OCR anchors.
5. Generate or reuse canonical generic, text-free pictograms when allowed.
6. Render editable `.pptx` files with native text, shapes, connectors, and images.

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

There is also one specialized renderer:

- `architecture_parallel_layers`

This layout handles architecture diagrams with:

- a left narrative/sidebar
- top user/device row
- horizontal stack/layer boxes
- source-cropped vendor/app logos
- a parallel platform/product layer
- connector lines
- a footer/callout

More layouts can be added by creating a prompt file and renderer pair.

## Repo Layout

```text
slidegen/
├── prompts/
│   └── spec_generation/
│       ├── system.md
│       ├── generic_slide.md
│       └── architecture_parallel_layers.md
├── src/
│   ├── generate_spec_openai.py
│   ├── run_batch.py
│   ├── run_pipeline.py
│   ├── extract_assets_vision.py
│   ├── extract_logos_by_text.py
│   ├── extract_logos_vision.py
│   ├── pptx_utils.py
│   ├── render_generic.py
│   ├── render_architecture.py
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
```

Set `OPENAI_API_KEY` in that local `.env` file. `.env` is ignored by git.

## Spec Generation Model

`gpt-5.5` is the default model for spec generation. You can override it per run:

```bash
.venv/bin/python src/run_pipeline.py \
  images/source.png \
  specs/source.auto.json \
  output/source.pptx \
  --generate-spec \
  --force-spec \
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
  output/source.pptx \
  --generate-spec \
  --force-spec
```

This will:

1. Generate a JSON spec from the image and Apple Vision OCR context.
2. Extract declared logo assets from the source image.
3. Verify the generated spec and print non-fatal warnings.
4. Render an editable `.pptx`.

The default `--spec-layout` is `generic_slide`. The model may emit a
`generic_deck` when the source image is too dense for one readable slide.

To rerender an existing spec without rerunning OCR or OpenAI calls:

```bash
.venv/bin/python src/run_pipeline.py \
  images/source.png \
  specs/source.auto.json \
  output/source.pptx \
  --skip-assets
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
  --force-spec
```

For a fast layout-QA pass without generated/cropped generic icons, add
`--skip-generic-assets`. In that mode real logos are still extracted, but
generic icons are drawn with editable placeholder PowerPoint shapes.

Useful narrower runs:

```bash
.venv/bin/python src/run_batch.py --start 23 --end 25 --force-spec
.venv/bin/python src/run_batch.py --limit 3 --force-spec
```

## Prompts

Prompts are versioned repo artifacts:

- `prompts/spec_generation/system.md`
- `prompts/spec_generation/generic_slide.md`
- `prompts/spec_generation/architecture_parallel_layers.md`

The prompt contract is deliberately strict:

- preserve readable slide copy
- use source-image pixel coordinates
- do not recreate real logos with image generation
- do not generate slide text as raster art
- emit a JSON spec that deterministic renderers can consume

## Asset Policy

Use this order:

1. Template/master assets for brand marks and recurring design assets.
2. Source crops or a logo library for real vendor/customer logos.
3. A generated icon library for generic, non-brand, text-free pictograms. The
   spec LLM emits stable `icon_id` and `icon_style` fields; the asset pipeline
   generates each concept/style once and reuses it across slides. Existing
   library icons can be reused without an API key.
4. Vision/OpenCV source crops as fallback for generic pictograms when generation
   is unavailable, disabled, or fails. Crop quality checks reject obviously bad
   icon crops.
5. Native PowerPoint placeholder icons only for layout QA or missing assets.

Image generation is not used to create full slides, charts, tables, real logos, or editable labels.
The asset code does not infer icon meaning from regexes or alias tables. The
spec LLM owns semantic normalization by emitting `icon_id`; if that field is
missing, the asset pipeline only falls back to the asset name. The generic icon
generation prompt is centralized in `src/extract_assets_vision.py` so repeated
concepts stay visually consistent across slides.

## Security Notes

The `.gitignore` is intentionally strict. Before publishing or pushing, run:

```bash
git status --short
git diff --cached --name-only
```

Do not commit local source images, generated outputs, extracted assets, templates, customer decks, or `.env`.
