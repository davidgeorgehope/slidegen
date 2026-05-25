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


ARCHITECTURE_SPEC_SCHEMA: dict[str, Any] = {
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


GENERIC_SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "required": [
        "slide",
        "source_size",
        "layout",
        "logo_assets",
        "asset_queries",
        "assets",
    ],
    "properties": {
        "slide": {"type": "string"},
        "source_size": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
        },
        "layout": {"type": "string", "enum": ["generic_slide", "generic_deck"]},
        "theme": {"type": "object"},
        "fonts": {"type": "object"},
        "header": {"type": "object"},
        "split_reason": {"type": "string"},
        "slides": {"type": "array", "items": {"type": "object"}},
        "elements": {"type": "array", "items": {"type": "object"}},
        "logo_assets": {"type": "array", "items": {"type": "object"}},
        "asset_queries": {"type": "array", "items": {"type": "object"}},
        "assets": {"type": "object"},
    },
}


def schema_for_layout(layout: str) -> dict[str, Any]:
    if layout == "architecture_parallel_layers":
        return ARCHITECTURE_SPEC_SCHEMA
    if layout in {"generic_slide", "generic_deck"}:
        return GENERIC_SPEC_SCHEMA
    raise ValueError(f"Unsupported layout: {layout}")


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


def bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


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


def normalize_points(value: Any) -> list[int] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    result = []
    for item in value:
        try:
            result.append(max(0, int(round(float(item)))))
        except (TypeError, ValueError):
            return None
    return result


def normalize_logo_assets(items: Any) -> list[dict[str, str]]:
    logo_assets = []
    seen = set()
    if not isinstance(items, list):
        return logo_assets
    for item in items:
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
    return logo_assets


def normalize_asset_queries(items: Any) -> list[dict[str, Any]]:
    queries = []
    seen = set()
    if not isinstance(items, list):
        return queries
    for item in items:
        if not isinstance(item, dict):
            continue
        name = clean_name(str(item.get("name") or item.get("semantic_label") or item.get("anchor_text") or ""))
        if not name or name in seen:
            continue
        seen.add(name)
        query = dict(item)
        query["name"] = name
        icon_id = clean_name(str(query.get("icon_id") or query.get("canonical_icon") or name))
        if icon_id.endswith("_icon"):
            icon_id = icon_id[:-5]
        if icon_id:
            query["icon_id"] = icon_id
        icon_style = str(query.get("icon_style") or query.get("style") or "blue_line").strip().lower().replace("-", "_")
        if icon_style not in {"blue_line", "blue_fill", "white_line", "white_on_blue", "status_check", "status_error"}:
            icon_style = "blue_line"
        query["icon_style"] = icon_style
        if query.get("asset"):
            query["asset"] = clean_name(str(query["asset"]))
        if query.get("semantic_label"):
            query["semantic_label"] = str(query["semantic_label"]).strip()
        if query.get("anchor_text"):
            query["anchor_text"] = str(query["anchor_text"]).strip()
        query.pop("generation_prompt", None)
        query["generatable"] = bool_value(query.get("generatable", False))
        if query.get("crop_rule") not in {
            "nearest_icon_left",
            "nearest_icon_right",
            "nearest_icon_above",
            "nearest_icon_below",
            "text_box",
        }:
            query["crop_rule"] = "nearest_icon_left"
        if "bbox" in query:
            query["bbox"] = normalize_bbox(query.get("bbox"), [0, 0, 1, 1])
        queries.append(query)
    return queries


def normalize_generic_elements(items: Any, source_size: list[int]) -> list[dict[str, Any]]:
    elements = []
    if not isinstance(items, list):
        return elements

    full_box = [0, 0, source_size[0], source_size[1]]
    allowed_types = {"text", "shape", "line", "image", "icon"}
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        element = dict(item)
        element_type = str(element.get("type", "")).strip().lower()
        if element_type not in allowed_types:
            continue
        element["type"] = element_type
        element["z"] = int(element.get("z", idx)) if str(element.get("z", idx)).lstrip("-").isdigit() else idx

        if element_type == "line":
            points = normalize_points(element.get("points"))
            if points is None:
                continue
            element["points"] = points
            element["stroke_width"] = max(0.25, min(12.0, float(element.get("stroke_width", 1.0) or 1.0)))
            element["arrow"] = bool(element.get("arrow", False))
            element["dash"] = bool(element.get("dash", False))
        else:
            element["bbox"] = normalize_bbox(element.get("bbox"), full_box)

        if element_type == "text":
            element["text"] = str(element.get("text") or element.get("content") or element.get("label") or "")
            if not element["text"].strip():
                continue
            if "font_size" in element:
                try:
                    element["font_size"] = max(5, min(72, int(round(float(element["font_size"])))))
                except (TypeError, ValueError):
                    element.pop("font_size", None)
            element["bold"] = bool(element.get("bold", False))
            element["italic"] = bool(element.get("italic", False))
        elif element_type == "shape":
            shape = str(element.get("shape") or "rect").strip().lower().replace("-", "_")
            if shape in {"roundrect", "rounded_rect", "rounded_rectangle"}:
                shape = "round_rect"
            if shape not in {"rect", "round_rect", "ellipse"}:
                shape = "rect"
            element["shape"] = shape
            if "stroke_width" in element:
                try:
                    element["stroke_width"] = max(0.0, min(12.0, float(element["stroke_width"])))
                except (TypeError, ValueError):
                    element["stroke_width"] = 0.75
        elif element_type in {"image", "icon"}:
            if element.get("asset"):
                element["asset"] = clean_name(str(element["asset"]))
            if element_type == "icon":
                if element.get("name"):
                    element["name"] = clean_name(str(element["name"]))
                element["icon_hint"] = str(
                    element.get("icon_hint") or element.get("name") or element.get("semantic_label") or "generic"
                ).strip()

        elements.append(element)

    return sorted(elements, key=lambda element: element.get("z", 0))


def postprocess_spec(spec: dict[str, Any], image_path: Path, layout: str) -> dict[str, Any]:
    with Image.open(image_path) as im:
        source_size = [int(im.width), int(im.height)]

    spec["slide"] = image_path.stem
    spec["source_size"] = source_size
    requested_layout = layout
    actual_layout = str(spec.get("layout") or requested_layout)
    if requested_layout in {"generic_slide", "generic_deck"} and actual_layout == "generic_deck":
        spec["layout"] = "generic_deck"
    else:
        spec["layout"] = requested_layout
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
    spec["asset_queries"] = normalize_asset_queries(spec.get("asset_queries", []))
    spec["assets"] = {}

    spec["logo_assets"] = normalize_logo_assets(spec.get("logo_assets", []))

    if spec["layout"] == "generic_deck":
        raw_slides = spec.get("slides", [])
        if not isinstance(raw_slides, list):
            raw_slides = []
        slides = []
        raw_logo_assets: list[Any] = list(spec.get("logo_assets", []))
        raw_asset_queries: list[Any] = list(spec.get("asset_queries", []))
        for idx, child in enumerate(raw_slides, start=1):
            if not isinstance(child, dict):
                continue
            slide_spec = dict(child)
            slide_spec["slide"] = clean_name(str(slide_spec.get("slide") or f"{image_path.stem}_{idx:02d}"))
            slide_spec["source_size"] = source_size
            slide_spec["layout"] = "generic_slide"
            slide_spec["theme"] = dict(spec["theme"])
            slide_spec["fonts"] = dict(spec["fonts"])
            slide_spec["header"] = dict(spec["header"])
            slide_spec["elements"] = normalize_generic_elements(slide_spec.get("elements", []), source_size)
            raw_logo_assets.extend(slide_spec.get("logo_assets", []))
            raw_asset_queries.extend(slide_spec.get("asset_queries", []))
            slide_spec["logo_assets"] = []
            slide_spec["asset_queries"] = []
            slide_spec["assets"] = {}
            slides.append(slide_spec)
        spec["slides"] = slides
        spec["logo_assets"] = normalize_logo_assets(raw_logo_assets)
        spec["asset_queries"] = normalize_asset_queries(raw_asset_queries)
        validate_spec(spec)
        return spec

    if spec["layout"] == "generic_slide":
        spec["elements"] = normalize_generic_elements(spec.get("elements", []), source_size)
        validate_spec(spec)
        return spec

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
    common_required = [
        "slide",
        "source_size",
        "layout",
        "theme",
        "fonts",
        "header",
        "logo_assets",
        "asset_queries",
        "assets",
    ]
    missing = [key for key in common_required if key not in spec]
    if missing:
        raise ValueError(f"Generated spec missing required keys: {', '.join(missing)}")
    if not isinstance(spec["logo_assets"], list):
        raise ValueError("Generated logo_assets must be a list")
    if not isinstance(spec["asset_queries"], list):
        raise ValueError("Generated asset_queries must be a list")

    if spec["layout"] == "generic_slide":
        if not isinstance(spec.get("elements"), list) or not spec["elements"]:
            raise ValueError("Generated generic spec has no elements")
        for idx, element in enumerate(spec["elements"]):
            if element.get("type") == "line":
                if not element.get("points"):
                    raise ValueError(f"Generated generic line element {idx} has no points")
            elif not element.get("bbox"):
                raise ValueError(f"Generated generic element {idx} has no bbox")
        return

    if spec["layout"] == "generic_deck":
        if not isinstance(spec.get("slides"), list) or not spec["slides"]:
            raise ValueError("Generated generic deck has no child slides")
        for idx, child in enumerate(spec["slides"]):
            if not isinstance(child, dict):
                raise ValueError(f"Generated generic deck slide {idx} is not an object")
            if child.get("layout") != "generic_slide":
                raise ValueError(f"Generated generic deck slide {idx} is not a generic_slide")
            if not isinstance(child.get("elements"), list) or not child["elements"]:
                raise ValueError(f"Generated generic deck slide {idx} has no elements")
        return

    required = [
        "left_panel",
        "devices",
        "layers",
        "saas_panel",
        "parallel_layer",
        "callout",
    ]
    missing = [key for key in required if key not in spec]
    if missing:
        raise ValueError(f"Generated spec missing required keys: {', '.join(missing)}")
    if spec["layout"] != "architecture_parallel_layers":
        raise ValueError(f"Unsupported generated layout: {spec['layout']}")
    if not spec["layers"]:
        raise ValueError("Generated architecture spec has no layers")


def generate_spec(image_path: Path, *, model: str, layout: str, include_ocr: bool) -> dict[str, Any]:
    system_prompt = read_prompt("system.md")
    schema = schema_for_layout(layout)
    if layout == "architecture_parallel_layers":
        layout_prompt = read_prompt("architecture_parallel_layers.md")
    else:
        layout_prompt = read_prompt("generic_slide.md")
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
                "schema": schema,
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
    parser.add_argument("--layout", default="generic_slide")
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
