"""Lightweight spec verification for generated slide specs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def center(bbox):
    x, y, w, h = bbox
    return x + w / 2, y + h / 2


def contains(bbox, point, pad=8):
    x, y, w, h = bbox
    px, py = point
    return x - pad <= px <= x + w + pad and y - pad <= py <= y + h + pad


def logo_refs(spec):
    refs = []
    for layer in spec.get("layers", []):
        if not isinstance(layer, dict):
            continue
        container = layer.get("bbox")
        if not container:
            continue
        for logo in layer.get("logos", []):
            refs.append((logo, layer.get("name", "layer"), container))

    panel = spec.get("saas_panel", {})
    if isinstance(panel, dict) and panel.get("bbox"):
        for logo in panel.get("logos", []):
            refs.append((logo, panel.get("title", "saas_panel"), panel["bbox"]))
    return refs


def generic_specs(spec):
    if spec.get("layout") != "generic_deck":
        return [spec]
    shared_assets = spec.get("assets", {})
    slides = []
    for child in spec.get("slides", []):
        if not isinstance(child, dict):
            continue
        merged = dict(child)
        merged["assets"] = {**shared_assets, **child.get("assets", {})}
        slides.append(merged)
    return slides


def generic_asset_refs(spec):
    refs = []
    for slide_spec in generic_specs(spec):
        for element in slide_spec.get("elements", []):
            if not isinstance(element, dict):
                continue
            if element.get("type") in {"image", "icon"} and element.get("asset"):
                refs.append((element["asset"], slide_spec.get("slide", "slide"), element.get("type")))
    return refs


def verify(spec: dict) -> list[str]:
    warnings = []
    declared = {item.get("name") for item in spec.get("logo_assets", []) if isinstance(item, dict)}
    declared_assets = {item.get("name") for item in spec.get("asset_queries", []) if isinstance(item, dict)}
    assets = spec.get("assets", {})
    seen_refs = {}

    for name, slide_name, element_type in generic_asset_refs(spec):
        if element_type == "icon":
            continue
        if name not in declared_assets and name not in declared:
            warnings.append(f"{element_type} asset `{name}` is referenced by `{slide_name}` but not declared")
        if name not in assets:
            warnings.append(f"{element_type} asset `{name}` is referenced by `{slide_name}` but has no generated/extracted asset")

    for name, container_name, container_bbox in logo_refs(spec):
        seen_refs.setdefault(name, []).append(container_name)
        if name not in declared:
            warnings.append(f"logo `{name}` is referenced by `{container_name}` but missing from logo_assets")
        asset = assets.get(name)
        if not asset:
            warnings.append(f"logo `{name}` is referenced by `{container_name}` but has no extracted asset")
            continue
        asset_bbox = asset.get("bbox")
        if asset_bbox and not contains(container_bbox, center(asset_bbox)):
            warnings.append(
                f"logo `{name}` asset bbox {asset_bbox} falls outside `{container_name}` bbox {container_bbox}; "
                "renderer will slot-place the crop instead of source-positioning it"
            )

    for name, containers in seen_refs.items():
        unique = list(dict.fromkeys(containers))
        if len(unique) > 1:
            warnings.append(f"logo `{name}` is reused in multiple containers: {', '.join(unique)}")

    return warnings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec")
    args = parser.parse_args()

    spec = json.loads(Path(args.spec).read_text())
    warnings = verify(spec)
    if not warnings:
        print("spec verification: OK")
        return
    print("spec verification warnings:")
    for warning in warnings:
        print(f"- {warning}")


if __name__ == "__main__":
    main()
