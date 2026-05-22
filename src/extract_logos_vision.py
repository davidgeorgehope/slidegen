"""OCR-driven logo extraction using macOS Vision.

The grid extractor is useful for a quick prototype, but it cannot know where a
vendor name starts or ends. This script uses Vision text boxes to find the
vendor names from the slide spec, expands each text box to nearby logo artwork,
and optionally writes the resulting crop paths/bboxes back into the spec.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

try:
    import Foundation
    import Quartz
    import Vision
except ImportError as exc:  # pragma: no cover - platform-specific import
    raise SystemExit("macOS Vision bindings are required: pyobjc-framework-Vision pyobjc-framework-Quartz") from exc


SAT_MIN = 38
VAL_MAX = 226
TEXT_EXPAND = 4
NEAR_GAP = 16
CROP_PAD = 6


@dataclass
class OCRBox:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def union_bbox(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    x0 = min(x for x, _, _, _ in boxes)
    y0 = min(y for _, y, _, _ in boxes)
    x1 = max(x + w for x, _, w, _ in boxes)
    y1 = max(y + h for _, y, _, h in boxes)
    return x0, y0, x1 - x0, y1 - y0


def intersects(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def expand_box(bbox: tuple[int, int, int, int], pad: int) -> tuple[int, int, int, int]:
    x, y, w, h = bbox
    return x - pad, y - pad, w + 2 * pad, h + 2 * pad


def ocr_text(image_path: Path) -> list[OCRBox]:
    url = Foundation.NSURL.fileURLWithPath_(str(image_path.resolve()))
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        raise RuntimeError(f"Could not load image: {image_path}")

    img_w = Quartz.CGImageGetWidth(image)
    img_h = Quartz.CGImageGetHeight(image)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)
    request.setRecognitionLanguages_(["en-US"])

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision OCR failed: {err}")

    boxes = []
    for obs in request.results() or []:
        cand = obs.topCandidates_(1)[0]
        bb = obs.boundingBox()
        x = int(round(bb.origin.x * img_w))
        y = int(round((1.0 - bb.origin.y - bb.size.height) * img_h))
        w = int(round(bb.size.width * img_w))
        h = int(round(bb.size.height * img_h))
        boxes.append(OCRBox(str(cand.string()), float(cand.confidence()), (x, y, w, h)))
    return boxes


def candidate_groups(ocr_boxes: list[OCRBox]) -> list[list[OCRBox]]:
    groups: list[list[OCRBox]] = [[box] for box in ocr_boxes]
    for i, a in enumerate(ocr_boxes):
        ax, ay, aw, ah = a.bbox
        for b in ocr_boxes[i + 1:]:
            bx, by, bw, bh = b.bbox
            same_column = abs(ax - bx) < max(32, min(aw, bw) * 0.8)
            close_y = abs((ay + ah / 2) - (by + bh / 2)) < 42
            stacked = same_column and abs((ay + ah) - by) < 42 or same_column and abs((by + bh) - ay) < 42
            same_line = close_y and abs(bx - (ax + aw)) < 130
            if same_line or stacked:
                group = sorted([a, b], key=lambda item: (item.bbox[1], item.bbox[0]))
                groups.append(group)
    return groups


def score_group(vendor_name: str, group: list[OCRBox]) -> float:
    target = normalize(vendor_name)
    observed = normalize(" ".join(box.text for box in group))
    if not observed:
        return 0.0
    score = SequenceMatcher(None, target, observed).ratio()
    if observed in target or target in observed:
        score = max(score, 0.9)
    return score


def match_vendor(vendor_name: str, ocr_boxes: list[OCRBox], used: set[int]) -> tuple[list[OCRBox], float] | tuple[None, float]:
    best_group = None
    best_score = 0.0
    for group in candidate_groups(ocr_boxes):
        ids = {id(box) for box in group}
        if ids & used:
            continue
        score = score_group(vendor_name, group)
        if score > best_score:
            best_group = group
            best_score = score
    if best_group is None or best_score < 0.58:
        return None, best_score
    used.update(id(box) for box in best_group)
    return best_group, best_score


def foreground_components(img: np.ndarray, window: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
    x, y, w, h = window
    x = max(0, x)
    y = max(0, y)
    w = min(img.shape[1] - x, w)
    h = min(img.shape[0] - y, h)
    crop = img[y:y + h, x:x + w]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    sat = hsv[..., 1]
    val = hsv[..., 2]
    mask = ((sat > SAT_MIN) | (val < VAL_MAX)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))

    num, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    components = []
    for i in range(1, num):
        cx, cy, cw, ch, area = stats[i]
        if area < 8:
            continue
        components.append((x + int(cx), y + int(cy), int(cw), int(ch)))
    return components


def near_selected(component: tuple[int, int, int, int], selected_bbox: tuple[int, int, int, int]) -> bool:
    cx, cy, cw, ch = component
    sx, sy, sw, sh = selected_bbox
    c_right = cx + cw
    c_bottom = cy + ch
    s_right = sx + sw
    s_bottom = sy + sh

    horizontal_gap = min(abs(cx - s_right), abs(sx - c_right))
    vertical_overlap = min(c_bottom, s_bottom) - max(cy, sy)
    vertical_gap = min(abs(cy - s_bottom), abs(sy - c_bottom))
    horizontal_overlap = min(c_right, s_right) - max(cx, sx)

    return (
        horizontal_gap <= NEAR_GAP and vertical_overlap > -3
        or vertical_gap <= NEAR_GAP and horizontal_overlap > -3
    )


def logo_bbox_from_text(img: np.ndarray, text_bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    tx, ty, tw, th = text_bbox
    window = (
        tx - max(90, int(tw * 0.9)),
        ty - max(34, int(th * 1.2)),
        tw + max(120, int(tw * 1.1)),
        th + max(56, int(th * 2.0)),
    )
    components = foreground_components(img, window)
    selected = [comp for comp in components if intersects(comp, expand_box(text_bbox, TEXT_EXPAND))]
    if not selected:
        return text_bbox

    changed = True
    while changed:
        changed = False
        current = union_bbox(selected)
        for comp in components:
            if comp in selected:
                continue
            if near_selected(comp, current):
                selected.append(comp)
                changed = True

    x, y, w, h = union_bbox(selected)
    x0 = max(0, x - CROP_PAD)
    y0 = max(0, y - CROP_PAD)
    x1 = min(img.shape[1], x + w + CROP_PAD)
    y1 = min(img.shape[0], y + h + CROP_PAD)
    return x0, y0, x1 - x0, y1 - y0


def make_background_transparent(crop_path: Path) -> None:
    img = Image.open(crop_path).convert("RGBA")
    pixels = img.load()
    w, h = img.size
    edge_samples = []
    for x in range(w):
        edge_samples.append(pixels[x, 0][:3])
        edge_samples.append(pixels[x, h - 1][:3])
    for y in range(h):
        edge_samples.append(pixels[0, y][:3])
        edge_samples.append(pixels[w - 1, y][:3])
    bg = tuple(sorted(channel)[len(channel) // 2] for channel in zip(*edge_samples))

    for y in range(h):
        for x in range(w):
            r, g, b, a = pixels[x, y]
            dist = abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2])
            bright_neutral = max(r, g, b) > 238 and (max(r, g, b) - min(r, g, b)) < 28
            if dist < 34 or bright_neutral:
                pixels[x, y] = (r, g, b, 0)
            else:
                pixels[x, y] = (r, g, b, a)
    img.save(crop_path)


def safe_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def update_spec(spec: dict, matches: dict[str, dict]) -> None:
    for column in spec.get("columns", []):
        for vendor in column.get("vendors", []):
            if not isinstance(vendor, dict):
                continue
            match = matches.get(vendor["name"])
            if match:
                vendor["image"] = match["path"]
                vendor["bbox"] = match["bbox"]
                vendor["ocr_text"] = match["ocr_text"]
                vendor["ocr_score"] = round(match["score"], 3)


def relative_to_repo(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


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
    img = cv2.imread(str(image_path))
    if img is None:
        raise SystemExit(f"Could not read {image_path}")

    ocr_boxes = ocr_text(image_path)
    used: set[int] = set()
    manifest = {"source_size": [int(img.shape[1]), int(img.shape[0])], "matches": []}
    matches_by_name: dict[str, dict] = {}

    for ci, column in enumerate(spec.get("columns", []), start=1):
        for vi, vendor in enumerate(column.get("vendors", []), start=1):
            name = vendor if isinstance(vendor, str) else vendor["name"]
            group, score = match_vendor(name, ocr_boxes, used)
            if group is None:
                print(f"warning: no OCR match for {name} (best score {score:.2f})", file=sys.stderr)
                continue

            text_bbox = union_bbox([box.bbox for box in group])
            bbox = logo_bbox_from_text(img, text_bbox)
            x, y, w, h = bbox
            crop = img[y:y + h, x:x + w]
            filename = f"col{ci}_v{vi}_{safe_name(name)}.png"
            path = out_dir / filename
            cv2.imwrite(str(path), crop)
            make_background_transparent(path)

            rel_path = relative_to_repo(path)
            entry = {
                "name": name,
                "ocr_text": " ".join(box.text for box in group),
                "score": score,
                "bbox": [int(x), int(y), int(w), int(h)],
                "path": rel_path,
            }
            manifest["matches"].append(entry)
            matches_by_name[name] = entry
            print(f"{name}: {entry['ocr_text']} score={score:.2f} bbox={entry['bbox']}")

    (out_dir / "logos_vision.json").write_text(json.dumps(manifest, indent=2))
    if args.update_spec:
        update_spec(spec, matches_by_name)
        spec_path.write_text(json.dumps(spec, indent=2) + "\n")


if __name__ == "__main__":
    main()
