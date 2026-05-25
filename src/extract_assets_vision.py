"""Resolve arbitrary slide assets from generation or Vision OCR anchors.

For generic pictograms the preferred flow is:

1. In `auto` mode, regenerate assets marked `generatable: true` when
   OPENAI_API_KEY exists.
2. If generation is unavailable or fails in `auto`, fall back to Vision/OpenCV
   extraction.
3. Use `extract` for deterministic source crops only, or `generate` to require
   GPT image generation.

Vision extraction is the scalable version of "crop the icon next to this
label":

    {
      "name": "overview_handshake",
      "anchor_text": "Different job to be done",
      "crop_rule": "nearest_icon_left"
    }

The extractor finds the OCR box for `anchor_text`, detects foreground
components near that box, chooses the nearest component cluster based on the
rule, writes a transparent PNG crop, and records the detected bbox/path back
into `spec["assets"]`.

Brand assets and vendor logos should not be regenerated. Use template/master
assets for brand marks and source/library assets for vendor logos.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import cv2
import numpy as np

from extract_logos_vision import (
    OCRBox,
    make_background_transparent,
    ocr_text,
    safe_name,
    union_bbox,
)


SAT_MIN = 34
VAL_MAX = 232
MIN_COMPONENT_AREA = 8
CLUSTER_GAP = 14
CROP_PAD = 6
IMAGE_GEN_CLI = Path.home() / ".codex/skills/.system/imagegen/scripts/image_gen.py"
GENERATED_CACHE_DIR = Path("extracted") / "generated_cache"


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def expand_bbox(bbox: tuple[int, int, int, int], pad: int, img: np.ndarray) -> tuple[int, int, int, int]:
    x, y, w, h = bbox
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(img.shape[1], x + w + pad)
    y1 = min(img.shape[0], y + h + pad)
    return x0, y0, x1 - x0, y1 - y0


def bbox_center(bbox: tuple[int, int, int, int]) -> tuple[float, float]:
    x, y, w, h = bbox
    return x + w / 2, y + h / 2


def gap_between(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    horizontal = max(0, max(ax, bx) - min(ax + aw, bx + bw))
    vertical = max(0, max(ay, by) - min(ay + ah, by + bh))
    return math.hypot(horizontal, vertical)


def candidate_groups(ocr_boxes: list[OCRBox]) -> list[list[OCRBox]]:
    groups = [[box] for box in ocr_boxes]
    for i, a in enumerate(ocr_boxes):
        ax, ay, aw, ah = a.bbox
        for b in ocr_boxes[i + 1:]:
            bx, by, bw, bh = b.bbox
            same_line = abs((ay + ah / 2) - (by + bh / 2)) < 24 and abs(bx - (ax + aw)) < 190
            same_column = abs(ax - bx) < max(40, min(aw, bw) * 0.9)
            stacked = same_column and abs((ay + ah) - by) < 42 or same_column and abs((by + bh) - ay) < 42
            if same_line or stacked:
                groups.append(sorted([a, b], key=lambda item: (item.bbox[1], item.bbox[0])))
    return groups


def group_score(anchor_text: str, group: list[OCRBox]) -> float:
    target = normalize(anchor_text)
    observed = normalize(" ".join(box.text for box in group))
    if not target or not observed:
        return 0.0
    score = SequenceMatcher(None, target, observed).ratio()
    if observed in target or target in observed:
        score = max(score, 0.92)
    return score


def preference_score(group_bbox: tuple[int, int, int, int], prefer: str | None, img: np.ndarray) -> float:
    if not prefer:
        return 0.0
    cx, cy = bbox_center(group_bbox)
    w, h = img.shape[1], img.shape[0]
    targets = {
        "top_left": (0, 0),
        "top_right": (w, 0),
        "bottom_left": (0, h),
        "bottom_right": (w, h),
        "left": (0, cy),
        "right": (w, cy),
        "top": (cx, 0),
        "bottom": (cx, h),
    }
    tx, ty = targets.get(prefer, (cx, cy))
    return -math.hypot(cx - tx, cy - ty) / max(w, h)


def match_anchor(anchor_text: str, ocr_boxes: list[OCRBox], img: np.ndarray, prefer: str | None = None):
    best = None
    for group in candidate_groups(ocr_boxes):
        bbox = union_bbox([box.bbox for box in group])
        score = group_score(anchor_text, group)
        if score < 0.55:
            continue
        combined = score + preference_score(bbox, prefer, img)
        if best is None or combined > best["combined"]:
            best = {
                "group": group,
                "bbox": bbox,
                "score": score,
                "combined": combined,
                "ocr_text": " ".join(box.text for box in group),
            }
    return best


def components_in_window(img: np.ndarray, window: tuple[int, int, int, int]) -> list[tuple[int, int, int, int]]:
    x, y, w, h = window
    x = max(0, x)
    y = max(0, y)
    w = min(img.shape[1] - x, w)
    h = min(img.shape[0] - y, h)
    if w <= 0 or h <= 0:
        return []

    crop = img[y:y + h, x:x + w]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    sat = hsv[..., 1]
    val = hsv[..., 2]
    mask = ((sat > SAT_MIN) | (val < VAL_MAX)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))

    num, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    components = []
    for i in range(1, num):
        cx, cy, cw, ch, area = stats[i]
        if area < MIN_COMPONENT_AREA:
            continue
        if cw > w * 0.8 or ch > h * 0.8:
            continue
        components.append((x + int(cx), y + int(cy), int(cw), int(ch)))
    return components


def cluster_components(components: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    clusters: list[list[tuple[int, int, int, int]]] = []
    for comp in components:
        placed = False
        for cluster in clusters:
            if any(gap_between(comp, other) <= CLUSTER_GAP for other in cluster):
                cluster.append(comp)
                placed = True
                break
        if not placed:
            clusters.append([comp])

    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            if changed:
                break
            for j in range(i + 1, len(clusters)):
                if any(gap_between(a, b) <= CLUSTER_GAP for a in clusters[i] for b in clusters[j]):
                    clusters[i].extend(clusters[j])
                    del clusters[j]
                    changed = True
                    break
    return [union_bbox(cluster) for cluster in clusters]


def search_window(anchor_bbox: tuple[int, int, int, int], rule: str) -> tuple[int, int, int, int]:
    ax, ay, aw, ah = anchor_bbox
    if rule == "text_box":
        return anchor_bbox
    if rule == "nearest_icon_left":
        return ax - 180, ay - 55, 175, ah + 110
    if rule == "nearest_icon_right":
        return ax + aw + 5, ay - 55, 175, ah + 110
    if rule == "nearest_icon_above":
        return ax - 40, ay - 145, aw + 80, 140
    if rule == "nearest_icon_below":
        return ax - 40, ay + ah + 5, aw + 80, 140
    raise ValueError(f"Unknown crop_rule: {rule}")


def choose_cluster(anchor_bbox: tuple[int, int, int, int], rule: str, clusters: list[tuple[int, int, int, int]]):
    if not clusters:
        return None

    ax, ay, aw, ah = anchor_bbox
    anchor_cx, anchor_cy = bbox_center(anchor_bbox)

    def score(cluster):
        cx, cy = bbox_center(cluster)
        x, y, w, h = cluster
        area = w * h
        if rule == "nearest_icon_left":
            side_penalty = 0 if cx < ax else 1000
            target = (ax - 45, anchor_cy)
        elif rule == "nearest_icon_right":
            side_penalty = 0 if cx > ax + aw else 1000
            target = (ax + aw + 45, anchor_cy)
        elif rule == "nearest_icon_above":
            side_penalty = 0 if cy < ay else 1000
            target = (anchor_cx, ay - 45)
        else:
            side_penalty = 0 if cy > ay + ah else 1000
            target = (anchor_cx, ay + ah + 45)
        compact_penalty = 0 if 10 <= w <= 110 and 10 <= h <= 110 else 120
        distance = math.hypot(cx - target[0], cy - target[1])
        return side_penalty + compact_penalty + distance - min(area, 6000) / 6000

    return min(clusters, key=score)


def crop_asset(img: np.ndarray, anchor_bbox: tuple[int, int, int, int], rule: str):
    if rule == "text_box":
        return expand_bbox(anchor_bbox, CROP_PAD, img)
    window = search_window(anchor_bbox, rule)
    clusters = cluster_components(components_in_window(img, window))
    cluster = choose_cluster(anchor_bbox, rule, clusters)
    if cluster is None:
        return None
    return expand_bbox(cluster, CROP_PAD, img)


def query_bbox_crop(query: dict, img: np.ndarray):
    bbox = query.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    try:
        x, y, w, h = [int(round(float(value))) for value in bbox]
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    x = max(0, min(x, img.shape[1] - 1))
    y = max(0, min(y, img.shape[0] - 1))
    w = max(1, min(w, img.shape[1] - x))
    h = max(1, min(h, img.shape[0] - y))
    return expand_bbox((x, y, w, h), int(query.get("pad", CROP_PAD)), img)


def relative_to_repo(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def existing_asset_path(asset: dict) -> Path | None:
    path_value = asset.get("path") if isinstance(asset, dict) else None
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path if path.exists() else None


def generation_prompt(query: dict) -> str:
    prompt = query.get("generation_prompt")
    if prompt:
        return prompt
    label = query.get("semantic_label") or query["name"].replace("_", " ")
    return (
        "Create a simple, text-free SaaS/security presentation pictogram. "
        f"Subject: {label}. "
        "Style: clean vector-like enterprise presentation icon, blue/green/red line art as appropriate, "
        "minimal detail, centered, generous padding. "
        "Background: perfectly flat solid #00ff00 chroma-key color for background removal. "
        "No words, no letters, no logos, no watermark, no shadow, no gradient background."
    )


def can_generate_asset(query: dict, mode: str) -> bool:
    if mode == "extract":
        return False
    if not query.get("generatable", False):
        return False
    if mode == "generate":
        return True
    return bool(os.getenv("OPENAI_API_KEY"))


def generate_asset(query: dict, out_dir: Path) -> dict | None:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required for --asset-mode generate")
    if not IMAGE_GEN_CLI.exists():
        raise RuntimeError(f"Image generation CLI not found: {IMAGE_GEN_CLI}")

    raw_path = out_dir / f"{safe_name(query['name'])}_generated_raw.png"
    final_path = out_dir / f"{safe_name(query['name'])}.png"
    prompt = generation_prompt(query)
    model = query.get("generation_model", "gpt-image-2")
    size = query.get("generation_size", "1024x1024")
    quality = query.get("generation_quality", "medium")
    cache_key = hashlib.sha256(
        json.dumps(
            {"model": model, "size": size, "quality": quality, "prompt": prompt},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    cache_path = GENERATED_CACHE_DIR / f"{cache_key}.png"
    if cache_path.exists():
        shutil.copyfile(cache_path, final_path)
        return {
            "path": relative_to_repo(final_path),
            "bbox": None,
            "anchor_text": query.get("anchor_text"),
            "source": "generated_cache",
            "generation_prompt": prompt,
            "cache_key": cache_key,
        }

    cmd = [
        sys.executable,
        str(IMAGE_GEN_CLI),
        "generate",
        "--model",
        model,
        "--prompt",
        prompt,
        "--size",
        size,
        "--quality",
        quality,
        "--out",
        str(raw_path),
        "--force",
    ]
    subprocess.run(cmd, check=True)
    raw_path.replace(final_path)
    make_background_transparent(final_path)
    GENERATED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(final_path, cache_path)
    return {
        "path": relative_to_repo(final_path),
        "bbox": None,
        "anchor_text": query.get("anchor_text"),
        "source": "generated",
        "generation_prompt": prompt,
        "cache_key": cache_key,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("spec")
    parser.add_argument("out_dir")
    parser.add_argument(
        "--asset-mode",
        choices=["extract", "auto", "generate"],
        default="auto",
        help=(
            "auto: generate generatable assets when OPENAI_API_KEY exists, otherwise "
            "fall back to Vision/OpenCV extraction. extract: Vision/OpenCV only. "
            "generate: require generation for generatable assets."
        ),
    )
    parser.add_argument("--update-spec", action="store_true")
    args = parser.parse_args()

    image_path = Path(args.image)
    spec_path = Path(args.spec)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = json.loads(spec_path.read_text())
    queries = spec.get("asset_queries", [])
    if not queries:
        raise SystemExit(f"{spec_path} has no asset_queries")

    img = cv2.imread(str(image_path))
    if img is None:
        raise SystemExit(f"Could not read {image_path}")
    ocr_boxes = None

    assets = dict(spec.get("assets", {})) if isinstance(spec.get("assets"), dict) else {}
    manifest = {"source_size": [int(img.shape[1]), int(img.shape[0])], "assets": {}}
    for query in queries:
        name = query["name"]
        existing_path = existing_asset_path(assets.get(name, {}))
        if existing_path is not None:
            manifest["assets"][name] = assets[name]
            print(f"{name}: reused {existing_path}")
            continue

        if can_generate_asset(query, args.asset_mode):
            try:
                asset = generate_asset(query, out_dir)
                assets[name] = asset
                manifest["assets"][name] = asset
                print(f"{name}: generated")
                continue
            except Exception as exc:
                if args.asset_mode == "generate":
                    raise
                print(f"warning: generation failed for {name}; falling back to Vision extraction: {exc}", file=sys.stderr)

        direct_bbox = query_bbox_crop(query, img)
        if direct_bbox is not None:
            x, y, w, h = direct_bbox
            crop = img[y:y + h, x:x + w]
            path = out_dir / f"{safe_name(name)}.png"
            cv2.imwrite(str(path), crop)
            if query.get("transparent", True):
                make_background_transparent(path)

            asset = {
                "path": relative_to_repo(path),
                "bbox": [int(x), int(y), int(w), int(h)],
                "source": "bbox_crop",
            }
            assets[name] = asset
            manifest["assets"][name] = asset
            print(f"{name}: bbox={asset['bbox']}")
            continue

        anchor_text = query.get("anchor_text")
        if not anchor_text:
            print(f"warning: no anchor_text or bbox for asset {name}", file=sys.stderr)
            continue
        if ocr_boxes is None:
            ocr_boxes = ocr_text(image_path)
        rule = query.get("crop_rule", "nearest_icon_left")
        match = match_anchor(anchor_text, ocr_boxes, img, query.get("prefer"))
        if match is None:
            print(f"warning: no OCR match for asset {name}: {anchor_text}", file=sys.stderr)
            continue
        bbox = crop_asset(img, match["bbox"], rule)
        if bbox is None:
            print(f"warning: no crop found for asset {name}: {anchor_text}", file=sys.stderr)
            continue

        x, y, w, h = bbox
        crop = img[y:y + h, x:x + w]
        path = out_dir / f"{safe_name(name)}.png"
        cv2.imwrite(str(path), crop)
        if query.get("transparent", True):
            make_background_transparent(path)

        asset = {
            "path": relative_to_repo(path),
            "bbox": [int(x), int(y), int(w), int(h)],
            "anchor_text": anchor_text,
            "ocr_text": match["ocr_text"],
            "ocr_score": round(match["score"], 3),
            "crop_rule": rule,
        }
        assets[name] = asset
        manifest["assets"][name] = asset
        print(f"{name}: {match['ocr_text']} rule={rule} score={match['score']:.2f} bbox={asset['bbox']}")

    (out_dir / "assets_vision.json").write_text(json.dumps(manifest, indent=2))
    if args.update_spec:
        spec["assets"] = assets
        spec_path.write_text(json.dumps(spec, indent=2) + "\n")


if __name__ == "__main__":
    main()
