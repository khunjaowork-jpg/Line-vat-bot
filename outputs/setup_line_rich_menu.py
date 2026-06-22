from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


RICH_MENU_API = "https://api.line.me/v2/bot/richmenu"
RICH_MENU_DATA_API = "https://api-data.line.me/v2/bot/richmenu"
DEFAULT_RICH_MENU_API = "https://api.line.me/v2/bot/user/all/richmenu"


def request_json(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LINE API error {exc.code} {exc.reason}: {body}") from exc


def request_bytes(method: str, url: str, token: str, content_type: str, data: bytes) -> None:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LINE API upload error {exc.code} {exc.reason}: {body}") from exc


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


def fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def draw_arrow_button(draw: ImageDraw.ImageDraw, cx: int, cy: int, color: str) -> None:
    draw.ellipse((cx - 58, cy - 58, cx + 58, cy + 58), fill=color)
    draw.line((cx - 16, cy - 28, cx + 18, cy, cx - 16, cy + 28), fill="#FFFFFF", width=14, joint="curve")


def draw_document_icon(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], accent: str) -> None:
    x1, y1, x2, y2 = box
    draw.ellipse((x1 - 42, y1 - 42, x2 + 34, y2 + 34), fill="#FCA5B8")
    draw.rounded_rectangle((x1 + 48, y1 + 82, x1 + 268, y1 + 360), radius=22, fill="#FFFFFF")
    draw.polygon([(x1 + 214, y1 + 82), (x1 + 268, y1 + 82), (x1 + 268, y1 + 136)], fill="#FFE3EA")
    for offset, width in [(135, 125), (190, 150), (245, 105), (300, 75)]:
        draw.rounded_rectangle((x1 + 92, y1 + offset, x1 + 92 + width, y1 + offset + 14), radius=7, fill=accent)
    draw.ellipse((x1 + 205, y1 + 225, x1 + 363, y1 + 383), fill="#FB7185")
    center_text(draw, (x1 + 205, y1 + 238, x1 + 363, y1 + 365), "฿", find_font(90, bold=True), "#FFFFFF")


def draw_stock_icon(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], accent: str) -> None:
    x1, y1, x2, y2 = box
    draw.ellipse((x1 - 20, y1 - 36, x2 + 44, y2 + 28), fill="#B9F3EA")
    draw.rounded_rectangle((x1 + 90, y1 + 150, x1 + 315, y1 + 345), radius=22, fill="#F8FAFC", outline="#CBD5E1", width=4)
    draw.polygon([(x1 + 64, y1 + 165), (x1 + 205, y1 + 35), (x1 + 348, y1 + 165)], fill=accent)
    draw.line((x1 + 76, y1 + 165, x1 + 205, y1 + 48, x1 + 336, y1 + 165), fill="#2DD4BF", width=38, joint="curve")
    for bx, by in [(x1 + 140, y1 + 250), (x1 + 226, y1 + 250), (x1 + 183, y1 + 175)]:
        draw.rounded_rectangle((bx, by, bx + 92, by + 82), radius=14, fill="#FDBA74", outline="#F59E0B", width=3)


def draw_hr_icon(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], accent: str) -> None:
    x1, y1, x2, y2 = box
    draw.ellipse((x1 - 24, y1 - 36, x2 + 36, y2 + 38), fill="#D8B4FE")
    people = [(x1 + 100, y1 + 185, 62, "#FDE68A"), (x1 + 198, y1 + 150, 82, "#FDBA74"), (x1 + 305, y1 + 190, 58, "#FDE68A")]
    for px, py, r, skin in people:
        draw.ellipse((px - r, py - r, px + r, py + r), fill=skin, outline="#A16207", width=3)
        draw.rounded_rectangle((px - r - 18, py + r - 4, px + r + 18, py + r + 140), radius=34, fill=accent)


def draw_product_icon(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], accent: str) -> None:
    x1, y1, x2, y2 = box
    draw.ellipse((x1 - 16, y1 - 24, x2 + 42, y2 + 40), fill="#FDE68A")
    draw.rounded_rectangle((x1 + 110, y1 + 120, x1 + 285, y1 + 330), radius=24, fill=accent)
    draw.arc((x1 + 150, y1 + 45, x1 + 245, y1 + 180), start=180, end=360, fill="#FFFFFF", width=18)
    draw.rounded_rectangle((x1 + 250, y1 + 235, x1 + 390, y1 + 365), radius=18, fill="#FED7AA")
    draw.rounded_rectangle((x1 + 275, y1 + 235, x1 + 330, y1 + 282), radius=8, fill="#FDBA74")
    draw.rounded_rectangle((x1 + 80, y1 + 230, x1 + 175, y1 + 345), radius=14, fill="#FFFFFF")
    draw.line((x1 + 96, y1 + 265, x1 + 156, y1 + 265), fill="#F59E0B", width=8)


def draw_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    title: str,
    subtitle: str,
    bg: str,
    accent: str,
    title_color: str,
    icon: str,
) -> None:
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=52, fill=bg, outline="#FFFFFF", width=8)
    draw.pieslice((x1 - 130, y1 - 170, x1 + 430, y1 + 310), start=0, end=92, fill="#FFFFFF")
    draw.ellipse((x1 + 70, y1 + 180, x1 + 500, y1 + 610), fill="#FFFFFF")
    for px in range(x1 + 90, x1 + 235, 44):
        for py in range(y2 - 145, y2 - 35, 44):
            draw.ellipse((px, py, px + 10, py + 10), fill=accent)

    icon_box = (x1 + 105, y1 + 150, x1 + 470, y1 + 520)
    if icon == "document":
        draw_document_icon(draw, icon_box, accent)
    elif icon == "stock":
        draw_stock_icon(draw, icon_box, accent)
    elif icon == "hr":
        draw_hr_icon(draw, icon_box, accent)
    else:
        draw_product_icon(draw, icon_box, accent)

    title_font = find_font(96, bold=True)
    subtitle_font = find_font(48)
    tx = x1 + 650
    ty = y1 + 220
    draw.text((tx, ty), title, font=title_font, fill=title_color)
    draw.rounded_rectangle((tx, ty + 130, tx + 120, ty + 143), radius=7, fill=accent)
    sy = ty + 185
    for line in fit_text(draw, subtitle, subtitle_font, 430):
        draw.text((tx, sy), line, font=subtitle_font, fill="#475569")
        sy += 66
    draw.rounded_rectangle((tx, sy + 12, tx + 120, sy + 25), radius=7, fill=accent)
    draw_arrow_button(draw, x2 - 135, y2 - 135, accent)


def create_rich_menu_image(path: Path) -> None:
    width, height = 2500, 1686
    image = Image.new("RGB", (width, height), "#F8FAFC")
    draw = ImageDraw.Draw(image)

    margin = 38
    gap = 34
    card_w = (width - margin * 2 - gap) // 2
    card_h = (height - margin * 2 - gap) // 2
    boxes = [
        (margin, margin, margin + card_w, margin + card_h),
        (margin + card_w + gap, margin, width - margin, margin + card_h),
        (margin, margin + card_h + gap, margin + card_w, height - margin),
        (margin + card_w + gap, margin + card_h + gap, width - margin, height - margin),
    ]
    cards = [
        ("บัญชี", "ดูข้อมูลทางการเงิน และรายงานบัญชี", "#FFE4E9", "#FB7185", "#3F3F46", "document"),
        ("สต็อค", "ตรวจสอบสต็อคสินค้า และความเคลื่อนไหว", "#DFFAF5", "#2DD4BF", "#3F3F46", "stock"),
        ("HR", "จัดการข้อมูลพนักงาน และการทำงาน", "#F3E8FF", "#A855F7", "#3F3F46", "hr"),
        ("สินค้า", "ค้นหาข้อมูลสินค้า ราคา และบาร์โค้ด", "#FEF3C7", "#FBBF24", "#3F3F46", "product"),
    ]
    for box, card in zip(boxes, cards):
        draw_card(draw, box, *card)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)


def delete_existing_rich_menus(token: str) -> None:
    try:
        request_json("DELETE", DEFAULT_RICH_MENU_API, token)
        print("Unlinked default rich menu from all users.")
    except RuntimeError as exc:
        print(f"Default rich menu unlink skipped: {exc}")
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
            {"bounds": {"x": 38, "y": 38, "width": 1195, "height": 788}, "action": {"type": "message", "text": "บัญชี"}},
            {"bounds": {"x": 1267, "y": 38, "width": 1195, "height": 788}, "action": {"type": "message", "text": "สต็อค"}},
            {"bounds": {"x": 38, "y": 860, "width": 1195, "height": 788}, "action": {"type": "message", "text": "HR"}},
            {"bounds": {"x": 1267, "y": 860, "width": 1195, "height": 788}, "action": {"type": "message", "text": "สินค้า"}},
        ],
    }
    result = request_json("POST", RICH_MENU_API, token, payload)
    rich_menu_id = result["richMenuId"]
    request_bytes("POST", f"{RICH_MENU_DATA_API}/{rich_menu_id}/content", token, "image/png", image_path.read_bytes())
    request_json("POST", f"{DEFAULT_RICH_MENU_API}/{rich_menu_id}", token)
    return rich_menu_id


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and set the default LINE rich menu for the VAT bot.")
    parser.add_argument("--token", default=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"), help="LINE channel access token")
    parser.add_argument("--image", default="outputs/line_rich_menu.png", help="Path to save generated rich menu PNG")
    parser.add_argument("--image-only", action="store_true", help="Only generate the rich menu PNG; do not call LINE API")
    parser.add_argument("--delete-existing", action="store_true", help="Delete existing rich menus before creating a new one")
    parser.add_argument("--delete-only", action="store_true", help="Only remove existing rich menus and default rich menu")
    args = parser.parse_args()

    image_path = Path(args.image)
    create_rich_menu_image(image_path)
    print(f"Generated rich menu image: {image_path}")

    if args.image_only:
        return 0

    if not args.token:
        print("Missing LINE channel access token. Set LINE_CHANNEL_ACCESS_TOKEN or pass --token.", file=sys.stderr)
        return 2

    if args.delete_only:
        delete_existing_rich_menus(args.token)
        print("Rich menu removed. The bot will show vertical buttons only when a menu is needed.")
        return 0

    if args.delete_existing:
        delete_existing_rich_menus(args.token)

    rich_menu_id = create_rich_menu(args.token, image_path)
    print(f"Created and set default rich menu: {rich_menu_id}")
    print("Open LINE OA chat again. The menu should appear at the bottom of the chat.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
