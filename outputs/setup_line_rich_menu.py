from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


RICH_MENU_API = "https://api.line.me/v2/bot/richmenu"
RICH_MENU_DATA_API = "https://api-data.line.me/v2/bot/richmenu"


def request_json(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def request_bytes(method: str, url: str, token: str, content_type: str, data: bytes) -> None:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=30) as response:
        response.read()


def find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        r"C:\Windows\Fonts\NotoSansThai-Bold.ttf" if bold else r"C:\Windows\Fonts\NotoSansThai-Regular.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThai-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def center_text(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font: ImageFont.ImageFont, fill: str) -> None:
    lines = text.splitlines()
    line_heights = []
    widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])
    total_height = sum(line_heights) + (len(lines) - 1) * 18
    y = box[1] + ((box[3] - box[1]) - total_height) // 2
    for line, width, line_height in zip(lines, widths, line_heights):
        x = box[0] + ((box[2] - box[0]) - width) // 2
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height + 18


def create_rich_menu_image(path: Path) -> None:
    width, height = 2500, 1686
    image = Image.new("RGB", (width, height), "#F8FAFC")
    draw = ImageDraw.Draw(image)

    title_font = find_font(82, bold=True)
    button_font = find_font(78, bold=True)
    hint_font = find_font(42)

    draw.rounded_rectangle((70, 55, width - 70, 260), radius=45, fill="#DBEAFE")
    center_text(draw, (70, 55, width - 70, 205), "Khunjao Back Office", title_font, "#1E3A8A")
    center_text(draw, (70, 185, width - 70, 252), "เลือกหมวดงานที่ต้องการใช้งาน", hint_font, "#1D4ED8")

    gap = 26
    top = 310
    left = 70
    button_w = width - (left * 2)
    button_h = (height - top - 70 - (gap * 3)) // 4
    boxes = []
    for index in range(4):
        y1 = top + index * (button_h + gap)
        boxes.append((left, y1, left + button_w, y1 + button_h))
    buttons = [
        ("บัญชี", "#BBF7D0", "#14532D"),
        ("สต็อค", "#BFDBFE", "#1E3A8A"),
        ("HR", "#FDE68A", "#78350F"),
        ("สินค้า", "#FECACA", "#7F1D1D"),
    ]

    for box, (text, fill, color) in zip(boxes, buttons):
        draw.rounded_rectangle(box, radius=40, fill=fill, outline="#FFFFFF", width=8)
        center_text(draw, box, text, button_font, color)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)


def delete_existing_rich_menus(token: str) -> None:
    result = request_json("GET", f"{RICH_MENU_API}/list", token)
    for item in result.get("richmenus", []):
        rich_menu_id = item.get("richMenuId")
        if rich_menu_id:
            request_json("DELETE", f"{RICH_MENU_API}/{rich_menu_id}", token)
            print(f"Deleted rich menu: {rich_menu_id}")


def create_rich_menu(token: str, image_path: Path) -> str:
    payload = {
        "size": {"width": 2500, "height": 1686},
        "selected": True,
        "name": "Khunjao Back Office Menu",
        "chatBarText": "เมนูหลัก",
        "areas": [
            {"bounds": {"x": 70, "y": 310, "width": 2360, "height": 307}, "action": {"type": "message", "text": "บัญชี"}},
            {"bounds": {"x": 70, "y": 643, "width": 2360, "height": 307}, "action": {"type": "message", "text": "สต็อค"}},
            {"bounds": {"x": 70, "y": 976, "width": 2360, "height": 307}, "action": {"type": "message", "text": "HR"}},
            {"bounds": {"x": 70, "y": 1309, "width": 2360, "height": 307}, "action": {"type": "message", "text": "สินค้า"}},
        ],
    }
    result = request_json("POST", RICH_MENU_API, token, payload)
    rich_menu_id = result["richMenuId"]
    request_bytes("POST", f"{RICH_MENU_DATA_API}/{rich_menu_id}/content", token, "image/png", image_path.read_bytes())
    request_json("POST", f"{RICH_MENU_API}/{rich_menu_id}/default", token)
    return rich_menu_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and set the default LINE rich menu for the VAT bot.")
    parser.add_argument("--token", default=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"), help="LINE channel access token")
    parser.add_argument("--image", default="outputs/line_rich_menu.png", help="Path to save generated rich menu PNG")
    parser.add_argument("--image-only", action="store_true", help="Only generate the rich menu PNG; do not call LINE API")
    parser.add_argument("--delete-existing", action="store_true", help="Delete existing rich menus before creating a new one")
    args = parser.parse_args()

    image_path = Path(args.image)
    create_rich_menu_image(image_path)
    print(f"Generated rich menu image: {image_path}")

    if args.image_only:
        return 0

    if not args.token:
        print("Missing LINE channel access token. Set LINE_CHANNEL_ACCESS_TOKEN or pass --token.", file=sys.stderr)
        return 2

    if args.delete_existing:
        delete_existing_rich_menus(args.token)

    rich_menu_id = create_rich_menu(args.token, image_path)
    print(f"Created and set default rich menu: {rich_menu_id}")
    print("Open LINE OA chat again. The menu should appear at the bottom of the chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
