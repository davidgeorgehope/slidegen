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
import tempfile
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


def bool_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def pending_icon_regenerations(spec: dict) -> list[str]:
    names = []
    for query in spec.get("asset_queries", []):
        if not isinstance(query, dict):
            continue
        if not bool_value(query.get("regenerate")) and not bool_value(query.get("force_regenerate")):
            continue
        name = str(query.get("name") or query.get("asset") or "").strip()
        if name:
            names.append(name)
    return names


def extract_generic_assets(
    image_path: Path,
    spec_path: Path,
    spec: dict,
    *,
    extract_root: Path | None,
    asset_mode: str,
    icon_generation_input: str,
    env: dict[str, str],
    only_assets: list[str] | None = None,
) -> None:
    cmd = [
        sys.executable,
        "src/extract_assets_vision.py",
        str(image_path),
        str(spec_path),
        str(default_extract_dir(spec, "assets", extract_root)),
        "--asset-mode",
        asset_mode,
        "--icon-generation-input",
        icon_generation_input,
        "--update-spec",
    ]
    if only_assets:
        cmd.extend(["--only-assets", ",".join(only_assets)])
    run(cmd, env)


def renderer_for(spec: dict) -> list[str]:
    layout = spec.get("layout")
    if layout in {"generic_slide", "generic_deck"}:
        return [sys.executable, "src/render_generic.py"]
    if layout == "architecture_parallel_layers":
        raise ValueError(
            "Unsupported legacy layout: architecture_parallel_layers. "
            "Regenerate the spec as generic_slide or generic_deck."
        )
    raise ValueError(f"Unsupported layout: {layout}")


def render(spec_path: Path, output_path: Path, env: dict[str, str]) -> None:
    run(renderer_for(json.loads(spec_path.read_text())) + [str(spec_path), str(output_path)], env)


def refine_structure(
    image_path: Path,
    spec_path: Path,
    *,
    iterations: int,
    model: str | None,
    env: dict[str, str],
    rendered_preview: Path | None = None,
) -> None:
    if iterations <= 0:
        return
    with tempfile.TemporaryDirectory(prefix="slidegen_structure_") as tmp:
        refine_dir = Path(tmp)
        for iteration in range(1, iterations + 1):
            previous_spec = spec_path.read_text()
            refined_path = refine_dir / f"{spec_path.name}.structure{iteration}.json"
            cmd = [
                sys.executable,
                "src/refine_structure_openai.py",
                str(image_path),
                str(spec_path),
                str(refined_path),
            ]
            if model:
                cmd.extend(["--model", model])
            if rendered_preview:
                cmd.extend(["--rendered-preview", str(rendered_preview)])
            run(cmd, env)
            refined_spec = refined_path.read_text()
            if refined_spec == previous_spec:
                print("structural refinement produced no spec changes; stopping", flush=True)
                break
            spec_path.write_text(refined_spec)


def refine_output(
    image_path: Path,
    spec_path: Path,
    output_path: Path,
    *,
    iterations: int,
    model: str | None,
    verify: bool,
    env: dict[str, str],
    extract_root: Path | None,
    asset_mode: str,
    icon_generation_input: str,
    skip_generic_assets: bool,
) -> None:
    with tempfile.TemporaryDirectory(prefix="slidegen_refine_") as tmp:
        refine_dir = Path(tmp)
        for iteration in range(1, iterations + 1):
            previous_spec = spec_path.read_text()
            preview_path = refine_dir / f"{output_path.stem}.refine{iteration}.png"
            refined_path = refine_dir / f"{spec_path.name}.refine{iteration}.json"
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
            regenerated_assets = pending_icon_regenerations(json.loads(refined_spec))
            if regenerated_assets:
                if skip_generic_assets:
                    print(
                        "refinement requested icon regeneration, but generic asset generation is skipped",
                        flush=True,
                    )
                else:
                    extract_generic_assets(
                        image_path,
                        spec_path,
                        json.loads(spec_path.read_text()),
                        extract_root=extract_root,
                        asset_mode=asset_mode,
                        icon_generation_input=icon_generation_input,
                        env=env,
                        only_assets=regenerated_assets,
                    )
            if verify:
                run([sys.executable, "src/verify_spec.py", str(spec_path)], env)
            render(spec_path, output_path, env)


def post_render_structure_refine(
    image_path: Path,
    spec_path: Path,
    output_path: Path,
    *,
    model: str | None,
    verify: bool,
    env: dict[str, str],
) -> None:
    with tempfile.TemporaryDirectory(prefix="slidegen_structure_final_") as tmp:
        preview_path = Path(tmp) / f"{output_path.stem}.structure.png"
        previous_spec = spec_path.read_text()
        run([sys.executable, "src/render_preview.py", str(output_path), str(preview_path)], env)
        refine_structure(
            image_path,
            spec_path,
            iterations=1,
            model=model,
            env=env,
            rendered_preview=preview_path,
        )
        if spec_path.read_text() == previous_spec:
            return
        if verify:
            run([sys.executable, "src/verify_spec.py", str(spec_path)], env)
        render(spec_path, output_path, env)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="source PNG")
    parser.add_argument("spec", help="where to write the generated JSON spec")
    parser.add_argument("output", help="output PPTX")
    parser.add_argument("--asset-mode", choices=["auto", "extract", "generate"], default="auto")
    parser.add_argument(
        "--spec-layout",
        choices=["generic_slide", "generic_deck"],
        default="generic_slide",
        help="layout to request from spec generation",
    )
    parser.add_argument("--spec-model", default=None, help="OpenAI model for spec generation")
    parser.add_argument(
        "--spec-refine",
        type=int,
        default=1,
        help="bounded structural QA passes before assets and after rendered QA; use 0 to skip",
    )
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
        "--icon-generation-input",
        choices=["source", "description"],
        default="description",
        help="description: describe source crop first, then generate from text; source: edit from source crop",
    )
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
    run(cmd, env)
    refine_structure(
        image_path,
        spec_path,
        iterations=args.spec_refine,
        model=args.spec_model,
        env=env,
    )

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
            extract_generic_assets(
                image_path,
                spec_path,
                spec,
                extract_root=extract_root,
                asset_mode=args.asset_mode,
                icon_generation_input=args.icon_generation_input,
                env=env,
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
            extract_root=extract_root,
            asset_mode=args.asset_mode,
            icon_generation_input=args.icon_generation_input,
            skip_generic_assets=args.skip_generic_assets,
        )
        if args.spec_refine > 0:
            post_render_structure_refine(
                image_path,
                spec_path,
                output_path,
                model=args.spec_model,
                verify=not args.no_verify,
                env=env,
            )


if __name__ == "__main__":
    main()
