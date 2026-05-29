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
import numpy as np
from PIL import Image

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


def source_size(spec: dict, img) -> tuple[int, int]:
    raw = spec.get("source_size")
    if isinstance(raw, list) and len(raw) == 2:
        try:
            return int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            pass
    return int(img.shape[1]), int(img.shape[0])


def relative_to_repo(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def iter_elements(spec: dict):
    for element in spec.get("elements", []):
        if isinstance(element, dict):
            yield element
    for slide in spec.get("slides", []):
        if not isinstance(slide, dict):
            continue
        for element in slide.get("elements", []):
            if isinstance(element, dict):
                yield element


def asset_bbox_hints(spec: dict, img) -> dict[str, tuple[int, int, int, int]]:
    source_w, source_h = source_size(spec, img)
    img_w = int(img.shape[1])
    img_h = int(img.shape[0])
    sx = img_w / source_w if source_w else 1.0
    sy = img_h / source_h if source_h else 1.0
    hints = {}
    for element in iter_elements(spec):
        if element.get("type") != "image":
            continue
        asset = element.get("asset")
        bbox = element.get("bbox")
        if not asset or not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            x, y, w, h = (float(value) for value in bbox)
        except (TypeError, ValueError):
            continue
        hints[str(asset)] = (
            int(round(x * sx)),
            int(round(y * sy)),
            int(round(w * sx)),
            int(round(h * sy)),
        )
    return hints


def clamp_bbox_to_hint(
    bbox: tuple[int, int, int, int],
    hint: tuple[int, int, int, int] | None,
    img,
) -> tuple[int, int, int, int]:
    if hint is None:
        return bbox
    x, y, w, h = bbox
    hx, hy, hw, hh = hint
    if hw <= 0 or hh <= 0:
        return bbox

    pad_x = max(8, int(hw * 0.15))
    pad_y = max(4, int(hh * 0.28))
    hx0 = max(0, hx - pad_x)
    hy0 = max(0, hy - pad_y)
    hx1 = min(int(img.shape[1]), hx + hw + pad_x)
    hy1 = min(int(img.shape[0]), hy + hh + pad_y)

    x0 = max(x, hx0)
    y0 = max(y, hy0)
    x1 = min(x + w, hx1)
    y1 = min(y + h, hy1)
    if x1 <= x0 or y1 <= y0:
        # The OCR text matched a different occurrence of the same logo text.
        # Prefer the source-position hint so repeated wordmarks do not all
        # collapse to the first OCR hit.
        exact_x = max(0, hx)
        exact_y = max(0, hy)
        exact_w = min(int(img.shape[1]) - exact_x, hw)
        exact_h = min(int(img.shape[0]) - exact_y, hh)
        return exact_x, exact_y, exact_w, exact_h
    return x0, y0, x1 - x0, y1 - y0


def bbox_intersects(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def crop_logo_asset(
    img,
    out_path: Path,
    bbox: tuple[int, int, int, int],
    *,
    remove_colored_background: bool = False,
) -> tuple[int, int, int, int]:
    x, y, w, h = bbox
    crop = img[y:y + h, x:x + w]
    cv2.imwrite(str(out_path), crop)
    make_background_transparent(out_path)
    if remove_colored_background:
        remove_colored_edge_background(out_path)
    return int(x), int(y), int(w), int(h)


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


def remove_colored_edge_background(path: Path) -> None:
    """Remove saturated panel backgrounds that survive neutral transparency.

    White/light slide backgrounds use the generic transparency helper. This
    pass is for logos placed on colored bands, where the source panel color can
    differ slightly from the editable shape fill and leave a visible rectangle.
    """
    with Image.open(path).convert("RGBA") as image:
        pixels = np.array(image)

    rgb = pixels[..., :3].astype(np.int16)
    edge_pixels = np.concatenate(
        [
            pixels[0, :, :],
            pixels[-1, :, :],
            pixels[:, 0, :],
            pixels[:, -1, :],
        ],
        axis=0,
    )
    edge_rgb = edge_pixels[edge_pixels[:, 3] > 0, :3].astype(np.int16)
    if edge_rgb.size == 0:
        return
    bg = np.median(edge_rgb, axis=0).astype(np.int16)
    bg_hsv = cv2.cvtColor(np.uint8([[bg]]), cv2.COLOR_RGB2HSV)[0, 0]
    if int(bg_hsv[1]) < 36:
        return

    hsv = cv2.cvtColor(pixels[..., :3], cv2.COLOR_RGB2HSV)
    delta = np.max(np.abs(rgb - bg), axis=2)
    colored_background = (
        (pixels[..., 3] > 0)
        & (delta < 150)
        & (hsv[..., 1] > 28)
        & (np.abs(hsv[..., 0].astype(np.int16) - int(bg_hsv[0])) < 35)
    )
    if not colored_background.any():
        return
    pixels[colored_background, :3] = 0
    pixels[colored_background, 3] = 0
    Image.fromarray(pixels).save(path)


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
    bbox_hints = asset_bbox_hints(spec, img)

    for item in spec.get("logo_assets", []):
        name = item["name"]
        target = item.get("match", name)
        hint = bbox_hints.get(name)
        group, score = match_vendor(target, boxes, set())
        out_path = out_dir / f"{safe_name(name)}.png"
        if not group:
            if hint is None:
                print(f"WARN no match for {name} ({target}), best={score:.3f}")
                continue
            x, y, w, h = crop_logo_asset(img, out_path, clamp_bbox_to_hint(hint, hint, img))
            entry = {
                "path": relative_to_repo(out_path),
                "bbox": [x, y, w, h],
                "source": "bbox_hint_logo_crop",
                "match": target,
                "ocr_text": None,
                "ocr_score": round(float(score), 3),
            }
            assets[name] = entry
            manifest[name] = entry
            print(f"{name}: bbox={[x, y, w, h]} match={target!r} source=bbox_hint_logo_crop")
            continue
        text_bbox = union_bbox([box.bbox for box in group])
        text_bbox = segmented_text_bbox(target, group, text_bbox)
        bbox = logo_bbox_from_text(img, text_bbox)
        used_hint_fallback = hint is not None and not bbox_intersects(bbox, hint)
        bbox = clamp_bbox_to_hint(bbox, hint, img)
        x, y, w, h = crop_logo_asset(
            img,
            out_path,
            bbox,
            remove_colored_background=used_hint_fallback,
        )

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
