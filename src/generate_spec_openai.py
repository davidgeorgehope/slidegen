"""Generate a slide JSON spec from a PNG using the OpenAI API.

The output is intentionally a spec, not a rendered slide. Rendering stays in
the deterministic layout-specific renderers.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any

from PIL import Image

try:
    from openai import OpenAI
except ImportError as exc:  # pragma: no cover - dependency error path
    raise SystemExit("openai package is required: .venv/bin/pip install openai") from exc


ROOT = Path(__file__).resolve().parents[1]
PROMPT_DIR = ROOT / "prompts" / "spec_generation"
DEFAULT_SPEC_MODEL = "gpt-5.5"
DEFAULT_THEME = {
    "bg_color": "F7FBFE",
    "panel_fill": "FFFFFF",
    "title_color": "051353",
    "accent_color": "285CDD",
    "body_color": "051353",
    "muted_color": "5A6A8A",
    "card_border": "D5E3EF",
    "green": "1C9B8A",
    "green_light": "F0FBF8",
    "purple": "7047D6",
    "purple_light": "F7F3FF",
    "blue_light": "EAF2FF",
}
DEFAULT_FONTS = {"title": "Helvetica Neue", "body": "Helvetica Neue"}
DEFAULT_HEADER = {
    "logo_image": "assets/brand_wordmark.png",
    "right_code": "01.01.26",
    "right_logo_placeholder": "[LOGO HERE]",
}


SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": [
        "slide",
        "source_size",
        "layout",
        "left_panel",
        "devices",
        "layers",
        "saas_panel",
        "parallel_layer",
        "callout",
        "logo_assets",
    ],
    "properties": {
        "slide": {"type": "string"},
        "source_size": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
        },
        "layout": {"type": "string", "enum": ["architecture_parallel_layers"]},
        "theme": {"type": "object"},
        "fonts": {"type": "object"},
        "header": {"type": "object"},
        "left_panel": {"type": "object"},
        "devices": {"type": "array", "items": {"type": "object"}},
        "layers": {"type": "array", "items": {"type": "object"}},
        "saas_panel": {"type": "object"},
        "parallel_layer": {"type": "object"},
        "callout": {"type": "object"},
        "logo_assets": {"type": "array", "items": {"type": "object"}},
        "asset_queries": {"type": "array", "items": {"type": "object"}},
        "assets": {"type": "object"},
    },
}


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def read_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text()


def image_data_url(image_path: Path) -> str:
    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def vision_ocr_context(image_path: Path) -> str:
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from extract_logos_vision import ocr_text

        boxes = ocr_text(image_path)
    except Exception as exc:  # noqa: BLE001 - OCR is a best-effort hint
        return f"OCR unavailable: {exc}"

    rows = []
    for box in sorted(boxes, key=lambda b: (b.bbox[1], b.bbox[0])):
        x, y, w, h = box.bbox
        rows.append(f"[{x},{y},{w},{h}] conf={box.confidence:.2f} text={box.text}")
    return "\n".join(rows)


def clean_name(value: str) -> str:
    chars = []
    previous_underscore = False
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
            previous_underscore = False
        elif not previous_underscore:
            chars.append("_")
            previous_underscore = True
    return "".join(chars).strip("_")


def normalize_bbox(value: Any, fallback: list[int]) -> list[int]:
    if not isinstance(value, list) or len(value) != 4:
        return fallback
    result = []
    for item in value:
        try:
            result.append(max(0, int(round(float(item)))))
        except (TypeError, ValueError):
            return fallback
    return result


def postprocess_spec(spec: dict[str, Any], image_path: Path, layout: str) -> dict[str, Any]:
    with Image.open(image_path) as im:
        source_size = [int(im.width), int(im.height)]

    spec["slide"] = image_path.stem
    spec["source_size"] = source_size
    spec["layout"] = layout
    model_header = spec.get("header", {}) if isinstance(spec.get("header"), dict) else {}
    spec["theme"] = dict(DEFAULT_THEME)
    spec["fonts"] = dict(DEFAULT_FONTS)
    spec["header"] = dict(DEFAULT_HEADER)
    if model_header.get("right_code") or model_header.get("date"):
        spec["header"]["right_code"] = str(model_header.get("right_code") or model_header.get("date"))
    if model_header.get("right_logo_placeholder") or model_header.get("placeholder"):
        spec["header"]["right_logo_placeholder"] = str(
            model_header.get("right_logo_placeholder") or model_header.get("placeholder")
        )
    spec.setdefault("asset_queries", [])
    spec["assets"] = {}

    logo_assets = []
    seen = set()
    for item in spec.get("logo_assets", []):
        if not isinstance(item, dict):
            continue
        match = str(item.get("match") or item.get("name") or "").strip()
        if not match:
            continue
        name = clean_name(str(item.get("name") or match))
        if not name or name in seen:
            continue
        seen.add(name)
        logo_assets.append({"name": name, "match": match})
    spec["logo_assets"] = logo_assets

    for layer in spec.get("layers", []):
        if not isinstance(layer, dict):
            continue
        layer["bbox"] = normalize_bbox(layer.get("bbox"), [577, 202, 774, 112])
        layer.setdefault("accent", "accent_color")
        layer["logos"] = [clean_name(str(name)) for name in layer.get("logos", []) if str(name).strip()]

    if isinstance(spec.get("saas_panel"), dict):
        spec["saas_panel"]["bbox"] = normalize_bbox(spec["saas_panel"].get("bbox"), [1455, 172, 183, 480])
        spec["saas_panel"]["logos"] = [
            clean_name(str(name)) for name in spec["saas_panel"].get("logos", []) if str(name).strip()
        ]
    if isinstance(spec.get("parallel_layer"), dict):
        spec["parallel_layer"]["bbox"] = normalize_bbox(spec["parallel_layer"].get("bbox"), [577, 679, 1060, 128])
    if isinstance(spec.get("callout"), dict):
        spec["callout"]["bbox"] = normalize_bbox(spec["callout"].get("bbox"), [381, 831, 1063, 84])

    validate_spec(spec)
    return spec


def validate_spec(spec: dict[str, Any]) -> None:
    required = [
        "slide",
        "source_size",
        "layout",
        "theme",
        "fonts",
        "header",
        "left_panel",
        "devices",
        "layers",
        "saas_panel",
        "parallel_layer",
        "callout",
        "logo_assets",
        "assets",
    ]
    missing = [key for key in required if key not in spec]
    if missing:
        raise ValueError(f"Generated spec missing required keys: {', '.join(missing)}")
    if spec["layout"] != "architecture_parallel_layers":
        raise ValueError(f"Unsupported generated layout: {spec['layout']}")
    if not spec["layers"]:
        raise ValueError("Generated architecture spec has no layers")
    if not isinstance(spec["logo_assets"], list):
        raise ValueError("Generated logo_assets must be a list")


def generate_spec(image_path: Path, *, model: str, layout: str, include_ocr: bool) -> dict[str, Any]:
    if layout != "architecture_parallel_layers":
        raise ValueError(f"Only architecture_parallel_layers is implemented today, got {layout}")

    system_prompt = read_prompt("system.md")
    layout_prompt = read_prompt("architecture_parallel_layers.md")
    ocr_context = vision_ocr_context(image_path) if include_ocr else "OCR omitted."
    user_prompt = (
        f"{layout_prompt}\n\n"
        f"Source image: {image_path.name}\n\n"
        "OCR boxes from the source image, in `[x,y,width,height]` pixels:\n"
        f"{ocr_context}\n\n"
        "Now produce the JSON spec for this source slide."
    )

    client = OpenAI()
    response = client.responses.create(
        model=model,
        instructions=system_prompt,
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_prompt},
                    {"type": "input_image", "image_url": image_data_url(image_path), "detail": "high"},
                ],
            }
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "slide_spec",
                "schema": SPEC_SCHEMA,
                "strict": False,
            },
            "verbosity": "low",
        },
        max_output_tokens=12000,
    )
    return postprocess_spec(json.loads(response.output_text), image_path, layout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("output_spec")
    parser.add_argument("--layout", default="architecture_parallel_layers")
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    load_env(ROOT / ".env")
    image_path = Path(args.image)
    out_path = Path(args.output_spec)
    if out_path.exists() and not args.force:
        raise SystemExit(f"{out_path} exists; pass --force to overwrite")

    model = args.model or os.environ.get("OPENAI_SPEC_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_SPEC_MODEL
    spec = generate_spec(image_path, model=model, layout=args.layout, include_ocr=not args.no_ocr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"wrote {out_path} using {model}")


if __name__ == "__main__":
    main()
