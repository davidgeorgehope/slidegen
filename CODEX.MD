# Codex CLI Provider Plan

## Goal

Reduce direct OpenAI API usage for local slide-generation experiments by adding
Codex CLI provider options, while preserving the current high-quality API path.

The source image remains the visual authority. The pipeline should still produce
fresh specs, generated generic icons, rendered PPTX files, visual QA refinement,
and structural QA refinement without manual fixes.

## Current State

- Spec generation, structural refinement, visual refinement, and icon
  description use the OpenAI SDK directly.
- Generic icon bitmap generation uses the imagegen CLI path with explicit
  `gpt-image-2`.
- A small probe showed `codex exec` can create a real generated PNG through its
  built-in image-generation capability.
- The Codex CLI image path appears to use the hosted image-generation tool, but
  the exact image model is not pinned by our code. Treat it as a provider, not
  as a guaranteed explicit `gpt-image-2` call.

## Non-Goals

- Do not replace the explicit OpenAI API path.
- Do not generate full slides as raster images.
- Do not use cropped generic icons as final output.
- Do not commit source images, templates, style guides, outputs, specs,
  extracted assets, generated icon caches, or secrets.
- Do not add manual repair steps. Any successful fix must be codified.

## Provider Split

Add two independent provider controls:

```text
--llm-provider openai|codex-cli
--image-provider openai|codex-cli
```

Environment fallbacks:

```text
SLIDEGEN_LLM_PROVIDER=openai
SLIDEGEN_IMAGE_PROVIDER=openai
```

Keep `openai` as the default until the Codex CLI path has enough clean-checkout
test coverage. Then local experiment commands can opt into Codex CLI explicitly.

## LLM Provider Interface

Use this for JSON/spec tasks:

- `generate_spec_openai.py`
- `refine_structure_openai.py`
- `refine_spec_openai.py`
- `describe_icon_reference()` inside `extract_assets_vision.py`

Target abstraction:

```text
run_json_task(
  task_name,
  prompt_text,
  image_paths,
  output_schema,
  model_hint,
  output_path
) -> dict
```

OpenAI implementation:

- Current Responses API behavior.
- Uses explicit model config such as `OPENAI_SPEC_MODEL`,
  `OPENAI_STRUCTURE_MODEL`, and `OPENAI_REFINE_MODEL`.

Codex CLI implementation:

- Calls `codex exec` non-interactively.
- Uses a temporary working directory under `/private/tmp/slidegen_codex`.
- Supplies prompt text, image paths, and schema/output contract.
- Requires strict JSON output validation before the pipeline accepts the result.
- Fails loudly on invalid JSON, missing fields, or unsupported operations.

Do not let Codex CLI rewrite arbitrary repo files. It should write only the
declared output JSON file inside the temp job directory, then the parent process
copies validated output to the real spec path.

## Image Provider Interface

Use this for generic icon bitmap generation only.

Target abstraction:

```text
generate_icon_bitmap(
  prompt,
  source_reference_path,
  output_path,
  size,
  quality,
  mode
) -> ImageGenerationResult
```

OpenAI implementation:

- Current imagegen CLI path.
- Explicit `gpt-image-2`.
- Supports current `description` and `source` icon-generation modes.
- Keeps existing chroma-key cleanup, padding normalization, palette checks,
  library caching, and regeneration loop.

Codex CLI implementation:

- Calls `codex exec` with an explicit instruction to generate a native bitmap
  and save it at the requested output path.
- For description mode, pass the icon prompt directly.
- For source mode, pass the source crop as an image reference if practical.
- The prompt must forbid placeholders, PIL/canvas drawings, SVG stand-ins, text,
  logos, and fake success.
- Validate the result with `PIL.Image.open`, dimensions, non-empty alpha/RGB
  content, and existing palette/coverage checks.
- If no PNG is produced, fail the asset step. Do not fall back to cropped icons.

## CLI Shape

Example final-quality API run:

```bash
.venv/bin/python src/run_batch.py \
  --slides 1,26-28 \
  --scratch-dir /private/tmp/slidegen_clean_e2e \
  --output-dir output/clean_e2e \
  --combined output/clean_e2e/slides_1_26_27_28.pptx \
  --asset-mode generate \
  --llm-provider openai \
  --image-provider openai
```

Example low-direct-API local experiment:

```bash
.venv/bin/python src/run_batch.py \
  --slides 1,26-28 \
  --scratch-dir /private/tmp/slidegen_codex_e2e \
  --output-dir output/codex_e2e \
  --combined output/codex_e2e/slides_1_26_27_28.pptx \
  --asset-mode generate \
  --llm-provider codex-cli \
  --image-provider codex-cli
```

Mixed mode should also work:

```bash
--llm-provider codex-cli --image-provider openai
--llm-provider openai --image-provider codex-cli
```

## Rollout Steps

1. Add provider flags to `run_pipeline.py` and `run_batch.py`.
2. Add a small provider module for validated JSON tasks.
3. Move direct OpenAI JSON calls behind the provider module without changing
   prompts or schemas.
4. Add Codex CLI JSON provider and test it on one small spec/refinement task.
5. Add image-provider selection inside `extract_assets_vision.py`.
6. Keep current OpenAI icon generation untouched as the default provider.
7. Add Codex CLI icon generation using the same generated-icon cache and
   validation path.
8. Run clean-checkout-style tests for slides `1,26,27,28`, then `23`.
9. Compare API vs Codex CLI outputs visually and keep both artifacts in
   `output/` for review, not in git.
10. Only consider changing defaults after repeated clean runs look comparable.

## Validation Gates

Every provider must pass the same pipeline checks:

- JSON schema validation.
- Spec verifier warnings reviewed.
- No unsupported legacy layouts.
- No missing referenced assets.
- No cropped generic icons in final-quality output.
- Generated icon file exists and is a valid image.
- Generated icon passes padding, palette, and basic artwork coverage checks.
- Rendered PPTX preview is created.
- Visual refinement loop can still request icon regeneration.
- Structural refinement can still add or adjust missing lines/shapes.

## Security Notes

- Never pass `.env` contents to Codex CLI prompts.
- Never commit generated outputs, source images, helper assets, template decks,
  specs, extracted assets, icon caches, or API keys.
- Keep Codex CLI temp work under `/private/tmp`.
- Ensure provider logs do not print secrets.
- Keep `.gitignore` strict.

## Open Questions

- Can Codex CLI reliably accept local image references for source-mode icon
  generation in non-interactive runs?
- Can Codex CLI produce consistent enough icon backgrounds for batch use?
- Is Codex CLI cheaper in the user's actual account quota model, or does it
  simply move cost from API billing to Codex usage limits?
- Should the Codex CLI image path be limited to description mode first, since
  that already looked better for several slides?

## Recommended First Implementation

Start with `--image-provider codex-cli` for description-mode icon generation
only. That gives the highest chance of saving direct image API cost while
touching the smallest part of the working pipeline.

After that, add `--llm-provider codex-cli` for structural and visual refinement.
Spec generation should move last because bad initial specs create the most
downstream noise.
