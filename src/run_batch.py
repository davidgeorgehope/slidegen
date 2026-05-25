"""Run the slide rebuild pipeline over a directory of source images."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from pptx import Presentation

from render_generic import SLIDE_H, SLIDE_W, add_slide, specs_from_root


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


def natural_key(path: Path):
    parts = re.split(r"(\d+)", path.stem)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def discover_images(images_dir: Path, pattern: str, start: int | None, end: int | None) -> list[Path]:
    images = sorted(images_dir.glob(pattern), key=natural_key)
    if start is None and end is None:
        return images

    filtered = []
    for path in images:
        match = re.search(r"(\d+)$", path.stem)
        if not match:
            continue
        number = int(match.group(1))
        if start is not None and number < start:
            continue
        if end is not None and number > end:
            continue
        filtered.append(path)
    return filtered


def run(cmd: list[str], env: dict[str, str], dry_run: bool) -> None:
    print("+ " + " ".join(cmd), flush=True)
    if dry_run:
        return
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def pipeline_cmd(args, image_path: Path, spec_path: Path, out_path: Path) -> list[str]:
    cmd = [
        sys.executable,
        "src/run_pipeline.py",
        str(image_path),
        str(spec_path),
        str(out_path),
        "--asset-mode",
        args.asset_mode,
        "--spec-layout",
        args.spec_layout,
    ]
    if args.spec_model:
        cmd.extend(["--spec-model", args.spec_model])
    for helper in args.style_guide_image:
        cmd.extend(["--style-guide-image", helper])
    if args.template_pptx:
        cmd.extend(["--template-pptx", args.template_pptx])
    if args.refine:
        cmd.extend(["--refine", str(args.refine)])
    if args.refine_model:
        cmd.extend(["--refine-model", args.refine_model])
    if args.force_spec:
        cmd.extend(["--generate-spec", "--force-spec"])
    elif not spec_path.exists():
        cmd.append("--generate-spec")
    if args.skip_assets:
        cmd.append("--skip-assets")
    if args.skip_generic_assets:
        cmd.append("--skip-generic-assets")
    if args.no_verify:
        cmd.append("--no-verify")
    return cmd


def combine_specs(spec_paths: list[Path], out_path: Path) -> None:
    prs = Presentation()
    prs.slide_width = SLIDE_W
    prs.slide_height = SLIDE_H

    added = 0
    skipped = []
    for spec_path in spec_paths:
        spec = json.loads(spec_path.read_text())
        if spec.get("layout") not in {"generic_slide", "generic_deck"}:
            skipped.append(spec_path)
            continue
        for slide_spec in specs_from_root(spec):
            add_slide(prs, slide_spec, ROOT)
            added += 1

    if added == 0:
        raise RuntimeError("No generic slides were available for the combined deck")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out_path)
    print(f"wrote {out_path} with {added} slide(s)")
    if skipped:
        print("skipped non-generic specs in combined deck:")
        for path in skipped:
            print(f"- {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--images-dir", default="images")
    parser.add_argument("--pattern", default="image*.png")
    parser.add_argument("--spec-dir", default="specs/auto")
    parser.add_argument("--output-dir", default="output/auto")
    parser.add_argument("--combined", default="output/all_slides_auto.pptx")
    parser.add_argument("--asset-mode", choices=["auto", "extract", "generate"], default="auto")
    parser.add_argument("--spec-layout", default="generic_slide")
    parser.add_argument("--spec-model", default=None)
    parser.add_argument("--style-guide-image", action="append", default=[])
    parser.add_argument("--template-pptx", default=None)
    parser.add_argument("--refine", type=int, default=0)
    parser.add_argument("--refine-model", default=None)
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force-spec", action="store_true")
    parser.add_argument("--skip-assets", action="store_true")
    parser.add_argument(
        "--skip-generic-assets",
        action="store_true",
        help="extract logos but leave generic icons to native renderer fallback",
    )
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--no-combined", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    images_dir = ROOT / args.images_dir
    spec_dir = ROOT / args.spec_dir
    output_dir = ROOT / args.output_dir
    combined_path = ROOT / args.combined
    images = discover_images(images_dir, args.pattern, args.start, args.end)
    if args.limit is not None:
        images = images[: args.limit]
    if not images:
        raise SystemExit(f"No images matched {images_dir / args.pattern}")

    env = load_env(ROOT / ".env")
    spec_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    spec_paths = []
    for idx, image_path in enumerate(images, start=1):
        spec_path = spec_dir / f"{image_path.stem}.json"
        out_path = output_dir / f"{image_path.stem}.pptx"
        print(f"\n[{idx}/{len(images)}] {image_path.name}", flush=True)
        run(pipeline_cmd(args, image_path, spec_path, out_path), env, args.dry_run)
        spec_paths.append(spec_path)

    if not args.no_combined and not args.dry_run:
        combine_specs(spec_paths, combined_path)


if __name__ == "__main__":
    main()
