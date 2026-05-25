"""Render a PPTX preview image with macOS Quick Look."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image


def newest_png(directory: Path) -> Path | None:
    images = list(directory.glob("*.png"))
    if not images:
        return None
    return max(images, key=lambda path: path.stat().st_mtime)


def render_preview(pptx_path: Path, output_path: Path, *, size: int) -> None:
    pptx_path = pptx_path.resolve()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_root = Path(os.environ.get("SLIDEGEN_PREVIEW_TMPDIR") or tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="slidegen_preview_", dir=tmp_root) as tmp:
        tmp_path = Path(tmp)
        try:
            result = subprocess.run(
                ["qlmanage", "-t", "-s", str(size), "-o", str(tmp_path), str(pptx_path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            details = "\n".join(part for part in (exc.stdout, exc.stderr) if part)
            raise RuntimeError(f"Quick Look failed for {pptx_path}\n{details}") from exc
        generated = newest_png(tmp_path)
        if generated is None:
            details = "\n".join(part for part in (result.stdout, result.stderr) if part)
            raise RuntimeError(f"Quick Look did not produce a PNG preview for {pptx_path}\n{details}")

        with Image.open(generated) as image:
            image.convert("RGB").save(output_path)
        shutil.copystat(generated, output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pptx")
    parser.add_argument("output_png")
    parser.add_argument("--size", type=int, default=1800)
    args = parser.parse_args()

    render_preview(Path(args.pptx), Path(args.output_png), size=args.size)
    print(f"wrote {args.output_png}")


if __name__ == "__main__":
    main()
