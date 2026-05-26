"""Run the slide rebuild pipeline for a spec.

This is intentionally small: specs declare their layout and asset needs; the
runner dispatches to generic extractors/renderers.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path) -> dict[str, str]:
    env = os.environ.copy()
    if not path.exists():
        return env
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env.setdefault(key, value)
    return env


def run(cmd: list[str], env: dict[str, str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def default_extract_dir(spec: dict, kind: str, extract_root: Path | None = None) -> Path:
    root = extract_root if extract_root is not None else ROOT / "extracted"
    return root / spec["slide"] / kind


def renderer_for(spec: dict) -> list[str]:
    layout = spec.get("layout")
    if layout in {"generic_slide", "generic_deck"}:
        return [sys.executable, "src/render_generic.py"]
    if layout == "architecture_parallel_layers":
        return [sys.executable, "src/render_architecture.py"]
    raise ValueError(f"Unsupported layout: {layout}")


def render(spec_path: Path, output_path: Path, env: dict[str, str]) -> None:
    run(renderer_for(json.loads(spec_path.read_text())) + [str(spec_path), str(output_path)], env)


def refine_output(
    image_path: Path,
    spec_path: Path,
    output_path: Path,
    *,
    iterations: int,
    model: str | None,
    verify: bool,
    env: dict[str, str],
) -> None:
    for iteration in range(1, iterations + 1):
        previous_spec = spec_path.read_text()
        preview_path = output_path.with_suffix(output_path.suffix + f".refine{iteration}.png")
        refined_path = spec_path.with_suffix(spec_path.suffix + f".refine{iteration}")
        run(
            [
                sys.executable,
                "src/render_preview.py",
                str(output_path),
                str(preview_path),
            ],
            env,
        )

        cmd = [
            sys.executable,
            "src/refine_spec_openai.py",
            str(image_path),
            str(spec_path),
            str(preview_path),
            str(refined_path),
        ]
        if model:
            cmd.extend(["--model", model])
        run(cmd, env)

        refined_spec = refined_path.read_text()
        if refined_spec == previous_spec:
            print("refinement produced no spec changes; stopping", flush=True)
            break
        spec_path.write_text(refined_spec)
        if verify:
            run([sys.executable, "src/verify_spec.py", str(spec_path)], env)
        render(spec_path, output_path, env)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="source PNG")
    parser.add_argument("spec", help="JSON spec")
    parser.add_argument("output", help="output PPTX")
    parser.add_argument("--asset-mode", choices=["auto", "extract", "generate"], default="auto")
    parser.add_argument("--generate-spec", action="store_true", help="draft the JSON spec from the source image first")
    parser.add_argument("--force-spec", action="store_true", help="overwrite an existing generated spec")
    parser.add_argument("--spec-layout", default="generic_slide", help="layout to request from spec generation")
    parser.add_argument("--spec-model", default=None, help="OpenAI model for spec generation")
    parser.add_argument("--style-guide-image", action="append", default=[], help="optional brand/style guide image")
    parser.add_argument("--template-pptx", default=None, help="optional template deck style reference")
    parser.add_argument(
        "--refine",
        type=int,
        default=2,
        help="render/preview/critique refinement iterations; use 0 for fast layout-only runs",
    )
    parser.add_argument("--refine-model", default=None, help="OpenAI model for refinement critique")
    parser.add_argument("--no-verify", action="store_true", help="skip non-fatal generated spec verification")
    parser.add_argument("--skip-assets", action="store_true")
    parser.add_argument("--extract-root", default=None, help="scratch root for extracted logos and generated assets")
    parser.add_argument("--icon-library-dir", default=None, help="scratch/cache directory for generated icon library")
    parser.add_argument(
        "--skip-generic-assets",
        action="store_true",
        help="layout-QA only: extract real logos but draw generic icons as native placeholders",
    )
    args = parser.parse_args()

    image_path = Path(args.image)
    spec_path = Path(args.spec)
    output_path = Path(args.output)
    env = load_env(ROOT / ".env")
    extract_root = Path(args.extract_root) if args.extract_root else None
    if args.icon_library_dir:
        env["SLIDEGEN_ICON_LIBRARY_DIR"] = str(Path(args.icon_library_dir))
    if args.skip_generic_assets:
        env["SLIDEGEN_ALLOW_NATIVE_ICON_PLACEHOLDERS"] = "1"

    if args.generate_spec or not spec_path.exists():
        cmd = [
            sys.executable,
            "src/generate_spec_openai.py",
            str(image_path),
            str(spec_path),
            "--layout",
            args.spec_layout,
        ]
        if args.spec_model:
            cmd.extend(["--model", args.spec_model])
        for helper in args.style_guide_image:
            cmd.extend(["--style-guide-image", helper])
        if args.template_pptx:
            cmd.extend(["--template-pptx", args.template_pptx])
        if args.force_spec or not spec_path.exists():
            cmd.append("--force")
        run(cmd, env)

    spec = json.loads(spec_path.read_text())
    if not args.skip_assets:
        if spec.get("logo_assets"):
            run(
                [
                    sys.executable,
                    "src/extract_logos_by_text.py",
                    str(image_path),
                    str(spec_path),
                    str(default_extract_dir(spec, "logos", extract_root)),
                    "--update-spec",
                ],
                env,
            )

        # Reload because logo extraction may have updated the spec.
        spec = json.loads(spec_path.read_text())
        if spec.get("asset_queries") and not args.skip_generic_assets:
            run(
                [
                    sys.executable,
                    "src/extract_assets_vision.py",
                    str(image_path),
                    str(spec_path),
                    str(default_extract_dir(spec, "assets", extract_root)),
                    "--asset-mode",
                    args.asset_mode,
                    "--update-spec",
                ],
                env,
            )

    if not args.no_verify:
        run([sys.executable, "src/verify_spec.py", str(spec_path)], env)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    render(spec_path, output_path, env)
    if args.refine > 0:
        refine_output(
            image_path,
            spec_path,
            output_path,
            iterations=args.refine,
            model=args.refine_model,
            verify=not args.no_verify,
            env=env,
        )


if __name__ == "__main__":
    main()
