"""Create a smaller PPTX by downsampling embedded media images.

The slide content remains editable because this rewrites only files under
`ppt/media/` inside the OOXML zip package. It is intended for Google Slides
conversion limits and browser upload reliability.
"""
from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path

from PIL import Image


MEDIA_PREFIX = "ppt/media/"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


def should_process(name: str) -> bool:
    path = Path(name)
    return name.startswith(MEDIA_PREFIX) and path.suffix.lower() in IMAGE_EXTENSIONS


def compact_image(data: bytes, *, max_edge: int, jpeg_quality: int) -> tuple[bytes, bool]:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        width, height = image.size
        changed = max(width, height) > max_edge
        if changed:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

        output = io.BytesIO()
        has_alpha = image.mode in {"RGBA", "LA"} or (
            image.mode == "P" and "transparency" in image.info
        )
        source_format = (image.format or "PNG").upper()
        if source_format in {"JPEG", "JPG"} and not has_alpha:
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=jpeg_quality, optimize=True)
        else:
            if image.mode not in {"RGBA", "LA", "P"}:
                image = image.convert("RGBA" if has_alpha else "RGB")
            image.save(output, format="PNG", optimize=True, compress_level=9)

        compacted = output.getvalue()
        if len(compacted) >= len(data):
            return data, False
        return compacted, changed or len(compacted) < len(data)


def compact_pptx(input_path: Path, output_path: Path, *, max_edge: int, jpeg_quality: int) -> dict[str, int]:
    stats = {
        "media_files": 0,
        "processed": 0,
        "changed": 0,
        "input_media_bytes": 0,
        "output_media_bytes": 0,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(input_path, "r") as source, zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            out_data = data
            if info.filename.startswith(MEDIA_PREFIX):
                stats["media_files"] += 1
                stats["input_media_bytes"] += len(data)
            if should_process(info.filename):
                stats["processed"] += 1
                try:
                    out_data, changed = compact_image(data, max_edge=max_edge, jpeg_quality=jpeg_quality)
                    if changed:
                        stats["changed"] += 1
                except Exception as exc:  # noqa: BLE001 - keep deck valid by copying unhandled images
                    print(f"warning: could not compact {info.filename}: {exc}")
                    out_data = data
            if info.filename.startswith(MEDIA_PREFIX):
                stats["output_media_bytes"] += len(out_data)

            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.compress_type = zipfile.ZIP_DEFLATED
            new_info.comment = info.comment
            new_info.extra = info.extra
            new_info.internal_attr = info.internal_attr
            new_info.external_attr = info.external_attr
            target.writestr(new_info, out_data)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--max-edge", type=int, default=384)
    parser.add_argument("--jpeg-quality", type=int, default=82)
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    stats = compact_pptx(
        input_path,
        output_path,
        max_edge=args.max_edge,
        jpeg_quality=args.jpeg_quality,
    )
    print(f"wrote {output_path}")
    print(f"input size: {input_path.stat().st_size:,} bytes")
    print(f"output size: {output_path.stat().st_size:,} bytes")
    print(
        "media bytes: "
        f"{stats['input_media_bytes']:,} -> {stats['output_media_bytes']:,}; "
        f"changed {stats['changed']}/{stats['processed']} processed images"
    )


if __name__ == "__main__":
    main()
