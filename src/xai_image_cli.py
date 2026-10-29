#!/usr/bin/env python3
"""Generate or edit an image with grok-imagine-image-2.0 and save it as PNG.

CLI-compatible with the old imagegen script used by extract_assets_vision.py:
  generate --model M --prompt P [--size S] [--quality Q] --out PATH [--force]
  edit --image IN --model M --prompt P [--size S] [--quality Q] --out PATH [--force]
--size/--quality are accepted for compatibility; xAI picks the resolution.
"""
from __future__ import annotations

import argparse
import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from xai_client import DEFAULT_IMAGE_MODEL, xai_client  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["generate", "edit"])
    parser.add_argument("--image")
    parser.add_argument("--model", default=DEFAULT_IMAGE_MODEL)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--size")
    parser.add_argument("--quality")
    parser.add_argument("--out", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"{out} exists (use --force)", file=sys.stderr)
        return 1
    model = args.model if args.model.startswith("grok-") else DEFAULT_IMAGE_MODEL
    client = xai_client()
    if args.mode == "edit":
        if not args.image:
            parser.error("--image is required for edit")
        data = base64.b64encode(Path(args.image).read_bytes()).decode()
        mime = "image/png" if args.image.lower().endswith(".png") else "image/jpeg"
        resp = client.post(
            "/images/edits",
            body={
                "model": model,
                "prompt": args.prompt,
                "image": {"url": f"data:{mime};base64,{data}", "type": "image_url"},
                "response_format": "b64_json",
            },
            cast_to=dict,
        )
        b64 = resp["data"][0]["b64_json"]
    else:
        resp = client.images.generate(model=model, prompt=args.prompt, response_format="b64_json")
        b64 = resp.data[0].b64_json

    from PIL import Image

    out.parent.mkdir(parents=True, exist_ok=True)
    Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA").save(out, format="PNG")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
