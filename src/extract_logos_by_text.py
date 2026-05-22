"""Extract logos using Vision text anchors from any slide spec.

Specs opt in with a `logo_assets` list:

    {
      "logo_assets": [
        {"name": "network_gateway", "match": "Network Gateway"},
        {"name": "crm_app", "match": "CRM App"}
      ]
    }

Each match is resolved against OCR text, expanded to nearby foreground pixels,
saved as a transparent PNG, and written to `spec["assets"]`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2

from extract_logos_vision import (
    OCRBox,
    logo_bbox_from_text,
    make_background_transparent,
    match_vendor,
    normalize,
    ocr_text,
    safe_name,
    union_bbox,
)


def relative_to_repo(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def segmented_text_bbox(target: str, group: list[OCRBox], text_bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Narrow a wide OCR box when Vision merged multiple adjacent logos."""
    if len(group) != 1:
        return text_bbox

    observed = normalize(group[0].text)
    needle = normalize(target)
    if not observed or not needle:
        return text_bbox

    start = observed.find(needle)
    if start < 0:
        # Allow for small OCR prefixes or artifacts before the matched text.
        for offset in range(1, min(3, len(observed))):
            start = observed[offset:].find(needle)
            if start >= 0:
                start += offset
                break
    if start < 0 or len(observed) <= len(needle):
        return text_bbox

    x, y, w, h = text_bbox
    if w < 110 or w < h * 3:
        return text_bbox

    pad = max(10, int(w * 0.04))
    x0 = x + int(w * start / len(observed)) - pad
    x1 = x + int(w * (start + len(needle)) / len(observed)) + pad
    return x0, y, max(24, x1 - x0), h


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("spec")
    parser.add_argument("out_dir")
    parser.add_argument("--update-spec", action="store_true")
    args = parser.parse_args()

    image_path = Path(args.image)
    spec_path = Path(args.spec)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = json.loads(spec_path.read_text())
    img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"Could not read {image_path}")

    boxes = ocr_text(image_path)
    assets = spec.setdefault("assets", {})
    manifest = {}

    for item in spec.get("logo_assets", []):
        name = item["name"]
        target = item.get("match", name)
        group, score = match_vendor(target, boxes, set())
        if not group:
            print(f"WARN no match for {name} ({target}), best={score:.3f}")
            continue
        text_bbox = union_bbox([box.bbox for box in group])
        text_bbox = segmented_text_bbox(target, group, text_bbox)
        bbox = logo_bbox_from_text(img, text_bbox)
        x, y, w, h = bbox
        crop = img[y:y + h, x:x + w]
        out_path = out_dir / f"{safe_name(name)}.png"
        cv2.imwrite(str(out_path), crop)
        make_background_transparent(out_path)

        entry = {
            "path": relative_to_repo(out_path),
            "bbox": [int(x), int(y), int(w), int(h)],
            "source": "vision_logo_crop",
            "match": target,
            "ocr_text": " ".join(box.text for box in group),
            "ocr_score": round(float(score), 3),
        }
        assets[name] = entry
        manifest[name] = entry
        print(f"{name}: {entry['ocr_text']} score={score:.3f} bbox={entry['bbox']}")

    (out_dir / "logos_vision.json").write_text(json.dumps(manifest, indent=2))
    if args.update_spec:
        spec_path.write_text(json.dumps(spec, indent=2) + "\n")


if __name__ == "__main__":
    main()
