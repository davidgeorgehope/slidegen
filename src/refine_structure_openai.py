"""Refine a generated spec for missing structural slide objects."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
except ImportError as exc:  # pragma: no cover - dependency error path
    raise SystemExit("openai package is required: .venv/bin/pip install openai") from exc

from generate_spec_openai import DEFAULT_SPEC_MODEL, validate_spec, vision_ocr_context
from refine_spec_openai import (
    REFINEMENT_SCHEMA,
    apply_refinement,
    element_catalog,
    image_data_url,
    lint_spec,
    load_env,
)


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "prompts" / "refinement" / "spec_structure.md"
STRUCTURAL_OPS = {"set_line_points", "add_line", "add_shape"}


def keep_structural_patches(response: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    filtered = dict(response)
    patches = []
    rejected = []
    for patch in response.get("patches", []):
        if not isinstance(patch, dict):
            continue
        op = str(patch.get("op") or "")
        if op in STRUCTURAL_OPS:
            patches.append(patch)
        else:
            rejected.append(f"{op or '<unknown>'}: structural refinement only allows line/shape operations")
    filtered["patches"] = patches
    return filtered, rejected


def request_structure_refinement(
    source_image: Path,
    spec: dict[str, Any],
    *,
    model: str,
    rendered_preview: Path | None = None,
) -> dict[str, Any]:
    prompt = PROMPT_PATH.read_text()
    ocr_context = vision_ocr_context(source_image)
    lint = lint_spec(spec)
    user_text = (
        f"{prompt}\n\n"
        "Element catalog. Use only these JSON pointer paths for existing elements:\n"
        f"{element_catalog(spec)}\n\n"
        "OCR boxes from the source image, in `[x,y,width,height]` pixels:\n"
        f"{ocr_context}\n\n"
        "Deterministic lint hints. Treat these as hints, not guaranteed errors:\n"
        f"{json.dumps(lint, indent=2)}\n\n"
        "Current spec JSON:\n"
        f"{json.dumps(spec, indent=2)}\n\n"
        "Return a structural quality review and constrained patch list. "
        "If the spec already captures the material structure, return no patches."
    )

    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": user_text},
        {"type": "input_text", "text": "Original source slide image:"},
        {"type": "input_image", "image_url": image_data_url(source_image), "detail": "high"},
    ]
    if rendered_preview is not None:
        content.extend(
            [
                {"type": "input_text", "text": "Rendered editable PPTX preview, for structural comparison only:"},
                {"type": "input_image", "image_url": image_data_url(rendered_preview), "detail": "high"},
            ]
        )

    client = OpenAI()
    response = client.responses.create(
        model=model,
        instructions="You are a strict structural QA pass for editable slide reconstruction. Return only JSON.",
        input=[
            {
                "role": "user",
                "content": content,
            }
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "slide_structure_refinement",
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
    parser.add_argument("output_spec")
    parser.add_argument("--model", default=None)
    parser.add_argument("--rendered-preview", default=None)
    args = parser.parse_args()

    load_env(ROOT / ".env")
    output_path = Path(args.output_spec)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not os.environ.get("OPENAI_API_KEY"):
        output_path.write_text(Path(args.spec).read_text())
        print("structural refinement skipped: OPENAI_API_KEY is not set")
        return

    spec = json.loads(Path(args.spec).read_text())
    if spec.get("layout") not in {"generic_slide", "generic_deck"}:
        output_path.write_text(json.dumps(spec, indent=2) + "\n")
        print(f"structural refinement skipped: unsupported layout {spec.get('layout')}")
        return

    validate_spec(spec)
    model = args.model or os.environ.get("OPENAI_STRUCTURE_MODEL") or os.environ.get("OPENAI_SPEC_MODEL") or DEFAULT_SPEC_MODEL
    rendered_preview = Path(args.rendered_preview) if args.rendered_preview else None
    response = request_structure_refinement(Path(args.source_image), spec, model=model, rendered_preview=rendered_preview)
    response, filtered_rejections = keep_structural_patches(response)
    refined, applied, rejected = apply_refinement(spec, response)
    rejected.extend(filtered_rejections)

    output_path.write_text(json.dumps(refined, indent=2) + "\n")
    print(f"structural refinement quality_score={response.get('quality_score')} model={model}")
    for issue in response.get("issues", [])[:8]:
        print(f"- {issue.get('severity')}: {issue.get('kind')}: {issue.get('description')}")
    if applied:
        print("applied structural patches:")
        for item in applied:
            print(f"- {item}")
    else:
        print("applied structural patches: none")
    if rejected:
        print("rejected unsafe structural patches:")
        for item in rejected:
            print(f"- {item}")


if __name__ == "__main__":
    main()
