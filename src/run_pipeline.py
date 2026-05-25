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
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def default_extract_dir(spec: dict, kind: str) -> Path:
    return ROOT / "extracted" / spec["slide"] / kind


def renderer_for(spec: dict) -> list[str]:
    layout = spec.get("layout")
    if layout in {"generic_slide", "generic_deck"}:
        return [sys.executable, "src/render_generic.py"]
    if layout == "architecture_parallel_layers":
        return [sys.executable, "src/render_architecture.py"]
    raise ValueError(f"Unsupported layout: {layout}")


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
    parser.add_argument("--no-verify", action="store_true", help="skip non-fatal generated spec verification")
    parser.add_argument("--skip-assets", action="store_true")
    parser.add_argument(
        "--skip-generic-assets",
        action="store_true",
        help="extract real logo assets but leave generic icons to the native renderer fallback",
    )
    args = parser.parse_args()

    image_path = Path(args.image)
    spec_path = Path(args.spec)
    output_path = Path(args.output)
    env = load_env(ROOT / ".env")

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
                    str(default_extract_dir(spec, "logos")),
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
                    str(default_extract_dir(spec, "assets")),
                    "--asset-mode",
                    args.asset_mode,
                    "--update-spec",
                ],
                env,
            )

    if not args.no_verify:
        run([sys.executable, "src/verify_spec.py", str(spec_path)], env)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    run(renderer_for(json.loads(spec_path.read_text())) + [str(spec_path), str(output_path)], env)


if __name__ == "__main__":
    main()
