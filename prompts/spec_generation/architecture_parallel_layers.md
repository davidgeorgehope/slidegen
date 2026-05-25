Create an `architecture_parallel_layers` JSON spec.

This layout is for slides that show:
- a left narrative/title/sidebar
- one or more horizontal stack/layer boxes in the center
- users/devices entering from the top
- SaaS/application/vendor logos on the right or inside layers
- a product/platform layer running in parallel near the bottom
- connector arrows or dotted relationship lines
- a footer/callout

Use this structure:

```json
{
  "slide": "source_slide",
  "source_size": [1672, 941],
  "layout": "architecture_parallel_layers",
  "theme": {},
  "fonts": {},
  "header": {},
  "left_panel": {
    "eyebrow": "REFERENCE ARCHITECTURE",
    "title_lines": ["Where each layer", "lives-and where", "the platform fits."],
    "body": "The modern enterprise stack is layered,\nbut the platform sits parallel-not downstream.",
    "steps": [
      {"name": "EDGE SECURITY", "body": "...", "icon": "globe"}
    ]
  },
  "devices": [
    {"label": "Managed\nDevice", "icon": "laptop"}
  ],
  "layers": [
    {
      "name": "EDGE SECURITY LAYER",
      "body": "Traffic control,\ninspection, policy",
      "bbox": [577, 202, 774, 112],
      "accent": "green",
      "middle_label": "",
      "right_label": "",
      "left_caption": "",
      "right_caption": "",
      "logos": ["network_gateway", "access_proxy"]
    }
  ],
  "saas_panel": {
    "title": "APPLICATIONS",
    "bbox": [1455, 172, 183, 480],
    "logos": ["productivity_suite", "crm_app"],
    "footer": "...and more"
  },
  "parallel_layer": {
    "title": "PLATFORM LAYER (PARALLEL)",
    "body": "App-native data + browser telemetry\ndirect from each SaaS application.",
    "bbox": [577, 679, 1060, 128],
    "capabilities": [
      {"title": "App-native\nConnectors", "icon": "connector"}
    ]
  },
  "callout": {
    "title": "The platform is parallel-not a feed off the SIEM.",
    "body": "We don't wait for logs to be forwarded.",
    "bbox": [381, 831, 1063, 84]
  },
  "logo_assets": [
    {"name": "network_gateway", "match": "Network Gateway"}
  ],
  "asset_queries": [],
  "assets": {}
}
```

Field guidance:
- `bbox` arrays must be `[x, y, width, height]` in source image pixels.
- Icon fields are semantic hints, not strict enums. Prefer familiar values like
  `globe`, `lock`, `shield`, `target`, `laptop`, `phone`, `users`, `connector`,
  `sync`, or `browser` when they fit, but use a better free-form phrase when
  the source slide calls for it.
- `layers[*].accent` should be one of `green`, `accent_color`, `purple` when
  possible; use another theme key or hex color only when visually necessary.
- Logo `name` values must be lowercase snake_case.
- Logo `match` values should be the visible OCR/vendor text to anchor the crop.
- Put every real source logo referenced by `layers`, `saas_panel`, or the parallel layer into `logo_assets`.
- Leave `assets` empty; extraction fills it later.
