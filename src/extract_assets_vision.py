"""Resolve arbitrary slide assets from generation or Vision OCR anchors.

For generic pictograms the preferred flow is:

1. Crop a source reference from the input image using `bbox` or an OCR anchor.
2. Use image generation as a cleanup/reconstruction pass from that source crop.
3. Reuse the generated icon library when the same source-reference crop has
   already been generated.
4. Fail loudly if a generic pictogram cannot be generated. Source crops are not
   acceptable final output for generic icons.

Vision extraction is still used to crop source references for generation and to
crop non-generic raster assets. It is not used as final output for generic
pictograms.

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
import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional until description mode is used
    OpenAI = None

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
GENERATED_ICON_LIBRARY_DIR = Path(os.getenv("SLIDEGEN_ICON_LIBRARY_DIR", "extracted/generated_icon_library"))
DEFAULT_ICON_PROMPT_VERSION = "v7"
CONTAINER_ICON_PROMPT_VERSION = "v8"
DESCRIPTION_ICON_PROMPT_VERSION = "v4"
DEFAULT_ICON_GENERATION_INPUT = "description"
CHROMA_KEY_CANDIDATES = ((255, 0, 255), (0, 255, 0), (255, 234, 0), (0, 255, 255))
CHROMA_BORDER_DELTA = 80
CHROMA_REMOVE_DELTA = 128
CHROMA_INTERNAL_REMOVE_DELTA = 80
CHROMA_ARTWORK_DELTA = 72
ICON_GENERATION_ATTEMPTS = 2
ICON_SOURCE_MATTE_ATTEMPTS = 1
DEFAULT_ICON_ARTWORK_TARGET_COVERAGE = 0.92
CONTAINER_ICON_ARTWORK_TARGET_COVERAGE = 0.94
DEFAULT_ICON_STYLE = "source-matched presentation pictogram"
DEFAULT_ICON_DESCRIBE_MODEL = "gpt-5.5"
ICON_DESCRIPTION_ATTEMPTS = 3
REGENERATION_QUERY_FIELDS = (
    "regenerate",
    "force_regenerate",
    "regeneration_guidance",
    "regeneration_reason",
)


@dataclass(frozen=True)
class CropQuality:
    ok: bool
    reason: str
    metrics: dict[str, float]


@dataclass(frozen=True)
class SourceReference:
    path: Path
    bbox: tuple[int, int, int, int]
    source: str
    image_hash: str
    chroma_key: tuple[int, int, int]
    palette: dict[str, float]


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def slug(value: str) -> str:
    chars = []
    previous_underscore = False
    for char in value.lower():
        if char.isalnum():
            chars.append(char)
            previous_underscore = False
        elif not previous_underscore:
            chars.append("_")
            previous_underscore = True
    parts = "".join(chars).strip("_").split("_")
    return "_".join(part for part in parts[:6] if part)


def semantic_icon(query: dict) -> tuple[str, str]:
    """Return the LLM-provided canonical icon id and human label.

    The spec generator is responsible for semantic normalization. This default
    is deliberately dumb so we do not grow a hidden rules engine here.
    """
    icon_id = slug(str(query.get("icon_id") or query.get("canonical_icon") or query.get("name") or "generic_icon"))
    if icon_id.endswith("_icon"):
        icon_id = icon_id[:-5]
    label = str(query.get("semantic_label") or icon_id.replace("_", " ")).strip()
    return icon_id or "generic_icon", label


def icon_style(query: dict) -> str:
    explicit = str(query.get("icon_style") or query.get("style") or query.get("icon_treatment") or "").strip()
    return explicit or DEFAULT_ICON_STYLE


def bool_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def regeneration_requested(query: dict) -> bool:
    return bool_value(query.get("regenerate")) or bool_value(query.get("force_regenerate"))


def regeneration_guidance(query: dict) -> str:
    return " ".join(str(query.get("regeneration_guidance") or "").split())


def regeneration_guidance_text(query: dict) -> str:
    guidance = regeneration_guidance(query)
    if not guidance:
        return ""
    return f"Visual QA correction guidance for this regeneration: {guidance} "


def regeneration_metadata(query: dict) -> dict[str, Any]:
    if not regeneration_requested(query):
        return {}
    metadata: dict[str, Any] = {"regenerated_from_refinement": True}
    guidance = regeneration_guidance(query)
    if guidance:
        metadata["regeneration_guidance"] = guidance
    reason = " ".join(str(query.get("regeneration_reason") or "").split())
    if reason:
        metadata["regeneration_reason"] = reason
    return metadata


def clear_regeneration_request(spec: dict, name: str) -> None:
    for query in spec.get("asset_queries", []):
        if not isinstance(query, dict):
            continue
        if str(query.get("name") or "") != name and str(query.get("asset") or "") != name:
            continue
        for field in REGENERATION_QUERY_FIELDS:
            query.pop(field, None)
        return


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


def intersection_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0 = max(ax, bx)
    y0 = max(ay, by)
    x1 = min(ax + aw, bx + bw)
    y1 = min(ay + ah, by + bh)
    if x1 <= x0 or y1 <= y0:
        return 0
    return int((x1 - x0) * (y1 - y0))


def foreground_ratio(crop: np.ndarray) -> float:
    if crop.size == 0:
        return 0.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    sat = hsv[..., 1]
    val = hsv[..., 2]
    mask = ((sat > SAT_MIN) | (val < VAL_MAX)).astype(np.uint8)
    return float(mask.mean())


def assess_crop_quality(
    bbox: tuple[int, int, int, int],
    crop: np.ndarray,
    *,
    query: dict,
    img: np.ndarray,
    ocr_boxes: list[OCRBox] | None = None,
) -> CropQuality:
    x, y, w, h = bbox
    area = max(1, w * h)
    metrics = {
        "width": float(w),
        "height": float(h),
        "area_ratio": float(area / max(1, img.shape[0] * img.shape[1])),
        "aspect": float(w / max(1, h)),
        "foreground_ratio": foreground_ratio(crop),
        "ocr_overlap_ratio": 0.0,
    }

    if bool_value(query.get("generatable", False)):
        if w < 12 or h < 12:
            return CropQuality(False, "crop too small for an icon", metrics)
        if w > 220 or h > 220:
            return CropQuality(False, "crop too large for an icon", metrics)
        if metrics["aspect"] < 0.25 or metrics["aspect"] > 4.0:
            return CropQuality(False, "crop aspect ratio is implausible for an icon", metrics)
        if metrics["foreground_ratio"] < 0.01:
            return CropQuality(False, "crop is mostly empty background", metrics)

        if ocr_boxes:
            text_area = sum(intersection_area(bbox, box.bbox) for box in ocr_boxes)
            metrics["ocr_overlap_ratio"] = float(text_area / area)
            if metrics["ocr_overlap_ratio"] > 0.10:
                return CropQuality(False, "crop appears to include text", metrics)

    return CropQuality(True, "ok", metrics)


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


def rgb_to_hex(color: tuple[int, int, int]) -> str:
    return f"#{color[0]:02X}{color[1]:02X}{color[2]:02X}"


def image_data_url(image_path: Path) -> str:
    mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return math.sqrt(sum((left - right) ** 2 for left, right in zip(a, b)))


def color_family_ratios_from_rgb(rgb: np.ndarray, alpha: np.ndarray | None = None) -> dict[str, float]:
    if rgb.size == 0:
        return {}
    rgb = rgb.reshape(-1, 3).astype(np.uint8)
    if alpha is not None:
        alpha_mask = alpha.reshape(-1) > 0
        rgb = rgb[alpha_mask]
    if rgb.size == 0:
        return {}

    hsv = cv2.cvtColor(rgb.reshape(-1, 1, 3), cv2.COLOR_RGB2HSV).reshape(-1, 3)
    hue = hsv[:, 0]
    sat = hsv[:, 1]
    val = hsv[:, 2]
    colored = (sat > 50) & (val > 70)
    colored_count = max(1, int(colored.sum()))
    total_count = max(1, len(rgb))

    families = {
        "red": colored & ((hue <= 10) | (hue >= 170)),
        "orange": colored & (hue > 10) & (hue <= 24),
        "yellow": colored & (hue > 24) & (hue <= 38),
        "green": colored & (hue > 38) & (hue <= 86),
        "cyan": colored & (hue > 86) & (hue <= 102),
        "blue": colored & (hue > 102) & (hue <= 132),
        "purple": colored & (hue > 132) & (hue < 170),
    }
    ratios = {"colored_total": float(colored.sum() / total_count)}
    for name, mask in families.items():
        ratios[name] = float(mask.sum() / colored_count)
        ratios[f"{name}_total"] = float(mask.sum() / total_count)
    return ratios


def color_family_ratios_from_bgr(crop: np.ndarray) -> dict[str, float]:
    if crop.size == 0:
        return {}
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    return color_family_ratios_from_rgb(rgb)


def color_family_for_rgb(color: tuple[int, int, int]) -> str | None:
    rgb = np.array(color, dtype=np.uint8).reshape(1, 1, 3)
    hue, sat, val = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV).reshape(3)
    if sat <= 50 or val <= 70:
        return None
    if hue <= 10 or hue >= 170:
        return "red"
    if hue <= 24:
        return "orange"
    if hue <= 38:
        return "yellow"
    if hue <= 86:
        return "green"
    if hue <= 102:
        return "cyan"
    if hue <= 132:
        return "blue"
    return "purple"


def observed_color_families(palette: dict[str, float], *, threshold: float = 0.04) -> list[str]:
    names = []
    for name in ("red", "orange", "yellow", "green", "cyan", "blue", "purple"):
        if palette.get(name, 0.0) >= threshold and palette.get(f"{name}_total", 0.0) >= 0.003:
            names.append(name)
    return names


def palette_prompt_text(palette: dict[str, float]) -> str:
    observed = observed_color_families(palette)
    if not observed:
        return (
            "Observed source foreground colors are mostly neutral/white; except for the flat chroma-key "
            "background outside the icon, do not introduce new accent colors. "
        )
    blocked = [
        name
        for name in ("red", "orange", "yellow", "green", "cyan", "blue", "purple")
        if name not in observed
    ]
    text = f"Observed source foreground color families: {', '.join(observed)}. "
    if blocked:
        text += (
            "Except for the flat chroma-key background outside the icon, "
            f"do not introduce absent accent colors such as {', '.join(blocked)}. "
        )
    return text


def generated_palette(path: Path) -> dict[str, float]:
    with Image.open(path).convert("RGBA") as image:
        pixels = np.array(image)
    return color_family_ratios_from_rgb(pixels[..., :3], pixels[..., 3])


def validate_generated_palette(path: Path, reference: SourceReference) -> dict[str, Any]:
    source = reference.palette
    generated = generated_palette(path)
    violations = []
    for name in ("red", "orange", "yellow", "green", "cyan", "blue", "purple"):
        source_has = source.get(name, 0.0) >= 0.04 and source.get(f"{name}_total", 0.0) >= 0.003
        generated_has = generated.get(name, 0.0) >= 0.10 and generated.get(f"{name}_total", 0.0) >= 0.015
        if generated_has and not source_has:
            violations.append(name)
    return {
        "source_palette": source,
        "generated_palette": generated,
        "violations": violations,
        "ok": not violations,
    }


def choose_chroma_key_from_crop(crop: np.ndarray) -> tuple[int, int, int]:
    if crop.size == 0:
        return CHROMA_KEY_CANDIDATES[0]
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).reshape(-1, 3)
    if len(rgb) > 12000:
        step = max(1, len(rgb) // 12000)
        rgb = rgb[::step]
    source_colors = [tuple(int(channel) for channel in pixel) for pixel in rgb]
    palette = color_family_ratios_from_bgr(crop)
    observed = set(observed_color_families(palette, threshold=0.02))

    def score(candidate: tuple[int, int, int]) -> tuple[int, int, float]:
        family = color_family_for_rgb(candidate)
        family_present = int(family in observed)
        green_family = int(family == "green")
        distance = min(color_distance(candidate, color) for color in source_colors)
        return (-family_present, -green_family, distance)

    return max(CHROMA_KEY_CANDIDATES, key=score)


def generated_icon_artwork_metrics(path: Path, chroma_key: tuple[int, int, int]) -> dict[str, float]:
    with Image.open(path).convert("RGBA") as image:
        pixels = np.array(image)
    opaque = pixels[..., 3] > 0
    opaque_count = int(opaque.sum())
    if opaque_count == 0:
        return {"opaque_ratio": 0.0, "chroma_key_ratio": 0.0}

    rgb = pixels[..., :3].astype(np.int16)
    delta = np.max(np.abs(rgb - np.array(chroma_key, dtype=np.int16)), axis=2)
    chroma_pixels = opaque & (delta <= CHROMA_ARTWORK_DELTA)
    ys, xs = np.where(opaque)
    alpha_width = int(xs.max() - xs.min() + 1) if xs.size else 0
    alpha_height = int(ys.max() - ys.min() + 1) if ys.size else 0
    return {
        "opaque_ratio": float(opaque.mean()),
        "chroma_key_ratio": float(chroma_pixels.sum() / opaque_count),
        "alpha_bbox_width_ratio": float(alpha_width / pixels.shape[1]) if pixels.shape[1] else 0.0,
        "alpha_bbox_height_ratio": float(alpha_height / pixels.shape[0]) if pixels.shape[0] else 0.0,
    }


def normalize_transparent_padding(path: Path, *, target_coverage: float = 0.90) -> dict[str, Any]:
    """Scale transparent PNG artwork so alpha content fills the canvas consistently."""
    with Image.open(path).convert("RGBA") as image:
        bbox = image.getbbox()
        if bbox is None:
            return {"normalized": False, "reason": "empty_alpha"}
        width, height = image.size
        left, top, right, bottom = bbox
        art_w = right - left
        art_h = bottom - top
        if art_w <= 0 or art_h <= 0:
            return {"normalized": False, "reason": "empty_bbox"}

        current_coverage = max(art_w / width, art_h / height)
        if current_coverage >= target_coverage * 0.98:
            return {
                "normalized": False,
                "alpha_bbox": [left, top, art_w, art_h],
                "alpha_bbox_coverage": float(current_coverage),
            }

        scale = min((width * target_coverage) / art_w, (height * target_coverage) / art_h)
        if scale <= 1.02:
            return {
                "normalized": False,
                "alpha_bbox": [left, top, art_w, art_h],
                "alpha_bbox_coverage": float(current_coverage),
            }

        crop = image.crop(bbox)
        new_w = max(1, min(width, int(round(art_w * scale))))
        new_h = max(1, min(height, int(round(art_h * scale))))
        crop = crop.resize((new_w, new_h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", image.size, (0, 0, 0, 0))
        paste_x = (width - new_w) // 2
        paste_y = (height - new_h) // 2
        canvas.alpha_composite(crop, (paste_x, paste_y))
        canvas.save(path)
        return {
            "normalized": True,
            "alpha_bbox": [left, top, art_w, art_h],
            "alpha_bbox_coverage": float(current_coverage),
            "normalized_bbox": [paste_x, paste_y, new_w, new_h],
            "normalized_coverage": float(max(new_w / width, new_h / height)),
        }


def crop_bytes(crop: np.ndarray) -> bytes:
    ok, encoded = cv2.imencode(".png", crop)
    if not ok:
        raise RuntimeError("Could not encode source reference crop")
    return encoded.tobytes()


def source_reference_for_query(query: dict, out_dir: Path, image_path: Path, img: np.ndarray) -> SourceReference | None:
    bbox = query_bbox_crop(query, img)
    source = "bbox_reference"

    if bbox is None and query.get("anchor_text"):
        ocr_boxes = ocr_text(image_path)
        match = match_anchor(str(query["anchor_text"]), ocr_boxes, img, query.get("prefer"))
        if match is not None:
            rule = query.get("crop_rule", "nearest_icon_left")
            bbox = crop_asset(img, match["bbox"], rule)
            source = "ocr_reference"

    if bbox is None:
        return None

    x, y, w, h = bbox
    crop = img[y:y + h, x:x + w]
    encoded = crop_bytes(crop)
    reference_hash = hashlib.sha256(encoded).hexdigest()[:16]
    out_dir.mkdir(parents=True, exist_ok=True)
    reference_path = out_dir / f"{safe_name(query['name'])}_source_reference.png"
    reference_path.write_bytes(encoded)
    return SourceReference(
        path=reference_path,
        bbox=bbox,
        source=source,
        image_hash=reference_hash,
        chroma_key=choose_chroma_key_from_crop(crop),
        palette=color_family_ratios_from_bgr(crop),
    )


def iter_spec_elements(spec: dict) -> list[dict]:
    if spec.get("layout") == "generic_deck":
        elements: list[dict] = []
        for child in spec.get("slides", []):
            if isinstance(child, dict):
                elements.extend(element for element in child.get("elements", []) if isinstance(element, dict))
        return elements
    return [element for element in spec.get("elements", []) if isinstance(element, dict)]


def annotate_queries_with_render_context(spec: dict, queries: list[dict]) -> list[dict]:
    """Tell image generation when the renderer already owns an icon container."""
    elements = iter_spec_elements(spec)
    shapes = [
        element
        for element in elements
        if element.get("type") == "shape"
        and element.get("shape") in {"ellipse", "round_rect", "rect"}
        and isinstance(element.get("bbox"), list)
        and len(element["bbox"]) == 4
    ]
    icon_elements = {
        str(element.get("asset")): element
        for element in elements
        if element.get("type") == "icon" and element.get("asset") and isinstance(element.get("bbox"), list)
    }
    annotated = []
    for query in queries:
        item = dict(query)
        icon = icon_elements.get(str(item.get("name")))
        if not icon:
            annotated.append(item)
            continue
        ix, iy, iw, ih = [float(value) for value in icon["bbox"]]
        if iw <= 0 or ih <= 0:
            annotated.append(item)
            continue
        cx = ix + iw / 2
        cy = iy + ih / 2
        containing_shapes = []
        for shape in shapes:
            sx, sy, sw, sh = [float(value) for value in shape["bbox"]]
            if sw < iw * 1.15 or sh < ih * 1.15:
                continue
            if sw > iw * 2.4 or sh > ih * 2.4:
                continue
            shape_cx = sx + sw / 2
            shape_cy = sy + sh / 2
            if abs(shape_cx - cx) > sw * 0.25 or abs(shape_cy - cy) > sh * 0.25:
                continue
            if sx <= cx <= sx + sw and sy <= cy <= sy + sh:
                containing_shapes.append((sw * sh, shape))
        if containing_shapes:
            _, container = min(containing_shapes, key=lambda value: value[0])
            item["renderer_draws_container"] = True
            item["container_bbox"] = [int(round(value)) for value in container["bbox"]]
            item["container_shape"] = str(container.get("shape") or "shape")
        annotated.append(item)
    return annotated


def make_chroma_key_transparent(path: Path, chroma_key: tuple[int, int, int]) -> None:
    """Remove generated icon chroma backgrounds without touching source crops."""
    with Image.open(path).convert("RGBA") as image:
        pixels = np.array(image)

    rgb = pixels[..., :3].astype(np.int16)
    remove = np.zeros(pixels.shape[:2], dtype=bool)
    border_rgb = np.concatenate(
        [
            rgb[0, :, :],
            rgb[-1, :, :],
            rgb[:, 0, :],
            rgb[:, -1, :],
        ],
        axis=0,
    )

    key = np.array(chroma_key, dtype=np.int16)
    border_delta = np.max(np.abs(border_rgb - key), axis=1)
    if float(np.mean(border_delta <= CHROMA_BORDER_DELTA)) >= 0.20:
        delta = np.max(np.abs(rgb - key), axis=2)
        key_mask = (delta <= CHROMA_REMOVE_DELTA).astype(np.uint8)
        num_labels, labels = cv2.connectedComponents(key_mask)
        if num_labels > 1:
            edge_labels = np.unique(
                np.concatenate(
                    [
                        labels[0, :],
                        labels[-1, :],
                        labels[:, 0],
                        labels[:, -1],
                    ]
                )
            )
            edge_labels = edge_labels[edge_labels != 0]
            if edge_labels.size:
                remove |= np.isin(labels, edge_labels)

    if not remove.any():
        for fallback in CHROMA_KEY_CANDIDATES:
            key = np.array(fallback, dtype=np.int16)
            border_delta = np.max(np.abs(border_rgb - key), axis=1)
            if float(np.mean(border_delta <= CHROMA_BORDER_DELTA)) < 0.20:
                continue
            delta = np.max(np.abs(rgb - key), axis=2)
            key_mask = (delta <= CHROMA_REMOVE_DELTA).astype(np.uint8)
            num_labels, labels = cv2.connectedComponents(key_mask)
            if num_labels <= 1:
                continue
            edge_labels = np.unique(
                np.concatenate(
                    [
                        labels[0, :],
                        labels[-1, :],
                        labels[:, 0],
                        labels[:, -1],
                    ]
                )
            )
            edge_labels = edge_labels[edge_labels != 0]
            if edge_labels.size:
                remove |= np.isin(labels, edge_labels)
                break

    if remove.any():
        pixels[remove, :3] = 0
        pixels[remove, 3] = 0
    else:
        make_background_transparent(path)
        with Image.open(path).convert("RGBA") as image:
            pixels = np.array(image)

    key = np.array(chroma_key, dtype=np.int16)
    delta = np.max(np.abs(pixels[..., :3].astype(np.int16) - key), axis=2)
    internal_key = (pixels[..., 3] > 0) & (delta <= CHROMA_INTERNAL_REMOVE_DELTA)
    if internal_key.any():
        pixels[internal_key, :3] = 0
        pixels[internal_key, 3] = 0
    Image.fromarray(pixels).save(path)


def background_guidance_for(reference: SourceReference, *, source_matte: bool = False) -> str:
    chroma_hex = rgb_to_hex(reference.chroma_key)
    if source_matte:
        return (
            "Use the same flat source-matched matte/background visible around the icon in the reference crop. "
            "That matte may remain in the final asset; do not use an artificial chroma-key background. "
        )
    return (
        f"Use a perfectly flat {chroma_hex} chroma-key background everywhere outside the icon artwork. "
        "Do not use the chroma-key color inside the icon artwork."
    )


def describe_icon_reference(query: dict, reference: SourceReference) -> str:
    if OpenAI is None:
        raise RuntimeError("openai package is required for description-based icon generation")
    icon_id, label = semantic_icon(query)
    model = os.environ.get("OPENAI_ICON_DESCRIBE_MODEL") or os.environ.get("OPENAI_SPEC_MODEL") or DEFAULT_ICON_DESCRIBE_MODEL
    container_context = ""
    if bool_value(query.get("renderer_draws_container", False)):
        container_context = (
            "The slide renderer already draws the outer icon badge/container. "
            "Describe the source-owned foreground artwork inside that outer container. "
            "Treat any surrounding outer circle, halo, badge, matte, crop edge, or page background as context, "
            "not artwork to reproduce. If a smaller centered colored disk or filled shape carries the pictogram "
            "inside the crop, describe it as part of the icon artwork because it is not the outer renderer-owned badge. "
        )
    prompt = (
        "Describe this small source icon for a separate image-generation model. "
        "Return one concise paragraph, no JSON. Describe only visible visual facts: subject, shapes, "
        "stroke/fill style, intentional badge or circular container if present, color families, "
        "line weight, and approximate composition. Treat square crop boundaries, page background, "
        "and incidental matte as context, not icon artwork; describe background only when it is "
        "clearly a designed icon container. Do not infer brand logos or add text. "
        f"{container_context}"
        f"Canonical icon id: {icon_id}. Intended subject: {label}. "
        f"Observed spec style hint: {icon_style(query)}. "
        f"{regeneration_guidance_text(query)}"
    )
    client = OpenAI()
    for attempt in range(1, ICON_DESCRIPTION_ATTEMPTS + 1):
        attempt_prompt = prompt
        if attempt > 1:
            attempt_prompt += " The previous attempt returned empty text. Return one concise non-empty paragraph."
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": attempt_prompt},
                        {"type": "input_image", "image_url": image_data_url(reference.path), "detail": "high"},
                    ],
                }
            ],
            text={"verbosity": "low"},
            max_output_tokens=500,
        )
        description = response.output_text.strip()
        if description:
            return description
        print(
            f"warning: icon description model returned empty text for {query.get('name')} "
            f"(attempt {attempt}/{ICON_DESCRIPTION_ATTEMPTS})",
            file=sys.stderr,
        )
    raise RuntimeError("icon description model returned empty text after retries")


def generation_prompt(query: dict, reference: SourceReference, *, source_matte: bool = False) -> str:
    icon_id, label = semantic_icon(query)
    container_guidance = ""
    if bool_value(query.get("renderer_draws_container", False)):
        container_guidance = (
            "The editable slide renderer already draws the surrounding badge/container "
            f"({query.get('container_shape', 'shape')} at {query.get('container_bbox')}). "
            "Do not include that outer renderer-owned container, page background, crop edge, panel, or matte in the asset. "
            "Preserve source-owned inner artwork visible inside the icon crop, including a centered colored disk or filled "
            "shape when it is part of the original icon treatment. "
            "Scale the source-owned icon artwork so it fills the transparent canvas with balanced padding. "
        )
    return (
        "Recreate the pictogram/icon shown in the attached source crop as a clean reusable presentation asset. "
        f"Canonical icon id: {icon_id}. Subject: {label}. "
        "The attached crop is the visual authority: preserve its colors, stroke weight, fill style, proportions, and visual density. "
        f"{regeneration_guidance_text(query)}"
        f"{palette_prompt_text(reference.palette)}"
        f"{container_guidance}"
        "If no separate renderer-owned container is provided, preserve the source crop's visible icon container when it is part of the icon artwork. "
        "If the crop includes nearby labels, text, page background, crop edges, or noise, ignore those artifacts and keep only the icon artwork. "
        "Do not invent a different palette, design system, badge color, or icon style. "
        "No readable words, letters, numbers, brand marks, logos, watermark, UI text, or shadow. "
        f"{background_guidance_for(reference, source_matte=source_matte)}"
    )


def description_generation_prompt(
    query: dict,
    reference: SourceReference,
    description: str,
    *,
    source_matte: bool = False,
) -> str:
    icon_id, label = semantic_icon(query)
    container_guidance = ""
    if bool_value(query.get("renderer_draws_container", False)):
        container_guidance = (
            "The editable slide renderer already draws the surrounding badge/container "
            f"({query.get('container_shape', 'shape')} at {query.get('container_bbox')}). "
            "Do not include that outer renderer-owned container, page background, crop edge, panel, or matte in the asset. "
            "Preserve source-owned inner artwork from the description, including a centered colored disk or filled shape "
            "when it carries the icon's original visual treatment. "
            "Scale the source-owned icon artwork so it fills the transparent canvas with balanced padding. "
        )
    preserve_guidance = (
        "Preserve the described visual treatment, colors, proportions, and visual density. "
        if bool_value(query.get("renderer_draws_container", False))
        else "Preserve the described visual treatment, colors, proportions, intentional badge/container if described, and visual density. "
    )
    return (
        "Create a clean reusable presentation icon from this visual description. "
        f"Canonical icon id: {icon_id}. Subject: {label}. "
        f"Visual description from the source icon: {description} "
        f"{regeneration_guidance_text(query)}"
        f"{palette_prompt_text(reference.palette)}"
        f"{container_guidance}"
        f"{preserve_guidance}"
        "Do not preserve square crop boundaries, page background, or incidental matte as artwork. "
        "Do not add readable words, letters, numbers, brand marks, logos, watermark, UI text, or shadow. "
        f"{background_guidance_for(reference, source_matte=source_matte)}"
    )


def icon_prompt_version(query: dict, generation_input: str = DEFAULT_ICON_GENERATION_INPUT) -> str:
    if generation_input == "description":
        base = f"{DEFAULT_ICON_PROMPT_VERSION}_desc_{DESCRIPTION_ICON_PROMPT_VERSION}"
        if bool_value(query.get("renderer_draws_container", False)):
            return f"{base}_container"
        return base
    if bool_value(query.get("renderer_draws_container", False)):
        return CONTAINER_ICON_PROMPT_VERSION
    return DEFAULT_ICON_PROMPT_VERSION


def generated_library_paths(icon_id: str, reference_hash: str, prompt_version: str) -> tuple[Path, Path, Path]:
    library_dir = GENERATED_ICON_LIBRARY_DIR / "source_reference" / safe_name(icon_id)
    library_stem = f"{reference_hash}__{prompt_version}"
    return library_dir, library_dir / f"{library_stem}.png", library_dir / f"{library_stem}.json"


def icon_artwork_target_coverage(query: dict) -> float:
    if bool_value(query.get("renderer_draws_container", False)):
        return CONTAINER_ICON_ARTWORK_TARGET_COVERAGE
    return DEFAULT_ICON_ARTWORK_TARGET_COVERAGE


def can_generate_asset(
    query: dict,
    mode: str,
    reference: SourceReference | None,
    generation_input: str = DEFAULT_ICON_GENERATION_INPUT,
) -> bool:
    if mode == "extract":
        return False
    if not bool_value(query.get("generatable", False)):
        return False
    if reference is None:
        return False
    if mode == "generate":
        return True
    icon_id, _ = semantic_icon(query)
    _, library_path, _ = generated_library_paths(icon_id, reference.image_hash, icon_prompt_version(query, generation_input))
    if library_path.exists():
        return True
    return bool(os.getenv("OPENAI_API_KEY"))


def generate_asset(
    query: dict,
    out_dir: Path,
    reference: SourceReference,
    *,
    generation_input: str = DEFAULT_ICON_GENERATION_INPUT,
) -> dict | None:
    raw_path = out_dir / f"{safe_name(query['name'])}_generated_raw.png"
    final_path = out_dir / f"{safe_name(query['name'])}.png"
    icon_description = describe_icon_reference(query, reference) if generation_input == "description" else None
    prompt = (
        description_generation_prompt(query, reference, icon_description)
        if icon_description
        else generation_prompt(query, reference)
    )
    model = query.get("generation_model", "gpt-image-2")
    size = query.get("generation_size", "1024x1024")
    quality = query.get("generation_quality", "medium")
    icon_id, label = semantic_icon(query)
    style = icon_style(query)
    chroma_hex = rgb_to_hex(reference.chroma_key)
    prompt_version = icon_prompt_version(query, generation_input)
    force_regenerate = regeneration_requested(query)
    regen_metadata = regeneration_metadata(query)
    cache_key = hashlib.sha256(
        json.dumps(
            {
                "model": model,
                "size": size,
                "quality": quality,
                "icon_id": icon_id,
                "style": style,
                "reference_hash": reference.image_hash,
                "reference_palette": reference.palette,
                "generation_input": generation_input,
                "icon_description": icon_description,
                "renderer_draws_container": bool_value(query.get("renderer_draws_container", False)),
                "regeneration_guidance": regeneration_guidance(query),
                "prompt_version": prompt_version,
                "prompt": prompt,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    library_dir, library_path, manifest_path = generated_library_paths(icon_id, reference.image_hash, prompt_version)
    if library_path.exists() and not force_regenerate:
        shutil.copyfile(library_path, final_path)
        make_chroma_key_transparent(final_path, reference.chroma_key)
        padding_metrics = normalize_transparent_padding(
            final_path,
            target_coverage=icon_artwork_target_coverage(query),
        )
        artwork_metrics = generated_icon_artwork_metrics(final_path, reference.chroma_key)
        artwork_metrics["padding_normalization"] = padding_metrics
        palette_check = validate_generated_palette(final_path, reference)
        artwork_metrics["palette_check"] = palette_check
        if palette_check["ok"]:
            shutil.copyfile(final_path, library_path)
            return {
                "path": relative_to_repo(final_path),
                "bbox": None,
                "anchor_text": query.get("anchor_text"),
                "source": "generated_icon_library",
                "icon_id": icon_id,
                "icon_style": style,
                "semantic_label": label,
                "generation_prompt": prompt,
                "prompt_version": prompt_version,
                "source_reference": relative_to_repo(reference.path),
                "source_reference_hash": reference.image_hash,
                "source_reference_bbox": [int(value) for value in reference.bbox],
                "source_reference_type": reference.source,
                "generation_input": generation_input,
                "icon_description": icon_description,
                "renderer_draws_container": bool_value(query.get("renderer_draws_container", False)),
                "container_bbox": query.get("container_bbox"),
                "container_shape": query.get("container_shape"),
                "chroma_key": chroma_hex,
                "source_palette": reference.palette,
                "cache_key": cache_key,
                "generation_metrics": artwork_metrics,
                "library_reused": True,
                **regen_metadata,
            }
        print(
            "warning: cached generated icon introduced absent source color families; regenerating "
            f"{query['name']}: {', '.join(palette_check['violations'])}",
            file=sys.stderr,
        )
        final_path.unlink(missing_ok=True)
        library_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is required to generate a missing icon")
    if not IMAGE_GEN_CLI.exists():
        raise RuntimeError(f"Image generation CLI not found: {IMAGE_GEN_CLI}")

    cmd = [sys.executable, str(IMAGE_GEN_CLI)]
    if generation_input == "description":
        cmd.append("generate")
    else:
        cmd.extend(["edit", "--image", str(reference.path)])
    cmd.extend(
        [
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
    )
    last_palette_check: dict[str, Any] | None = None
    strategies = ["chroma"] * ICON_GENERATION_ATTEMPTS + ["source_matte"] * ICON_SOURCE_MATTE_ATTEMPTS
    background_mode = "chroma"
    for attempt, strategy in enumerate(strategies, start=1):
        source_matte = strategy == "source_matte"
        background_mode = strategy
        attempt_prompt = (
            description_generation_prompt(query, reference, icon_description, source_matte=source_matte)
            if icon_description
            else generation_prompt(query, reference, source_matte=source_matte)
        )
        if last_palette_check and last_palette_check.get("violations"):
            attempt_prompt = (
                attempt_prompt
                + " Previous generated attempt introduced absent source color families: "
                + ", ".join(str(item) for item in last_palette_check["violations"])
                + ". Regenerate using only the colors visible in the source crop."
            )
        cmd[cmd.index("--prompt") + 1] = attempt_prompt
        subprocess.run(cmd, check=True)
        raw_path.replace(final_path)
        if not source_matte:
            make_chroma_key_transparent(final_path, reference.chroma_key)
        padding_metrics = normalize_transparent_padding(
            final_path,
            target_coverage=icon_artwork_target_coverage(query),
        )
        artwork_metrics = generated_icon_artwork_metrics(final_path, reference.chroma_key)
        artwork_metrics["padding_normalization"] = padding_metrics
        artwork_metrics["background_mode"] = background_mode
        palette_check = validate_generated_palette(final_path, reference)
        artwork_metrics["palette_check"] = palette_check
        if palette_check["ok"]:
            break
        last_palette_check = palette_check
        final_path.unlink(missing_ok=True)
        if attempt == len(strategies):
            raise RuntimeError(
                f"generated icon introduced absent source color families: {', '.join(palette_check['violations'])}"
            )
    library_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(final_path, library_path)
    manifest_path.write_text(
        json.dumps(
            {
                "icon_id": icon_id,
                "icon_style": style,
                "semantic_label": label,
                "model": model,
                "size": size,
                "quality": quality,
                "prompt_version": prompt_version,
                "source_reference_hash": reference.image_hash,
                "source_reference_bbox": [int(value) for value in reference.bbox],
                "source_reference_type": reference.source,
                "source_palette": reference.palette,
                "generation_input": generation_input,
                "icon_description": icon_description,
                "renderer_draws_container": bool_value(query.get("renderer_draws_container", False)),
                "container_bbox": query.get("container_bbox"),
                "container_shape": query.get("container_shape"),
                "regeneration_guidance": regeneration_guidance(query),
                "chroma_key": chroma_hex,
                "background_mode": background_mode,
                "generation_metrics": artwork_metrics,
                "cache_key": cache_key,
                "generation_prompt": prompt,
            },
            indent=2,
        )
        + "\n"
    )
    return {
        "path": relative_to_repo(final_path),
        "bbox": None,
        "anchor_text": query.get("anchor_text"),
        "source": "generated_icon_library",
        "icon_id": icon_id,
        "icon_style": style,
        "semantic_label": label,
        "generation_prompt": prompt,
        "prompt_version": prompt_version,
        "source_reference": relative_to_repo(reference.path),
        "source_reference_hash": reference.image_hash,
        "source_reference_bbox": [int(value) for value in reference.bbox],
        "source_reference_type": reference.source,
        "source_palette": reference.palette,
        "generation_input": generation_input,
        "icon_description": icon_description,
        "renderer_draws_container": bool_value(query.get("renderer_draws_container", False)),
        "container_bbox": query.get("container_bbox"),
        "container_shape": query.get("container_shape"),
        "chroma_key": chroma_hex,
        "background_mode": background_mode,
        "generation_metrics": artwork_metrics,
        "cache_key": cache_key,
        "library_reused": False,
        **regen_metadata,
    }


def refresh_generated_asset(
    asset: dict,
    query: dict,
    path: Path,
    reference: SourceReference,
    *,
    generation_input: str,
) -> dict | None:
    """Re-run deterministic cleanup/metrics on an existing generated asset."""
    icon_id, label = semantic_icon(query)
    style = icon_style(query)
    prompt_version = icon_prompt_version(query, generation_input)
    make_chroma_key_transparent(path, reference.chroma_key)
    padding_metrics = normalize_transparent_padding(
        path,
        target_coverage=icon_artwork_target_coverage(query),
    )
    artwork_metrics = generated_icon_artwork_metrics(path, reference.chroma_key)
    artwork_metrics["padding_normalization"] = padding_metrics
    palette_check = validate_generated_palette(path, reference)
    artwork_metrics["palette_check"] = palette_check
    if not palette_check["ok"]:
        print(
            "warning: existing generated icon still introduced absent source color families; regenerating "
            f"{query['name']}: {', '.join(palette_check['violations'])}",
            file=sys.stderr,
        )
        return None

    _, library_path, _ = generated_library_paths(icon_id, reference.image_hash, prompt_version)
    library_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, library_path)
    refreshed = dict(asset)
    refreshed.update(
        {
            "path": relative_to_repo(path),
            "bbox": None,
            "anchor_text": query.get("anchor_text"),
            "source": "generated_icon_library",
            "icon_id": icon_id,
            "icon_style": style,
            "semantic_label": label,
            "prompt_version": prompt_version,
            "source_reference": relative_to_repo(reference.path),
            "source_reference_hash": reference.image_hash,
            "source_reference_bbox": [int(value) for value in reference.bbox],
            "source_reference_type": reference.source,
            "source_palette": reference.palette,
            "generation_input": generation_input,
            "renderer_draws_container": bool_value(query.get("renderer_draws_container", False)),
            "container_bbox": query.get("container_bbox"),
            "container_shape": query.get("container_shape"),
            "chroma_key": rgb_to_hex(reference.chroma_key),
            "generation_metrics": artwork_metrics,
            "library_reused": True,
        }
    )
    return refreshed


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
            "auto/generate: require generated-library output for generatable icons. "
            "extract: crop only non-generatable raster assets; generatable icons fail."
        ),
    )
    parser.add_argument(
        "--icon-generation-input",
        choices=["source", "description"],
        default=DEFAULT_ICON_GENERATION_INPUT,
        help="description: describe source crop first, then generate from text; source: edit from source crop",
    )
    parser.add_argument(
        "--only-assets",
        default=None,
        help="comma-separated asset names to process; other existing assets are left unchanged",
    )
    parser.add_argument("--update-spec", action="store_true")
    args = parser.parse_args()

    image_path = Path(args.image)
    spec_path = Path(args.spec)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = json.loads(spec_path.read_text())
    queries = annotate_queries_with_render_context(spec, spec.get("asset_queries", []))
    if not queries:
        raise SystemExit(f"{spec_path} has no asset_queries")

    img = cv2.imread(str(image_path))
    if img is None:
        raise SystemExit(f"Could not read {image_path}")
    ocr_boxes = None

    assets = dict(spec.get("assets", {})) if isinstance(spec.get("assets"), dict) else {}
    only_assets = {item.strip() for item in args.only_assets.split(",") if item.strip()} if args.only_assets else None
    manifest = {"source_size": [int(img.shape[1]), int(img.shape[0])], "assets": {}}
    for query in queries:
        name = query["name"]
        if only_assets is not None and name not in only_assets:
            if name in assets:
                manifest["assets"][name] = assets[name]
            continue
        is_generatable = bool_value(query.get("generatable", False))
        force_regenerate = is_generatable and regeneration_requested(query)
        existing_asset = assets.get(name, {})
        existing_path = existing_asset_path(existing_asset)
        if force_regenerate:
            assets.pop(name, None)
            print(f"{name}: refinement requested regeneration")
        elif existing_path is not None:
            existing_generation_input = existing_asset.get("generation_input") or "source"
            expected_prompt_version = icon_prompt_version(query, args.icon_generation_input)
            existing_prompt_version = existing_asset.get("prompt_version")
            if is_generatable and (
                existing_asset.get("source") != "generated_icon_library"
                or existing_generation_input != args.icon_generation_input
                or existing_prompt_version != expected_prompt_version
            ):
                print(
                    f"warning: ignoring existing asset for final-quality icon {name}: "
                    f"source={existing_asset.get('source')} "
                    f"generation_input={existing_generation_input} "
                    f"prompt_version={existing_prompt_version}",
                    file=sys.stderr,
                )
                assets.pop(name, None)
            else:
                if is_generatable:
                    reference = source_reference_for_query(query, out_dir, image_path, img)
                    if reference is None:
                        raise RuntimeError(f"source reference crop is required to refresh generated icon {name}")
                    refreshed_asset = refresh_generated_asset(
                        existing_asset,
                        query,
                        existing_path,
                        reference,
                        generation_input=args.icon_generation_input,
                    )
                    if refreshed_asset is not None:
                        assets[name] = refreshed_asset
                        manifest["assets"][name] = refreshed_asset
                        print(f"{name}: refreshed {existing_path}")
                        continue
                    assets.pop(name, None)
                    existing_path.unlink(missing_ok=True)
                else:
                    manifest["assets"][name] = assets[name]
                    print(f"{name}: reused {existing_path}")
                    continue

        if name in assets and not is_generatable:
            existing_path = existing_asset_path(assets[name])
            if existing_path is not None:
                manifest["assets"][name] = assets[name]
                print(f"{name}: reused {existing_path}")
                continue

        if is_generatable:
            if args.asset_mode == "extract":
                raise RuntimeError(f"asset-mode extract cannot produce final-quality generated icon {name}")
            reference = source_reference_for_query(query, out_dir, image_path, img)
            if reference is None:
                raise RuntimeError(f"source reference crop is required to generate final-quality icon {name}")
            asset = generate_asset(query, out_dir, reference, generation_input=args.icon_generation_input)
            assets[name] = asset
            manifest["assets"][name] = assets[name]
            if force_regenerate:
                clear_regeneration_request(spec, name)
            action = "regenerated" if force_regenerate else "reused generated icon" if asset.get("library_reused") else "generated"
            print(f"{name}: {action}")
            continue

        direct_bbox = query_bbox_crop(query, img)
        icon_id, icon_label = semantic_icon(query)
        style = icon_style(query)
        if direct_bbox is not None:
            x, y, w, h = direct_bbox
            crop = img[y:y + h, x:x + w]
            if bool_value(query.get("generatable", False)) and ocr_boxes is None:
                try:
                    ocr_boxes = ocr_text(image_path)
                except Exception as exc:  # noqa: BLE001 - quality text check is best effort
                    print(f"warning: OCR unavailable for crop quality check: {exc}", file=sys.stderr)
            quality = assess_crop_quality(direct_bbox, crop, query=query, img=img, ocr_boxes=ocr_boxes)
            if not quality.ok:
                print(f"warning: rejected bbox crop for {name}: {quality.reason}", file=sys.stderr)
                if not query.get("anchor_text"):
                    continue
            else:
                path = out_dir / f"{safe_name(name)}.png"
                cv2.imwrite(str(path), crop)
                if bool_value(query.get("transparent", True)):
                    make_background_transparent(path)

                asset = {
                    "path": relative_to_repo(path),
                    "bbox": [int(x), int(y), int(w), int(h)],
                    "source": "bbox_crop",
                    "icon_id": icon_id,
                    "icon_style": style,
                    "semantic_label": icon_label,
                    "crop_quality": quality.reason,
                    "crop_metrics": quality.metrics,
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
        quality = assess_crop_quality(bbox, crop, query=query, img=img, ocr_boxes=ocr_boxes)
        if not quality.ok:
            print(f"warning: rejected OCR crop for {name}: {quality.reason}", file=sys.stderr)
            continue
        path = out_dir / f"{safe_name(name)}.png"
        cv2.imwrite(str(path), crop)
        if bool_value(query.get("transparent", True)):
            make_background_transparent(path)

        asset = {
            "path": relative_to_repo(path),
            "bbox": [int(x), int(y), int(w), int(h)],
            "source": "ocr_crop",
            "anchor_text": anchor_text,
            "ocr_text": match["ocr_text"],
            "ocr_score": round(match["score"], 3),
            "crop_rule": rule,
            "icon_id": icon_id,
            "icon_style": style,
            "semantic_label": icon_label,
            "crop_quality": quality.reason,
            "crop_metrics": quality.metrics,
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
