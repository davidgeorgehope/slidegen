"""Refine a generated slide spec by critiquing a rendered PPTX preview."""
from __future__ import annotations

import argparse
import base64
import copy
import json
import mimetypes
import os
import statistics
import sys
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except ImportError as exc:  # pragma: no cover - dependency error path
    raise SystemExit("openai package is required: .venv/bin/pip install openai") from exc


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "prompts" / "refinement" / "visual_quality.md"
DEFAULT_REFINE_MODEL = "gpt-5.5"

REFINEMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["quality_score", "issues", "patches"],
    "properties": {
        "quality_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "kind", "description", "paths"],
                "properties": {
                    "severity": {"type": "string", "enum": ["minor", "major", "critical"]},
                    "kind": {"type": "string"},
                    "description": {"type": "string"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "patches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "reason"],
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["set_bbox", "shift_bbox", "set_font_size", "scale_text_group"],
                    },
                    "path": {"type": "string"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "bbox": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                    "dx": {"type": "number"},
                    "dy": {"type": "number"},
                    "font_size": {"type": "number"},
                    "scale": {"type": "number"},
                    "reason": {"type": "string"},
                },
            },
        },
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


def image_data_url(image_path: Path) -> str:
    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def source_size(spec: dict[str, Any]) -> tuple[int, int]:
    size = spec.get("source_size")
    if isinstance(size, list) and len(size) == 2:
        try:
            return max(1, int(size[0])), max(1, int(size[1]))
        except (TypeError, ValueError):
            pass
    return (1672, 941)


def text_preview(text: Any, limit: int = 90) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "..."


def iter_elements(spec: dict[str, Any]):
    if spec.get("layout") == "generic_deck":
        for slide_idx, slide in enumerate(spec.get("slides", [])):
            if not isinstance(slide, dict):
                continue
            for element_idx, element in enumerate(slide.get("elements", [])):
                if isinstance(element, dict):
                    yield f"/slides/{slide_idx}/elements/{element_idx}", element
        return

    for element_idx, element in enumerate(spec.get("elements", [])):
        if isinstance(element, dict):
            yield f"/elements/{element_idx}", element


def element_catalog(spec: dict[str, Any]) -> str:
    rows = []
    for path, element in iter_elements(spec):
        row = {
            "path": path,
            "type": element.get("type"),
            "bbox": element.get("bbox"),
            "role": element.get("role"),
            "font_size": element.get("font_size"),
            "text": text_preview(element.get("text")),
            "z": element.get("z"),
        }
        rows.append(json.dumps({k: v for k, v in row.items() if v not in (None, "", [])}, separators=(",", ":")))
    return "\n".join(rows)


def area(bbox: list[int]) -> float:
    return max(0, bbox[2]) * max(0, bbox[3])


def intersection(a: list[int], b: list[int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    h = max(0, min(ay + ah, by + bh) - max(ay, by))
    return float(w * h)


def lint_spec(spec: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    source_w, source_h = source_size(spec)
    text_items = [
        (path, element)
        for path, element in iter_elements(spec)
        if element.get("type") == "text" and isinstance(element.get("bbox"), list)
    ]

    for path, element in text_items:
        x, y, w, h = [int(v) for v in element["bbox"]]
        if x < 0 or y < 0 or x + w > source_w or y + h > source_h:
            issues.append(f"{path}: text bbox is partly off-slide: {element['bbox']}")
        if w < 16 or h < 10:
            issues.append(f"{path}: text bbox is very small: {element['bbox']}")

    for idx, (path_a, element_a) in enumerate(text_items):
        bbox_a = element_a["bbox"]
        for path_b, element_b in text_items[idx + 1 :]:
            bbox_b = element_b["bbox"]
            overlap = intersection(bbox_a, bbox_b)
            if overlap <= 0:
                continue
            smaller = max(1.0, min(area(bbox_a), area(bbox_b)))
            if overlap / smaller >= 0.18:
                issues.append(
                    f"{path_a} and {path_b}: text bboxes overlap by {overlap / smaller:.0%}; "
                    f"text=`{text_preview(element_a.get('text'), 45)}` / `{text_preview(element_b.get('text'), 45)}`"
                )

    groups: dict[tuple[str, int], list[tuple[str, dict[str, Any]]]] = {}
    for path, element in text_items:
        role = str(element.get("role") or "body").lower()
        bbox = element["bbox"]
        key = (role, int(bbox[1] // max(1, source_h * 0.16)))
        groups.setdefault(key, []).append((path, element))
    for (role, _row), items in groups.items():
        sizes = []
        for _path, element in items:
            try:
                sizes.append(float(element.get("font_size")))
            except (TypeError, ValueError):
                pass
        if len(sizes) < 3:
            continue
        median = statistics.median(sizes)
        if median <= 0:
            continue
        largest = max(sizes)
        smallest = min(sizes)
        if largest / max(1.0, smallest) >= 1.45:
            paths = ", ".join(path for path, _element in items[:8])
            issues.append(f"{role} peer group has large font variance ({smallest:g}-{largest:g}, median {median:g}): {paths}")

    return issues[:30]


def parse_pointer(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError("path must be a JSON pointer")
    parts = path.strip("/").split("/")
    return [part.replace("~1", "/").replace("~0", "~") for part in parts]


def resolve_element(spec: dict[str, Any], path: str) -> dict[str, Any]:
    parts = parse_pointer(path)
    if len(parts) == 2 and parts[0] == "elements":
        return spec["elements"][int(parts[1])]
    if len(parts) == 4 and parts[0] == "slides" and parts[2] == "elements":
        return spec["slides"][int(parts[1])]["elements"][int(parts[3])]
    raise ValueError(f"unsupported element path: {path}")


def clamp_bbox(raw_bbox: Any, spec: dict[str, Any]) -> list[int]:
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        raise ValueError("bbox must be [x,y,width,height]")
    source_w, source_h = source_size(spec)
    x, y, w, h = [int(round(float(value))) for value in raw_bbox]
    w = max(4, min(source_w, w))
    h = max(4, min(source_h, h))
    x = max(0, min(source_w - w, x))
    y = max(0, min(source_h - h, y))
    return [x, y, w, h]


def role_min_font(element: dict[str, Any]) -> float:
    role = str(element.get("role") or "body").lower()
    if role in {"title", "headline", "h1"}:
        return 22.0
    if role in {"subtitle", "section", "h2", "heading", "section_heading", "section_title"}:
        return 16.0
    if role in {"caption", "eyebrow", "footer", "label"}:
        return 8.0
    return 11.0


def current_font_size(element: dict[str, Any]) -> float:
    try:
        return float(element.get("font_size"))
    except (TypeError, ValueError):
        bbox = element.get("bbox") if isinstance(element.get("bbox"), list) else [0, 0, 100, 30]
        role = str(element.get("role") or "body").lower()
        if role in {"title", "headline", "h1"}:
            return max(28.0, min(72.0, float(bbox[3]) * 0.42))
        if role in {"subtitle", "section", "h2", "heading"}:
            return max(18.0, min(42.0, float(bbox[3]) * 0.34))
        if role in {"caption", "eyebrow", "footer", "label"}:
            return max(8.0, min(20.0, float(bbox[3]) * 0.34))
        return max(11.0, min(30.0, float(bbox[3]) * 0.32))


def clamp_font_size(raw_size: Any, element: dict[str, Any]) -> int:
    requested = float(raw_size)
    old_size = current_font_size(element)
    lower = max(role_min_font(element), old_size * 0.75)
    upper = min(96.0, old_size * 1.12)
    return int(round(max(lower, min(upper, requested))))


def apply_patch(spec: dict[str, Any], patch: dict[str, Any]) -> str:
    op = patch.get("op")
    if op == "set_bbox":
        element = resolve_element(spec, str(patch.get("path") or ""))
        if "bbox" not in element:
            raise ValueError("target element has no bbox")
        element["bbox"] = clamp_bbox(patch.get("bbox"), spec)
        return f"set_bbox {patch.get('path')}"

    if op == "shift_bbox":
        element = resolve_element(spec, str(patch.get("path") or ""))
        if "bbox" not in element:
            raise ValueError("target element has no bbox")
        source_w, source_h = source_size(spec)
        dx = max(-source_w * 0.12, min(source_w * 0.12, float(patch.get("dx") or 0)))
        dy = max(-source_h * 0.12, min(source_h * 0.12, float(patch.get("dy") or 0)))
        x, y, w, h = element["bbox"]
        element["bbox"] = clamp_bbox([x + dx, y + dy, w, h], spec)
        return f"shift_bbox {patch.get('path')}"

    if op == "set_font_size":
        element = resolve_element(spec, str(patch.get("path") or ""))
        if element.get("type") != "text":
            raise ValueError("font target is not text")
        element["font_size"] = clamp_font_size(patch.get("font_size"), element)
        return f"set_font_size {patch.get('path')}"

    if op == "scale_text_group":
        paths = patch.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("scale_text_group requires paths")
        scale = max(0.78, min(1.08, float(patch.get("scale") or 1.0)))
        applied = 0
        for path in paths:
            element = resolve_element(spec, str(path))
            if element.get("type") != "text":
                continue
            element["font_size"] = clamp_font_size(current_font_size(element) * scale, element)
            applied += 1
        if applied == 0:
            raise ValueError("scale_text_group did not target text")
        return f"scale_text_group {applied} elements"

    raise ValueError(f"unsupported op: {op}")


def apply_refinement(original: dict[str, Any], response: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    refined = copy.deepcopy(original)
    applied: list[str] = []
    rejected: list[str] = []
    for patch in response.get("patches", []):
        if not isinstance(patch, dict):
            continue
        try:
            applied.append(apply_patch(refined, patch))
        except Exception as exc:  # noqa: BLE001 - bad model patch should not kill the run
            rejected.append(f"{patch.get('op', '<unknown>')}: {exc}")

    sys.path.insert(0, str(ROOT / "src"))
    from generate_spec_openai import validate_spec

    validate_spec(refined)
    return refined, applied, rejected


def request_refinement(
    source_image: Path,
    rendered_preview: Path,
    spec: dict[str, Any],
    *,
    model: str,
) -> dict[str, Any]:
    prompt = PROMPT_PATH.read_text()
    lint = lint_spec(spec)
    user_text = (
        f"{prompt}\n\n"
        "Element catalog. Use only these JSON pointer paths in patches:\n"
        f"{element_catalog(spec)}\n\n"
        "Deterministic lint hints. Treat these as hints, not guaranteed errors:\n"
        f"{json.dumps(lint, indent=2)}\n\n"
        "Current spec JSON:\n"
        f"{json.dumps(spec, indent=2)}\n\n"
        "Return a quality review and constrained patch list. If the slide is already acceptable, return no patches."
    )

    client = OpenAI()
    response = client.responses.create(
        model=model,
        instructions="You are a strict visual QA pass for editable slide reconstruction. Return only JSON.",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_text},
                    {"type": "input_text", "text": "Original source slide image:"},
                    {"type": "input_image", "image_url": image_data_url(source_image), "detail": "high"},
                    {"type": "input_text", "text": "Rendered editable PPTX preview:"},
                    {"type": "input_image", "image_url": image_data_url(rendered_preview), "detail": "high"},
                ],
            }
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "slide_refinement",
                "schema": REFINEMENT_SCHEMA,
                "strict": False,
            },
            "verbosity": "low",
        },
        max_output_tokens=8000,
    )
    return json.loads(response.output_text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_image")
    parser.add_argument("spec")
    parser.add_argument("rendered_preview")
    parser.add_argument("output_spec")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    load_env(ROOT / ".env")
    if not os.environ.get("OPENAI_API_KEY"):
        output = Path(args.output_spec)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(Path(args.spec).read_text())
        print("refinement skipped: OPENAI_API_KEY is not set")
        return

    spec_path = Path(args.spec)
    spec = json.loads(spec_path.read_text())
    if spec.get("layout") not in {"generic_slide", "generic_deck"}:
        output = Path(args.output_spec)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(spec, indent=2) + "\n")
        print(f"refinement skipped: unsupported layout {spec.get('layout')}")
        return

    model = args.model or os.environ.get("OPENAI_REFINE_MODEL") or os.environ.get("OPENAI_SPEC_MODEL") or DEFAULT_REFINE_MODEL
    response = request_refinement(Path(args.source_image), Path(args.rendered_preview), spec, model=model)
    refined, applied, rejected = apply_refinement(spec, response)

    output_path = Path(args.output_spec)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(refined, indent=2) + "\n")

    print(f"refinement quality_score={response.get('quality_score')} model={model}")
    for issue in response.get("issues", [])[:8]:
        print(f"- {issue.get('severity')}: {issue.get('kind')}: {issue.get('description')}")
    if applied:
        print("applied patches:")
        for item in applied:
            print(f"- {item}")
    else:
        print("applied patches: none")
    if rejected:
        print("rejected unsafe patches:")
        for item in rejected:
            print(f"- {item}")


if __name__ == "__main__":
    main()
