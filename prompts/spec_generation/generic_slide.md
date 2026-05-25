Create a `generic_slide` JSON spec.

This layout is the default fallback for any slide screenshot. It should be
usable across title slides, diagrams, grids, tables, timelines, callouts,
comparison slides, dense product slides, and architecture slides.

The renderer is deterministic and editable. Describe the slide as native
PowerPoint text, shapes, lines, images, and icons. Do not ask the renderer to
use the source screenshot as the slide background.

For normal source images, emit one `generic_slide`:

```json
{
  "slide": "source_slide",
  "source_size": [1672, 941],
  "layout": "generic_slide",
  "theme": {},
  "fonts": {},
  "header": {},
  "elements": [
    {
      "type": "shape",
      "shape": "round_rect",
      "bbox": [40, 90, 430, 120],
      "fill": "FFFFFF",
      "stroke": "D5E3EF",
      "stroke_width": 1,
      "radius": 0.12
    },
    {
      "type": "text",
      "text": "Visible slide copy",
      "bbox": [72, 120, 360, 42],
      "role": "body",
      "font_size": 18,
      "color": "051353",
      "bold": false,
      "align": "left"
    },
    {
      "type": "line",
      "points": [120, 220, 420, 220],
      "stroke": "285CDD",
      "stroke_width": 2,
      "dash": false,
      "arrow": false
    },
    {
      "type": "icon",
      "name": "secure_browser",
      "icon_hint": "secure browser session",
      "asset": "secure_browser_icon",
      "bbox": [92, 275, 54, 54]
    },
    {
      "type": "image",
      "asset": "workflow_screenshot",
      "bbox": [860, 170, 520, 320],
      "fit": "contain"
    }
  ],
  "logo_assets": [
    {"name": "crm_logo", "match": "CRM"}
  ],
  "asset_queries": [
    {
      "name": "secure_browser_icon",
      "icon_id": "secure_browser",
      "icon_style": "round source-colored line icon with a soft circular badge",
      "semantic_label": "secure browser session",
      "generatable": true,
      "anchor_text": "Browser session",
      "crop_rule": "nearest_icon_left",
      "bbox": [92, 275, 54, 54]
    }
  ],
  "assets": {}
}
```

For dense source images that would become cramped or unreadable as one editable
slide, split the source into a `generic_deck`:

```json
{
  "slide": "source_slide",
  "source_size": [1672, 941],
  "layout": "generic_deck",
  "split_reason": "The source contains a dense matrix and detailed side notes.",
  "slides": [
    {
      "slide": "source_slide_01_overview",
      "source_size": [1672, 941],
      "layout": "generic_slide",
      "title": "Overview",
      "elements": []
    },
    {
      "slide": "source_slide_02_details",
      "source_size": [1672, 941],
      "layout": "generic_slide",
      "title": "Details",
      "elements": []
    }
  ],
  "logo_assets": [],
  "asset_queries": [],
  "assets": {}
}
```

Field guidance:
- `bbox` arrays must be `[x, y, width, height]` in source image pixels.
- `points` arrays must be `[x1, y1, x2, y2]` in source image pixels.
- Use `generic_deck` when one source image has too much information for one
  clean slide. Split by natural sections: overview vs details, left vs right
  panels, top vs bottom flows, before vs after, or one major concept per slide.
- When splitting, preserve the important meaning and visible copy, but do not
  repeat every tiny detail on every generated slide. Each child slide should be
  readable and self-contained.
- A split should usually be 2-4 slides, not an unbounded decomposition.
- Use `elements` for all visible slide structure that should be editable.
- `font_size` should be the observed source-image pixel size, not PowerPoint
  points. The renderer converts source pixels into slide points.
- Use `type: "text"` for every readable word, number, title, caption, table cell,
  label, date, or callout. Never render slide text as an image.
- Use `type: "shape"` for cards, panels, chips, rows, columns, badges, table
  cells, containers, separators, and background bands.
- Use `type: "line"` for connectors, dividers, arrows, brackets, flow lines,
  table rules, and relationship lines.
- Use `type: "icon"` for generic pictograms. `icon_hint` is a free semantic
  phrase, not an enum. Examples: `identity governance`, `database scan`,
  `browser isolation`, `user group`, `policy engine`, `risk signal`,
  `SaaS connector`, `ticket workflow`, `audit evidence`, `device posture`.
- For generic non-brand icons, add an `asset_queries` item with
  `generatable: true`. Include a stable `icon_id`; the model should do semantic
  normalization, not the renderer. Reuse the same `icon_id` for the same
  concept across slides, for example `api_key`, `oauth_client`,
  `service_account`, `user_group`, `shield_check`, `document_search`, or
  `target_crosshair`.
- `icon_style` is optional free-form visual observation from the source image,
  not an enum. Use it only to describe what is visibly present, for example
  `round purple badge with blue key line art`, `thin navy outline icon without
  fill`, or `white pictogram inside a dark square`.
- Image generation is the primary path for generic icons when `OPENAI_API_KEY`
  exists; the asset pipeline first crops the source icon reference from `bbox`
  or `anchor_text`, then uses that source crop as the imagegen reference.
  Vision/source-crop output remains the fallback.
- Do not emit per-icon `generation_prompt` text. The asset pipeline builds a
  standardized prompt from the source reference crop, `icon_id`, optional
  `icon_style`, and `semantic_label` so the icon follows the source image.
- Do not generate real vendor, customer, product, or brand logos. Put those in
  `logo_assets` with visible OCR text in `match`.
- Use `type: "image"` only for non-editable source visual material that should
  remain raster, such as screenshots, photos, UI captures, or complex brand
  marks. Add an `asset_queries` item with a `bbox` for direct crop fallback.
- Color values can be six-digit hex strings with or without `#`, or keys from
  `theme`.
- Keep the element count practical. Capture important layout and copy first.
  For very dense source slides, prefer editable group structure and key text
  over tiny decorative details.
- Do not reconstruct cropped off-slide remnants, thumbnail scroll patterns, or
  partial artifacts at the image edge unless they carry semantic meaning.
- Leave `assets` empty; extraction/generation fills it later.
