# slidegen

Convert slide screenshots or PNG exports into editable PowerPoint decks.

The project is intentionally spec-driven:

1. Read a source image.
2. Use OCR and/or an OpenAI vision model to draft a JSON slide spec.
3. Extract real source logos/assets from OCR anchors.
4. Generate only generic, text-free pictograms when allowed.
5. Render editable `.pptx` files with native text, shapes, connectors, and images.

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

## Current Supported Layout

The first generic renderer is:

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
│       └── architecture_parallel_layers.md
├── src/
│   ├── generate_spec_openai.py
│   ├── run_pipeline.py
│   ├── extract_assets_vision.py
│   ├── extract_logos_by_text.py
│   ├── extract_logos_vision.py
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
OPENAI_SPEC_MODEL=gpt-5.4-mini
```

Set `OPENAI_API_KEY` in that local `.env` file. `.env` is ignored by git.

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

1. Generate a JSON spec from the image and OCR context.
2. Extract declared logo assets from the source image.
3. Verify the generated spec and print non-fatal warnings.
4. Render an editable `.pptx`.

To rerender an existing spec without rerunning OCR or OpenAI calls:

```bash
.venv/bin/python src/run_pipeline.py \
  images/source.png \
  specs/source.auto.json \
  output/source.pptx \
  --skip-assets
```

## Prompts

Prompts are versioned repo artifacts:

- `prompts/spec_generation/system.md`
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
3. Generated assets only for generic, non-brand, text-free pictograms marked as generatable.

Image generation is not used to create full slides, charts, tables, real logos, or editable labels.

## Security Notes

The `.gitignore` is intentionally strict. Before publishing or pushing, run:

```bash
git status --short
git diff --cached --name-only
```

Do not commit local source images, generated outputs, extracted assets, templates, customer decks, or `.env`.
