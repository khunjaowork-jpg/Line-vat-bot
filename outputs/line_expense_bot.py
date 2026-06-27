from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import sys
import uuid
import urllib.parse
import urllib.request
from urllib.parse import unquote
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from openpyxl import load_workbook


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CONFIG: dict[str, Any] = {}
CONFIG_PATH: Path | None = None
STATE_CACHE: dict[str, Any] = {}


def runtime_log(message: str) -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line, file=sys.stderr)
    try:
        Path("outputs/line_bot_runtime.log").open("a", encoding="utf-8").write(line + "\n")
    except Exception:
        pass


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if os.getenv("GOOGLE_VISION_API_KEY"):
        config["google_vision_api_key"] = os.getenv("GOOGLE_VISION_API_KEY")
    if os.getenv("GOOGLE_APPS_SCRIPT_URL"):
        config["google_apps_script_url"] = os.getenv("GOOGLE_APPS_SCRIPT_URL")
    if os.getenv("GOOGLE_APPS_SCRIPT_SECRET"):
        config["google_apps_script_secret"] = os.getenv("GOOGLE_APPS_SCRIPT_SECRET")
    if os.getenv("VAT_RATE"):
        config["vat_rate"] = float(os.getenv("VAT_RATE", "0.07"))
    if os.getenv("PORT"):
        config["port"] = int(os.getenv("PORT", "8080"))
    if os.getenv("TESSERACT_CMD"):
        config["tesseract_cmd"] = os.getenv("TESSERACT_CMD")
    if os.getenv("TESSDATA_DIR"):
        config["tessdata_dir"] = os.getenv("TESSDATA_DIR")
    return config


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    if CONFIG_PATH is None:
        return path.resolve()
    return (CONFIG_PATH.parent.parent / path).resolve()


def state_path() -> Path:
    return resolve_path(CONFIG.get("state_file", "outputs/line_bot_state.json"))


def reply_image_dir() -> Path:
    return resolve_path(CONFIG.get("reply_image_dir", "outputs/line_reply_images"))


def load_state_cache() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        runtime_log(f"State load failed: {exc}")
        return {}


def save_state_cache() -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(STATE_CACHE, handle, ensure_ascii=False, indent=2, default=str)


def get_user_state(line_user_id: str) -> dict[str, Any]:
    if not line_user_id:
        line_user_id = "unknown"
    return dict(STATE_CACHE.get(line_user_id, {}))


def set_user_state(line_user_id: str, state: dict[str, Any]) -> None:
    if not line_user_id:
        line_user_id = "unknown"
    state["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    STATE_CACHE[line_user_id] = state
    save_state_cache()


def clear_user_state(line_user_id: str) -> None:
    if not line_user_id:
        line_user_id = "unknown"
    if line_user_id in STATE_CACHE:
        del STATE_CACHE[line_user_id]
        save_state_cache()


def serialize_data(data: dict[str, Any]) -> dict[str, Any]:
    serialized = dict(data)
    if isinstance(serialized.get("date"), (dt.date, dt.datetime)):
        serialized["date"] = serialized["date"].isoformat()
    return serialized


def deserialize_data(data: dict[str, Any]) -> dict[str, Any]:
    restored = dict(data)
    if isinstance(restored.get("date"), str):
        try:
            restored["date"] = dt.date.fromisoformat(restored["date"][:10])
        except ValueError:
            restored["date"] = dt.date.today()
    return restored


def verify_line_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")


def line_request(method: str, url: str, token: str, data: bytes | None = None, content_type: str | None = None) -> bytes:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"status": "raw", "body": body}


def download_line_content(message_id: str, token: str, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    data = line_request("GET", url, token)
    path = archive_dir / f"{dt.datetime.now():%Y%m%d_%H%M%S}_{message_id}_{uuid.uuid4().hex[:8]}.jpg"
    path.write_bytes(data)
    return path


def get_line_display_name(line_user_id: str) -> str:
    if not line_user_id:
        return ""
    line_config = CONFIG.get("line", {})
    token_name = line_config.get("channel_access_token_env", "LINE_CHANNEL_ACCESS_TOKEN")
    token = os.getenv(token_name)
    if not token:
        return ""
    try:
        data = line_request("GET", f"https://api.line.me/v2/bot/profile/{line_user_id}", token)
        profile = json.loads(data.decode("utf-8"))
        return str(profile.get("displayName") or "").strip()
    except Exception as exc:
        runtime_log(f"LINE profile lookup failed: {exc}")
        return ""


def reply_messages(reply_token: str, messages: list[dict[str, Any]]) -> None:
    line_config = CONFIG["line"]
    if not line_config.get("reply_enabled", True):
        return
    token = os.getenv(line_config["channel_access_token_env"])
    if not token:
        runtime_log("LINE reply skipped: missing channel access token")
        return
    payload = {"replyToken": reply_token, "messages": messages[:5]}
    try:
        line_request(
            "POST",
            "https://api.line.me/v2/bot/message/reply",
            token,
            data=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
        runtime_log("LINE reply sent")
    except Exception as exc:
        runtime_log(f"LINE reply failed: {exc}")


def push_line_messages(to_id: str, messages: list[dict[str, Any]]) -> None:
    line_config = CONFIG.get("line", {})
    token_name = line_config.get("channel_access_token_env", "LINE_CHANNEL_ACCESS_TOKEN")
    token = os.getenv(token_name)
    if not token or not to_id:
        runtime_log("LINE push skipped: missing token or target id")
        return
    payload = {"to": to_id, "messages": messages[:5]}
    try:
        line_request(
            "POST",
            "https://api.line.me/v2/bot/message/push",
            token,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
        runtime_log(f"LINE push sent to {to_id}")
    except Exception as exc:
        runtime_log(f"LINE push failed: {exc}")


def text_message(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text[:4900]}


def quick_reply_text_message(text: str, buttons: list[tuple[str, str]]) -> dict[str, Any]:
    message = text_message(text)
    message["quickReply"] = {
        "items": [
            {
                "type": "action",
                "action": {
                    "type": "message",
                    "label": label[:20],
                    "text": value,
                },
            }
            for label, value in buttons
        ][:13]
    }
    return message


def buttons_template_message(text: str, buttons: list[tuple[str, str]], title: str | None = None) -> dict[str, Any]:
    template: dict[str, Any] = {
        "type": "buttons",
        "text": text[:160],
        "actions": [
            {
                "type": "message",
                "label": label[:20],
                "text": value,
            }
            for label, value in buttons
        ][:4],
    }
    if title:
        template["title"] = title[:40]
    return {
        "type": "template",
        "altText": text[:400],
        "template": template,
    }


def image_message(public_url: str) -> dict[str, Any]:
    return {
        "type": "image",
        "originalContentUrl": public_url,
        "previewImageUrl": public_url,
    }


def reply_text(reply_token: str, text: str) -> None:
    reply_messages(reply_token, [text_message(text)])


def normalize_amount(value: str) -> float | None:
    cleaned = re.sub(r"[^\d.,-]", "", value).replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date(text: str) -> dt.date | None:
    patterns = [
        r"(?<!\d)(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?!\d)",
        r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})(?!\d)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        parts = [int(part) for part in match.groups()]
        try:
            if len(str(parts[0])) == 4:
                year, month, day = parts
                if year > 2400:
                    year -= 543
            else:
                day, month, year = parts
                if year < 100:
                    year += 2000
                if year > 2400:
                    year -= 543
            return dt.date(year, month, day)
        except ValueError:
            continue
    return None


def find_amount_after_keywords(text: str, keywords: list[str]) -> float | None:
    for keyword in keywords:
        pattern = rf"{keyword}[\s:]*([0-9,]+(?:\.\d{{1,2}})?)"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            amount = normalize_amount(match.group(1))
            if amount is not None:
                return amount
    return None


def find_likely_total(text: str) -> float | None:
    amounts: list[float] = []
    for match in re.finditer(r"(?<![\d-])(\d{1,3}(?:,\d{3})*|\d+)\.\d{2}(?!\d)", text):
        amount = normalize_amount(match.group(0))
        if amount is not None and 0 < amount < 10_000_000:
            amounts.append(amount)
    if not amounts:
        return None
    return max(amounts)


def extract_document_no(text: str) -> str:
    keywords = [
        "invoice no",
        "invoice no.",
        "invoice number",
        "tax invoice no",
        "tax invoice no.",
        "receipt no",
        "receipt no.",
        "receipt number",
        "document no",
        "document number",
        "เลขที่เอกสาร",
        "เลขที่บิล",
        "เลขที่ใบเสร็จ",
        "เลขที่ใบกำกับ",
        "เลขที่",
    ]
    # Match line-by-line first so the captured value does not drift into later text.
    candidates = [line.strip() for line in text.splitlines() if line.strip()]
    candidates.append(re.sub(r"\s+", " ", text))

    value_pattern = r"([A-Z0-9ก-๙][A-Z0-9ก-๙./_-]{1,40})"
    for candidate in candidates:
        for keyword in keywords:
            keyword_pattern = re.escape(keyword).replace(r"\ ", r"\s*")
            pattern = rf"{keyword_pattern}\s*(?:[:#\-]|no\.?|number|เลขที่)?\s*{value_pattern}"
            match = re.search(pattern, candidate, flags=re.IGNORECASE)
            if not match:
                continue
            value = match.group(1).strip(" .:-#")
            if value and value.lower() not in {"date", "no", "number"}:
                return value
    return ""


def normalize_invoice_no(value: Any) -> str:
    text = str(value or "").strip()
    return text if text else "-"


def extract_document_type(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).lower()
    thai_compact = re.sub(r"\s+", "", text)
    if "tax invoice" in compact or "ใบกำกับภาษี" in thai_compact or "ใบกํากับภาษี" in thai_compact:
        return "ใบกำกับภาษี"
    if "receipt" in compact or "ใบเสร็จ" in thai_compact:
        return "ใบเสร็จ"
    if "bill" in compact or "บิล" in thai_compact:
        return "บิล"
    if "invoice" in compact:
        return "ใบกำกับภาษี"
    return "บิล/ใบเสร็จ"


def parse_receipt_text(text: str, vat_rate: float) -> dict[str, Any]:
    compact = re.sub(r"\s+", " ", text)
    total = find_amount_after_keywords(
        compact,
        ["ยอดรวม", "รวมทั้งสิ้น", "จำนวนเงินรวม", "total", "grand total", "amount due"],
    )
    vat = find_amount_after_keywords(
        compact,
        ["ภาษีมูลค่าเพิ่ม", "ภาษี", "vat", "value added tax"],
    )
    withholding_tax = find_amount_after_keywords(
        compact,
        ["withholding tax", "wht", "tax withheld", "\u0e20\u0e32\u0e29\u0e35\u0e2b\u0e31\u0e01 \u0e13 \u0e17\u0e35\u0e48\u0e08\u0e48\u0e32\u0e22", "\u0e20\u0e32\u0e29\u0e35\u0e2b\u0e31\u0e01\u0e13\u0e17\u0e35\u0e48\u0e08\u0e48\u0e32\u0e22"],
    )
    before_vat = find_amount_after_keywords(
        compact,
        ["ยอดก่อนภาษี", "มูลค่าสินค้า", "subtotal", "before vat"],
    )

    if total is None:
        total = find_likely_total(compact)

    if before_vat is None and total is not None:
        if vat is not None:
            before_vat = total - vat
        else:
            before_vat = round(total / (1 + vat_rate), 2)
            vat = round(total - before_vat, 2)
    if vat is None and before_vat is not None:
        vat = round(before_vat * vat_rate, 2)
    if total is None and before_vat is not None:
        total = round(before_vat + (vat or 0), 2)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    vendor = lines[0][:80] if lines else ""
    invoice_no = normalize_invoice_no(extract_document_no(text))

    confidence = 0.25
    if parse_date(compact):
        confidence += 0.2
    if vendor:
        confidence += 0.15
    if before_vat is not None:
        confidence += 0.2
    if vat is not None:
        confidence += 0.1
    if total is not None:
        confidence += 0.1

    return {
        "date": parse_date(compact) or dt.date.today(),
        "document_type": extract_document_type(text),
        "invoice_no": invoice_no,
        "vendor": vendor,
        "description": "LINE receipt OCR",
        "category": CONFIG.get("default_category", "อื่น ๆ"),
        "before_vat": round(before_vat or 0, 2),
        "vat": round(vat or 0, 2),
        "withholding_tax": round(withholding_tax or 0, 2),
        "total": round(total or 0, 2),
        "claimable": CONFIG.get("default_claimable", "Yes") if (vat or 0) > 0 else "No",
        "confidence": min(confidence, 1.0),
        "raw_text": compact[:500],
    }


def apply_transaction_type_defaults(data: dict[str, Any], transaction_type: str) -> dict[str, Any]:
    updated = dict(data)
    normalized_type = "Revenue" if str(transaction_type).lower() == "revenue" else "Expense"
    updated["transaction_type"] = normalized_type
    updated["invoice_no"] = normalize_invoice_no(updated.get("invoice_no"))
    if normalized_type == "Revenue":
        updated["category"] = updated.get("category") or CONFIG.get("default_revenue_category", "Revenue")
        updated["claimable"] = "Yes"
        if updated.get("description") == "LINE receipt OCR":
            updated["description"] = "LINE revenue OCR"
    else:
        updated["category"] = updated.get("category") or CONFIG.get("default_category", "Other")
        updated["claimable"] = updated.get("claimable") or CONFIG.get("default_claimable", "Yes")
    return updated


def ocr_image(image_path: Path) -> str:
    vision_api_key = CONFIG.get("google_vision_api_key") or os.getenv(CONFIG.get("google_vision_api_key_env", "GOOGLE_VISION_API_KEY"))
    if vision_api_key:
        try:
            runtime_log("Google Vision OCR started")
            text = google_vision_ocr_image(image_path, vision_api_key)
            runtime_log(f"Google Vision OCR completed characters={len(text)}")
            if text.strip():
                return text
            raise RuntimeError("Google Vision OCR returned empty text")
        except Exception as exc:
            raise RuntimeError(f"Google Vision OCR failed within time limit: {exc}") from exc

    try:
        from PIL import Image, ImageOps
        import pytesseract
    except ImportError as exc:
        raise RuntimeError("Install OCR packages first: pip install pillow pytesseract") from exc

    lang = CONFIG.get("tesseract_lang", "tha+eng")
    if CONFIG.get("tesseract_cmd"):
        tesseract_cmd = str(CONFIG["tesseract_cmd"])
        if Path(tesseract_cmd).is_absolute() or "/" in tesseract_cmd or "\\" in tesseract_cmd:
            tesseract_cmd = str(resolve_path(tesseract_cmd))
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    if CONFIG.get("tessdata_dir"):
        tessdata_dir = resolve_path(CONFIG["tessdata_dir"])
        os.environ["TESSDATA_PREFIX"] = str(tessdata_dir)
    with Image.open(image_path) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((int(CONFIG.get("ocr_max_dimension", 1600)), int(CONFIG.get("ocr_max_dimension", 1600))))
        image = ImageOps.autocontrast(image.convert("L"))
        tesseract_config = CONFIG.get("tesseract_config", "--oem 1 --psm 11")
        timeout = int(CONFIG.get("ocr_timeout_seconds", 15))
        try:
            return pytesseract.image_to_string(image, lang=lang, config=tesseract_config, timeout=timeout)
        except RuntimeError as exc:
            raise RuntimeError(f"Tesseract OCR failed within time limit: {exc}") from exc


def google_vision_ocr_image(image_path: Path, api_key: str) -> str:
    image_content = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "requests": [
            {
                "image": {"content": image_content},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                "imageContext": {"languageHints": ["th", "en"]},
            }
        ]
    }
    request = urllib.request.Request(
        f"https://vision.googleapis.com/v1/images:annotate?key={api_key}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    timeout = int(CONFIG.get("google_vision_timeout_seconds", 15))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))

    if result.get("error"):
        raise RuntimeError(result["error"].get("message", result["error"]))

    responses = result.get("responses", [])
    if not responses:
        return ""
    first = responses[0]
    if first.get("error"):
        raise RuntimeError(first["error"].get("message", first["error"]))
    if first.get("fullTextAnnotation", {}).get("text"):
        return first["fullTextAnnotation"]["text"]
    annotations = first.get("textAnnotations") or []
    if annotations:
        return annotations[0].get("description", "")
    return ""


def next_empty_row(sheet, start_row: int = 4) -> int:
    row = start_row
    while sheet.cell(row=row, column=1).value:
        row += 1
    return row


def copy_formula_row(sheet, row: int, cols: tuple[int, ...]) -> None:
    if row <= 4:
        return
    for col in cols:
        source = sheet.cell(row=row - 1, column=col)
        target = sheet.cell(row=row, column=col)
        if isinstance(source.value, str) and source.value.startswith("="):
            target.value = source.value.replace(str(row - 1), str(row))


def find_transaction_row(sheet, date_value: dt.date) -> int:
    month_names = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]
    target_month = month_names[date_value.month - 1]
    block_start = None

    for row in range(1, sheet.max_row + 1):
        value = sheet.cell(row=row, column=1).value
        if isinstance(value, str) and target_month in value:
            block_start = row + 2
            break
    if block_start is None:
        raise RuntimeError("Cannot find matching month block in Transactions_12M.")

    block_end = min(block_start + 29, sheet.max_row)
    for row in range(block_start, block_end + 1):
        if not sheet.cell(row=row, column=1).value:
            return row
    raise RuntimeError(f"No empty rows left in Transactions_12M for {date_value:%Y-%m}.")


def append_to_transactions(sheet, row: int, image_path: Path, data: dict[str, Any]) -> None:
    sheet.cell(row=row, column=1, value=data["date"])
    sheet.cell(row=row, column=2, value=data.get("transaction_type", "Expense"))
    sheet.cell(row=row, column=3, value=normalize_invoice_no(data.get("invoice_no")))
    sheet.cell(row=row, column=4, value=data["vendor"])
    sheet.cell(row=row, column=5, value=data["description"])
    sheet.cell(row=row, column=6, value=data["category"])
    sheet.cell(row=row, column=7, value=data["before_vat"])
    sheet.cell(row=row, column=8, value=CONFIG.get("vat_rate", 0.07))
    sheet.cell(row=row, column=9, value=f'=IF(G{row}="","",IF(OR(B{row}="Revenue",K{row}="Yes"),ROUND(G{row}*H{row},2),0))')
    sheet.cell(row=row, column=10, value=f'=IF(G{row}="","",G{row}+ROUND(G{row}*H{row},2))')
    sheet.cell(row=row, column=11, value=data["claimable"])
    sheet.cell(row=row, column=12, value=f'=IF(A{row}="","",DATE(YEAR(A{row}),MONTH(A{row}),1))')
    sheet.cell(row=row, column=13, value=str(image_path))
    sheet.cell(row=row, column=14, value=data["confidence"])
    sheet.cell(row=row, column=15, value=f'=IF(B{row}="Revenue",G{row},0)')
    sheet.cell(row=row, column=16, value=f'=IF(B{row}="Expense",G{row},0)')
    sheet.cell(row=row, column=17, value=data["raw_text"])
    sheet.cell(row=row, column=18, value=data.get("document_type", "บิล/ใบเสร็จ"))


def append_to_legacy_expenses(sheet, row: int, image_path: Path, data: dict[str, Any]) -> None:
    sheet.cell(row=row, column=1, value=data["date"])
    sheet.cell(row=row, column=2, value=normalize_invoice_no(data.get("invoice_no")))
    sheet.cell(row=row, column=3, value=data["vendor"])
    sheet.cell(row=row, column=4, value=data["description"])
    sheet.cell(row=row, column=5, value=data["category"])
    sheet.cell(row=row, column=6, value=data["before_vat"])
    sheet.cell(row=row, column=7, value=CONFIG.get("vat_rate", 0.07))
    sheet.cell(row=row, column=10, value=data["claimable"])
    sheet.cell(row=row, column=12, value=str(image_path))
    sheet.cell(row=row, column=13, value=data["confidence"])
    sheet.cell(row=row, column=14, value=data["raw_text"])
    sheet.cell(row=row, column=15, value=data.get("document_type", "บิล/ใบเสร็จ"))
    copy_formula_row(sheet, row, (8, 9, 11))


def append_to_legacy_revenue(sheet, row: int, data: dict[str, Any]) -> None:
    sheet.cell(row=row, column=1, value=data["date"])
    sheet.cell(row=row, column=2, value=normalize_invoice_no(data.get("invoice_no")))
    sheet.cell(row=row, column=3, value=data["vendor"])
    sheet.cell(row=row, column=4, value=data["description"])
    sheet.cell(row=row, column=5, value=data["before_vat"])
    sheet.cell(row=row, column=6, value=CONFIG.get("vat_rate", 0.07))
    sheet.cell(row=row, column=7, value=f'=IF(E{row}="","",ROUND(E{row}*F{row},2))')
    sheet.cell(row=row, column=8, value=f'=IF(E{row}="","",E{row}+G{row})')
    sheet.cell(row=row, column=9, value="Yes")
    sheet.cell(row=row, column=10, value=f'=IF(A{row}="","",DATE(YEAR(A{row}),MONTH(A{row}),1))')
    sheet.cell(row=row, column=11, value=data.get("document_type", ""))


def append_import_log(log, image_path: Path, data: dict[str, Any], status: str, message: str, line_user_id: str) -> None:
    log_row = next_empty_row(log)
    log.cell(row=log_row, column=1, value=dt.datetime.now())
    log.cell(row=log_row, column=2, value=str(image_path))
    log.cell(row=log_row, column=3, value=status)
    log.cell(row=log_row, column=4, value=data["date"])
    log.cell(row=log_row, column=5, value=data["vendor"])
    log.cell(row=log_row, column=6, value=data["total"])
    log.cell(row=log_row, column=7, value=data["vat"])
    log.cell(row=log_row, column=8, value=f"{message} LINE user: {line_user_id}")
    log.cell(row=log_row, column=9, value=data.get("document_type", ""))


def format_parsed_details(data: dict[str, Any], heading: str = "บิลนำเข้า") -> str:
    return (
        f"==== {heading} ====\n"
        f"ประเภท: {data.get('transaction_type', '-')}\n"
        f"ประเภทเอกสาร: {data.get('document_type') or '-'}\n"
        f"วันที่: {data.get('date')}\n"
        f"เลขที่บิล: {data.get('invoice_no') or '-'}\n"
        f"ชื่อร้าน/คู่ค้า: {data.get('vendor') or '-'}\n"
        f"ผู้นำส่งเอกสาร: {data.get('submitter_name') or '-'}\n"
        f"หมวด: {data.get('category') or '-'}\n"
        f"ยอดก่อน VAT: {float(data.get('before_vat') or 0):,.2f}\n"
        f"VAT: {float(data.get('vat') or 0):,.2f}\n"
        f"ภาษีหัก ณ ที่จ่าย: {float(data.get('withholding_tax') or 0):,.2f}\n"
        f"ยอดรวม: {float(data.get('total') or 0):,.2f}\n"
        f"ความมั่นใจ OCR: {float(data.get('confidence') or 0):.0%}"
    )


def confirmation_prompt(data: dict[str, Any]) -> str:
    return (
        format_parsed_details(data, "บิลนำเข้า") + "\n\n"
        "กรุณาตรวจสอบข้อมูลก่อนบันทึกลง Excel\n"
        "ถ้าถูกต้อง พิมพ์: ตรวจสอบและยืนยัน\n"
        "ถ้าต้องการแก้ไข พิมพ์: แก้ไข"
    )


def correction_form(data: dict[str, Any]) -> str:
    return (
        "กรุณาแก้ไขข้อมูลในแบบฟอร์มนี้ แล้วส่งกลับมาได้เลยค่ะ\n\n"
        f"ประเภทเอกสาร: {data.get('document_type') or ''}\n"
        f"วันที่: {data.get('date') or ''}\n"
        f"เลขที่บิล: {normalize_invoice_no(data.get('invoice_no'))}\n"
        f"ชื่อร้าน/คู่ค้า: {data.get('vendor') or ''}\n"
        f"ผู้นำส่งเอกสาร: {data.get('submitter_name') or ''}\n"
        f"หมวด: {data.get('category') or ''}\n"
        f"ยอดก่อน VAT: {float(data.get('before_vat') or 0):.2f}\n"
        f"VAT: {float(data.get('vat') or 0):.2f}\n"
        f"ภาษีหัก ณ ที่จ่าย: {float(data.get('withholding_tax') or 0):.2f}\n"
        f"ยอดรวม: {float(data.get('total') or 0):.2f}"
    )


def menu_text() -> str:
    return (
        "กรุณาเลือกเมนู\n"
        "1. บิลรายรับ\n"
        "2. บิลรายจ่าย\n"
        "3. เรียกดูรายละเอียดบัญชี\n"
        "4. ยกเลิกการทำรายการ\n\n"
        "พิมพ์เลขเมนูที่ต้องการได้เลยค่ะ"
    )


def account_menu_button(number: str, title: str, text: str, background: str, accent: str) -> dict[str, Any]:
    return {
        "type": "box",
        "layout": "horizontal",
        "cornerRadius": "18px",
        "backgroundColor": background,
        "paddingAll": "16px",
        "spacing": "14px",
        "action": {"type": "message", "label": title[:20], "text": text},
        "contents": [
            {
                "type": "box",
                "layout": "vertical",
                "width": "54px",
                "height": "54px",
                "cornerRadius": "27px",
                "backgroundColor": accent,
                "alignItems": "center",
                "justifyContent": "center",
                "contents": [
                    {
                        "type": "text",
                        "text": number,
                        "weight": "bold",
                        "size": "xl",
                        "color": "#FFFFFF",
                        "align": "center",
                    }
                ],
            },
            {
                "type": "text",
                "text": f"{number}. {title}",
                "weight": "bold",
                "size": "xl",
                "color": "#111C4E",
                "wrap": True,
                "gravity": "center",
                "flex": 1,
            },
            {
                "type": "text",
                "text": ">",
                "weight": "bold",
                "size": "xxl",
                "color": accent,
                "align": "end",
                "gravity": "center",
                "flex": 0,
            },
        ],
    }


def menu_message() -> dict[str, Any]:
    buttons = [
        account_menu_button("1", "บิลรายรับ", "1", "#F1F7FF", "#3B82F6"),
        account_menu_button("2", "บิลรายจ่าย", "2", "#EFFCF8", "#10B981"),
        account_menu_button("3", "เรียกดูบัญชี", "3", "#FFF7E8", "#F59E0B"),
        account_menu_button("4", "ยกเลิกรายการ", "4", "#FFF1F6", "#EC4899"),
    ]
    return {
        "type": "flex",
        "altText": "กรุณาเลือกเมนูบัญชี",
        "contents": {
            "type": "bubble",
            "size": "giga",
            "body": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#FFFFFF",
                "paddingAll": "18px",
                "spacing": "14px",
                "contents": [
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "cornerRadius": "18px",
                        "backgroundColor": "#F6F0FF",
                        "paddingAll": "16px",
                        "spacing": "12px",
                        "contents": [
                            {
                                "type": "box",
                                "layout": "vertical",
                                "width": "48px",
                                "height": "48px",
                                "cornerRadius": "24px",
                                "backgroundColor": "#7C3AED",
                                "alignItems": "center",
                                "justifyContent": "center",
                                "contents": [
                                    {
                                        "type": "text",
                                        "text": "≡",
                                        "weight": "bold",
                                        "size": "xxl",
                                        "color": "#FFFFFF",
                                        "align": "center",
                                    }
                                ],
                            },
                            {
                                "type": "text",
                                "text": "กรุณาเลือกเมนู",
                                "weight": "bold",
                                "size": "xxl",
                                "color": "#111C4E",
                                "gravity": "center",
                                "wrap": True,
                                "flex": 1,
                            },
                        ],
                    },
                    *buttons,
                ],
            },
        },
    }


def coming_soon_message(section: str) -> dict[str, Any]:
    return buttons_template_message(
        f"หมวด {section}\n\n"
        "ระบบหมวดนี้ยังอยู่ระหว่างเตรียมใช้งานค่ะ\n"
        "ตอนนี้สามารถใช้งานหมวดบัญชีได้ก่อน",
        [
            ("เปิดเมนูบัญชี", "บัญชี"),
        ],
    )


def stock_menu_message() -> dict[str, Any]:
    return stock_branch_menu_message()


def stock_branch_menu_message() -> dict[str, Any]:
    return buttons_template_message(
        "กรุณาเลือกสาขาที่ต้องการตรวจสอบสต็อค\nหลังเลือกสาขา สามารถพิมพ์ชื่อสินค้า/บาร์โค้ด หรือสแกนบาร์โค้ดได้สูงสุด 10 รายการต่อครั้ง",
        [
            ("1. สี่แยก", "ค้นหาสต็อค:สี่แยก"),
            ("2. พัสดุสี่แยก", "ค้นหาสต็อค:พัสดุสี่แยก"),
            ("3. ทะเล", "ค้นหาสต็อค:ทะเล"),
            ("4. เขาใหญ่", "ค้นหาสต็อค:เขาใหญ่"),
        ],
    )

def schedule_month_menu_message() -> dict[str, Any]:
    return buttons_template_message(
        "เลือกเดือนตารางงานที่ต้องการดู",
        [
            ("เดือนก่อนหน้า", "ตารางงาน:-1"),
            ("เดือนนี้", "ตารางงาน:0"),
            ("เดือนถัดไป", "ตารางงาน:1"),
        ],
        title="ตารางงาน",
    )


def hr_menu_button(
    number: str,
    title: str,
    subtitle: str,
    text: str,
    background: str,
    accent: str,
) -> dict[str, Any]:
    return {
        "type": "box",
        "layout": "horizontal",
        "cornerRadius": "18px",
        "backgroundColor": background,
        "paddingAll": "14px",
        "spacing": "12px",
        "action": {"type": "message", "label": title[:20], "text": text},
        "contents": [
            {
                "type": "box",
                "layout": "vertical",
                "width": "50px",
                "height": "50px",
                "cornerRadius": "18px",
                "backgroundColor": accent,
                "alignItems": "center",
                "justifyContent": "center",
                "contents": [
                    {
                        "type": "text",
                        "text": number,
                        "weight": "bold",
                        "size": "xxl",
                        "color": "#FFFFFF",
                        "align": "center",
                    }
                ],
            },
            {
                "type": "box",
                "layout": "vertical",
                "flex": 1,
                "spacing": "3px",
                "justifyContent": "center",
                "contents": [
                    {
                        "type": "text",
                        "text": title,
                        "weight": "bold",
                        "size": "lg",
                        "color": "#1F2937",
                        "wrap": True,
                    },
                    {
                        "type": "text",
                        "text": subtitle,
                        "size": "sm",
                        "color": "#64748B",
                        "wrap": True,
                    },
                ],
            },
            {
                "type": "box",
                "layout": "vertical",
                "width": "32px",
                "height": "32px",
                "cornerRadius": "16px",
                "backgroundColor": "#FFFFFF",
                "alignItems": "center",
                "justifyContent": "center",
                "contents": [
                    {
                        "type": "text",
                        "text": ">",
                        "weight": "bold",
                        "size": "lg",
                        "color": accent,
                        "align": "center",
                    }
                ],
            },
        ],
    }


def hr_menu_message() -> dict[str, Any]:
    buttons = [
        hr_menu_button("1", "ตารางงาน", "ดูตารางงานของคุณ", "ตารางงาน", "#F3ECFF", "#8B5CF6"),
        hr_menu_button("2", "ลาป่วย", "แจ้งลาป่วย / บันทึกการลาป่วย", "ลาป่วย", "#E8FBF7", "#2DD4BF"),
        hr_menu_button("3", "ลากิจ", "แจ้งลากิจ / บันทึกการลากิจ", "ลากิจ", "#FFF7E6", "#F59E0B"),
        hr_menu_button("4", "แจ้งขอวันหยุดล่วงหน้า", "ขอวันหยุดล่วงหน้า / วางแผนวันหยุด", "แจ้งขอวันหยุดล่วงหน้า", "#FFF0F6", "#EC4899"),
        hr_menu_button("5", "แจ้งเปลี่ยนเวลาเข้า-ออกงาน", "แจ้งเปลี่ยนเวลาเข้า-ออกงานล่วงหน้า", "แจ้งเปลี่ยนเวลาเข้า-ออกงาน", "#EFF6FF", "#3B82F6"),
        hr_menu_button("6", "แจ้งเปลี่ยนวันทำงาน", "แจ้งเปลี่ยนวันทำงาน / สลับวันทำงาน", "แจ้งเปลี่ยนวันทำงาน", "#F5F3FF", "#A855F7"),
    ]
    return {
        "type": "flex",
        "altText": "กรุณาเลือกเมนู HR",
        "contents": {
            "type": "bubble",
            "size": "giga",
            "body": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#FFFBF7",
                "paddingAll": "16px",
                "spacing": "10px",
                "contents": [
                    {
                        "type": "text",
                        "text": "กรุณาเลือกเมนู HR",
                        "weight": "bold",
                        "size": "xl",
                        "color": "#111827",
                    },
                    {
                        "type": "text",
                        "text": "แตะปุ่มที่ต้องการใช้งานได้เลยค่ะ",
                        "size": "sm",
                        "color": "#64748B",
                        "margin": "xs",
                    },
                    {"type": "separator", "margin": "md"},
                    *buttons,
                ],
            },
        },
    }



def hr_request_blank(request_type: str, line_user_id: str = "") -> dict[str, Any]:
    today = dt.date.today().isoformat()
    return {
        "request_type": request_type,
        "employee_name": get_line_display_name(line_user_id),
        "start_date": today,
        "end_date": today,
        "work_date": today,
        "old_date": "",
        "new_date": "",
        "old_time": "",
        "new_time": "",
        "reason": "",
        "note": "",
        "status": "รออนุมัติ",
    }


def hr_request_form(data: dict[str, Any]) -> str:
    request_type = str(data.get("request_type") or "คำขอ HR")
    common = (
        f"==== {request_type} ====\n"
        "กรุณากรอก/แก้ไขเฉพาะหัวข้อที่ต้องการ แล้วส่งกลับมาได้เลยค่ะ\n\n"
        f"ชื่อพนักงาน: {data.get('employee_name') or ''}\n"
    )
    if request_type in {"ลาป่วย", "ลากิจ", "แจ้งขอวันหยุดล่วงหน้า"}:
        return (
            common +
            f"วันที่เริ่ม: {data.get('start_date') or ''}\n"
            f"วันที่สิ้นสุด: {data.get('end_date') or ''}\n"
            f"เหตุผล: {data.get('reason') or ''}\n"
            f"หมายเหตุ: {data.get('note') or ''}"
        )
    if request_type == "แจ้งเปลี่ยนเวลาเข้า-ออกงาน":
        return (
            common +
            f"วันที่ทำงาน: {data.get('work_date') or ''}\n"
            f"เวลาเดิม: {data.get('old_time') or ''}\n"
            f"เวลาใหม่: {data.get('new_time') or ''}\n"
            f"เหตุผล: {data.get('reason') or ''}\n"
            f"หมายเหตุ: {data.get('note') or ''}"
        )
    if request_type == "แจ้งเปลี่ยนวันทำงาน":
        return (
            common +
            f"วันที่เดิม: {data.get('old_date') or ''}\n"
            f"วันที่ใหม่: {data.get('new_date') or ''}\n"
            f"เหตุผล: {data.get('reason') or ''}\n"
            f"หมายเหตุ: {data.get('note') or ''}"
        )
    return common + f"รายละเอียด: {data.get('note') or ''}"


def format_hr_request(data: dict[str, Any]) -> str:
    return (
        f"==== คำขอ HR ====\n"
        f"ประเภท: {data.get('request_type') or '-'}\n"
        f"ชื่อพนักงาน: {data.get('employee_name') or '-'}\n"
        f"วันที่เริ่ม: {data.get('start_date') or '-'}\n"
        f"วันที่สิ้นสุด: {data.get('end_date') or '-'}\n"
        f"วันที่ทำงาน: {data.get('work_date') or '-'}\n"
        f"วันที่เดิม: {data.get('old_date') or '-'}\n"
        f"วันที่ใหม่: {data.get('new_date') or '-'}\n"
        f"เวลาเดิม: {data.get('old_time') or '-'}\n"
        f"เวลาใหม่: {data.get('new_time') or '-'}\n"
        f"เหตุผล: {data.get('reason') or '-'}\n"
        f"หมายเหตุ: {data.get('note') or '-'}\n"
        f"สถานะ: {data.get('status') or 'รออนุมัติ'}"
    )


def hr_confirm_message(data: dict[str, Any]) -> dict[str, Any]:
    return quick_reply_text_message(
        format_hr_request(data) + "\n\n"
        "กรุณาตรวจสอบข้อมูลก่อนส่งคำขออนุมัติ\n"
        "1 = ยืนยันส่งคำขอ\n"
        "2 = แก้ไขข้อมูล",
        [
            ("1 ยืนยัน", "1"),
            ("2 แก้ไข", "2"),
        ],
    )


def parse_hr_request_text(text: str, data: dict[str, Any]) -> dict[str, Any]:
    updated = dict(data)
    aliases = {
        "ประเภท": "request_type",
        "ชื่อพนักงาน": "employee_name",
        "ชื่อ": "employee_name",
        "วันที่เริ่ม": "start_date",
        "วันที่สิ้นสุด": "end_date",
        "วันที่ลา": "start_date",
        "วันที่ทำงาน": "work_date",
        "วันที่เดิม": "old_date",
        "วันที่ใหม่": "new_date",
        "เวลาเดิม": "old_time",
        "เวลาใหม่": "new_time",
        "เหตุผล": "reason",
        "หมายเหตุ": "note",
        "รายละเอียด": "note",
    }
    normalized_aliases = {re.sub(r"[\s/_-]+", "", k.lower()): v for k, v in aliases.items()}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^(แก้ไข|เปลี่ยน)\s*", "", line)
        match = re.match(r"^([^:=:\-]+)\s*[:=:\-]\s*(.*)$", line)
        if not match:
            match = re.match(r"^(\S+)\s+(.+)$", line)
        if not match:
            continue
        raw_key = match.group(1).strip().lower()
        value = match.group(2).strip()
        key = aliases.get(raw_key) or normalized_aliases.get(re.sub(r"[\s/_-]+", "", raw_key))
        if not key:
            continue
        if key.endswith("date") or key in {"start_date", "end_date", "work_date", "old_date", "new_date"}:
            parsed = parse_date(value)
            updated[key] = parsed.isoformat() if parsed else value
        else:
            updated[key] = value
    return updated


def abort_flow_message(reason: str) -> str:
    return (
        f"{reason}\n\n"
        "ระบบหยุดงานรายการนี้ให้แล้วค่ะ สามารถเริ่มทำรายการใหม่ได้เลย\n\n"
        + menu_text()
    )


def blank_manual_entry(transaction_type: str) -> dict[str, Any]:
    normalized_type = "Revenue" if str(transaction_type).lower() == "revenue" else "Expense"
    category = CONFIG.get("default_revenue_category", "Sales") if normalized_type == "Revenue" else CONFIG.get("default_category", "Other")
    return {
        "transaction_type": normalized_type,
        "document_type": "",
        "date": dt.date.today(),
        "invoice_no": "-",
        "vendor": "",
        "description": "Manual entry from LINE",
        "category": category,
        "before_vat": 0,
        "vat": 0,
        "withholding_tax": 0,
        "total": 0,
        "claimable": CONFIG.get("default_claimable", "Yes"),
        "confidence": 0,
        "raw_text": "",
        "submitter_name": "",
    }


def manual_entry_form(data: dict[str, Any]) -> str:
    return (
        "OCR อ่านเอกสารไม่สำเร็จหรือใช้เวลานานเกินไปค่ะ\n"
        "กรุณากรอกรายละเอียดตามแบบฟอร์มนี้ แล้วส่งกลับมาได้เลยค่ะ\n\n"
        f"ประเภทเอกสาร: {data.get('document_type') or ''}\n"
        f"วันที่: {data.get('date') or ''}\n"
        f"เลขที่บิล: {normalize_invoice_no(data.get('invoice_no'))}\n"
        f"ชื่อร้าน/คู่ค้า: {data.get('vendor') or ''}\n"
        f"ผู้นำส่งเอกสาร: {data.get('submitter_name') or ''}\n"
        f"หมวด: {data.get('category') or ''}\n"
        f"ยอดก่อน VAT: {float(data.get('before_vat') or 0):.2f}\n"
        f"VAT: {float(data.get('vat') or 0):.2f}\n"
        f"ภาษีหัก ณ ที่จ่าย: {float(data.get('withholding_tax') or 0):.2f}\n"
        f"ยอดรวม: {float(data.get('total') or 0):.2f}"
    )


def confirm_edit_button(label: str, text: str, background: str, icon: str) -> dict[str, Any]:
    return {
        "type": "box",
        "layout": "horizontal",
        "cornerRadius": "24px",
        "backgroundColor": background,
        "paddingAll": "18px",
        "spacing": "14px",
        "action": {"type": "message", "label": label[:20], "text": text},
        "contents": [
            {
                "type": "box",
                "layout": "vertical",
                "width": "52px",
                "height": "52px",
                "cornerRadius": "26px",
                "backgroundColor": "#FFFFFF",
                "alignItems": "center",
                "justifyContent": "center",
                "contents": [
                    {
                        "type": "text",
                        "text": icon,
                        "weight": "bold",
                        "size": "xxl",
                        "color": background,
                        "align": "center",
                    }
                ],
            },
            {
                "type": "text",
                "text": label,
                "weight": "bold",
                "size": "xxl",
                "color": "#FFFFFF",
                "gravity": "center",
                "wrap": True,
                "flex": 1,
            },
        ],
    }


def confirm_edit_buttons_message() -> dict[str, Any]:
    return {
        "type": "flex",
        "altText": "ยืนยันหรือแก้ไขข้อมูล",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#FFFFFF",
                "paddingAll": "18px",
                "spacing": "16px",
                "contents": [
                    confirm_edit_button("1. ยืนยัน", "1", "#10B981", "✓"),
                    confirm_edit_button("2. แก้ไข", "2", "#8B5CF6", "✎"),
                ],
            },
        },
    }


def approval_buttons_message(request_id: str) -> dict[str, Any]:
    return {
        "type": "flex",
        "altText": "อนุมัติหรือไม่อนุมัติ",
        "contents": {
            "type": "bubble",
            "size": "mega",
            "body": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#FFFFFF",
                "paddingAll": "18px",
                "spacing": "16px",
                "contents": [
                    confirm_edit_button("1. อนุมัติ", f"HR_APPROVE:{request_id}", "#10B981", "✓"),
                    confirm_edit_button("2. ไม่อนุมัติ", f"HR_REJECT:{request_id}", "#EF4444", "✕"),
                ],
            },
        },
    }


def confirmation_prompt(data: dict[str, Any]) -> list[dict[str, Any]]:
    detail_text = (
        format_parsed_details(data, "บิลนำเข้า") + "\n\n"
        "กรุณาตรวจสอบข้อมูลก่อนบันทึกลง Google Sheet ค่ะ\n"
        "กดปุ่มด้านล่าง หรือพิมพ์เลข 1/2 ได้เลย"
    )
    return [text_message(detail_text), confirm_edit_buttons_message()]


def confirm_pending_to_google(line_user_id: str, state: dict[str, Any], public_base_url: str) -> str | list[dict[str, Any]]:
    pending = deserialize_data(state["pending_data"])
    if not str(pending.get("submitter_name") or "").strip():
        state["mode"] = "awaiting_submitter_name"
        state["pending_data"] = serialize_data(pending)
        set_user_state(line_user_id, state)
        return "กรุณาระบุชื่อผู้นำส่งเอกสารก่อนบันทึกค่ะ"
    if not state.get("duplicate_checked"):
        try:
            matches = search_google_sheet_by_total(pending.get("total"))
        except Exception as exc:
            runtime_log(f"Duplicate total check failed: {exc}")
            matches = []
        if matches:
            state["mode"] = "awaiting_duplicate_confirmation"
            state["duplicate_matches"] = matches[:10]
            state["pending_data"] = serialize_data(pending)
            set_user_state(line_user_id, state)
            return (
                format_google_sheet_matches(matches, "พบรายการยอดรวมซ้ำใน Google Sheet") +
                "\n\nรายการนี้เป็นข้อมูลตัวเดียวกันหรือไม่?\n"
                "ตอบ 1 = ใช่ เป็นรายการเดียวกัน\n"
                "ตอบ 2 = ไม่ใช่ บันทึกเป็นรายการใหม่"
            )
    image_path = Path(state.get("image_path", ""))
    if not image_path.exists():
        return "ไม่พบไฟล์รูปเอกสารเดิมค่ะ กรุณาส่งเอกสารใหม่อีกครั้ง"
    result = None
    try:
        result = send_to_google_sheet(pending, image_path, line_user_id, public_base_url)
    except Exception as exc:
        runtime_log(f"Google Sheet save failed: {exc}")
        clear_user_state(line_user_id)
        return abort_flow_message(f"Google Sheet: ยังไม่สำเร็จ ({exc})")
    summary_image = render_row_summary_image("Google Sheet", "-", pending, "บิลนำเข้า")
    messages = [
        text_message("บันทึกเรียบร้อย\nGoogle Sheet: บันทึกสำเร็จ"),
        image_message(public_file_url(public_base_url, summary_image)),
    ]
    substitute_match_data = substitute_match_from_pending(pending, result)
    if can_create_substitute_receipt(substitute_match_data):
        set_user_state(
            line_user_id,
            {
                "mode": "awaiting_substitute_receipt_decision",
                "substitute_match": substitute_match_data,
            },
        )
        messages.append(
            quick_reply_text_message(
                "บันทึกเป็นบิล/บิลเงินสดเรียบร้อยค่ะ\n\n"
                "ต้องการสร้างใบแทนสำหรับพิมพ์เก็บเป็น hard copy ไหมคะ?\n"
                "เลือกปุ่มด้านล่าง หรือพิมพ์เลขตอบกลับได้เลย\n\n"
                "1 = ต้องการสร้างใบแทน\n"
                "2 = ไม่ต้องการ",
                [
                    ("🧾 1 สร้างใบแทน", "1"),
                    ("ไม่สร้างใบแทน", "2"),
                ],
            )
        )
    else:
        clear_user_state(line_user_id)
    runtime_log(
        f"Confirmed LINE receipt -> Google Sheet "
        f"type={pending.get('transaction_type')} date={pending['date']} total={pending['total']}"
    )
    return messages


def parse_correction_text(text: str, data: dict[str, Any]) -> dict[str, Any]:
    updated = dict(data)
    aliases = {
        "date": "date",
        "วันที่": "date",
        "invoice": "invoice_no",
        "invoice no": "invoice_no",
        "invoice number": "invoice_no",
        "เลขที่เอกสาร": "invoice_no",
        "เลขที่บิล": "invoice_no",
        "เลขที่ใบเสร็จ": "invoice_no",
        "ประเภทเอกสาร": "document_type",
        "ประเภทบิล": "document_type",
        "ชนิดเอกสาร": "document_type",
        "document type": "document_type",
        "vendor": "vendor",
        "supplier": "vendor",
        "ร้าน": "vendor",
        "ผู้ขาย": "vendor",
        "คู่ค้า": "vendor",
        "category": "category",
        "หมวด": "category",
        "description": "description",
        "รายละเอียด": "description",
        "before vat": "before_vat",
        "ยอดก่อน vat": "before_vat",
        "ยอดก่อนภาษี": "before_vat",
        "vat": "vat",
        "ภาษี": "vat",
        "total": "total",
        "ยอดรวม": "total",
    }
    changed = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^([^:=：]+)\s*[:=：]\s*(.+)$", line)
        if not match:
            continue
        raw_key, value = match.group(1).strip().lower(), match.group(2).strip()
        key = aliases.get(raw_key)
        if not key:
            continue
        if key == "date":
            parsed_date = parse_date(value)
            if parsed_date:
                updated[key] = parsed_date
                changed = True
        elif key in {"before_vat", "vat", "total"}:
            amount = normalize_amount(value)
            if amount is not None:
                updated[key] = round(amount, 2)
                changed = True
        else:
            updated[key] = value
            changed = True

    if changed:
        if updated.get("total") and updated.get("vat") and not updated.get("before_vat"):
            updated["before_vat"] = round(float(updated["total"]) - float(updated["vat"]), 2)
        elif updated.get("before_vat") and updated.get("vat"):
            updated["total"] = round(float(updated["before_vat"]) + float(updated["vat"]), 2)
    return updated


def parse_correction_text_v2(text: str, data: dict[str, Any]) -> dict[str, Any]:
    updated = dict(data)
    changed_fields: set[str] = set()

    aliases = {
        "date": "date",
        "\u0e27\u0e31\u0e19\u0e17\u0e35\u0e48": "date",
        "invoice": "invoice_no",
        "invoice no": "invoice_no",
        "invoice number": "invoice_no",
        "\u0e40\u0e25\u0e02\u0e17\u0e35\u0e48\u0e40\u0e2d\u0e01\u0e2a\u0e32\u0e23": "invoice_no",
        "\u0e40\u0e25\u0e02\u0e17\u0e35\u0e48\u0e1a\u0e34\u0e25": "invoice_no",
        "\u0e40\u0e25\u0e02\u0e17\u0e35\u0e48\u0e43\u0e1a\u0e40\u0e2a\u0e23\u0e47\u0e08": "invoice_no",
        "type": "transaction_type",
        "\u0e1b\u0e23\u0e30\u0e40\u0e20\u0e17": "transaction_type",
        "\u0e1b\u0e23\u0e30\u0e40\u0e20\u0e17\u0e23\u0e32\u0e22\u0e01\u0e32\u0e23": "transaction_type",
        "\u0e1b\u0e23\u0e30\u0e40\u0e20\u0e17\u0e40\u0e2d\u0e01\u0e2a\u0e32\u0e23": "document_type",
        "\u0e1b\u0e23\u0e30\u0e40\u0e20\u0e17\u0e1a\u0e34\u0e25": "document_type",
        "\u0e0a\u0e19\u0e34\u0e14\u0e40\u0e2d\u0e01\u0e2a\u0e32\u0e23": "document_type",
        "document type": "document_type",
        "vendor": "vendor",
        "supplier": "vendor",
        "\u0e23\u0e49\u0e32\u0e19": "vendor",
        "\u0e1c\u0e39\u0e49\u0e02\u0e32\u0e22": "vendor",
        "\u0e04\u0e39\u0e48\u0e04\u0e49\u0e32": "vendor",
        "\u0e0a\u0e37\u0e48\u0e2d\u0e23\u0e49\u0e32\u0e19": "vendor",
        "\u0e0a\u0e37\u0e48\u0e2d\u0e23\u0e49\u0e32\u0e19/\u0e04\u0e39\u0e48\u0e04\u0e49\u0e32": "vendor",
        "\u0e0a\u0e37\u0e48\u0e2d\u0e1c\u0e39\u0e49\u0e02\u0e32\u0e22": "vendor",
        "\u0e1c\u0e39\u0e49\u0e19\u0e33\u0e2a\u0e48\u0e07\u0e40\u0e2d\u0e01\u0e2a\u0e32\u0e23": "submitter_name",
        "\u0e0a\u0e37\u0e48\u0e2d\u0e1c\u0e39\u0e49\u0e19\u0e33\u0e2a\u0e48\u0e07": "submitter_name",
        "submitter": "submitter_name",
        "submitter name": "submitter_name",
        "category": "category",
        "\u0e2b\u0e21\u0e27\u0e14": "category",
        "description": "description",
        "\u0e23\u0e32\u0e22\u0e25\u0e30\u0e40\u0e2d\u0e35\u0e22\u0e14": "description",
        "before vat": "before_vat",
        "\u0e22\u0e2d\u0e14\u0e01\u0e48\u0e2d\u0e19 vat": "before_vat",
        "\u0e22\u0e2d\u0e14\u0e01\u0e48\u0e2d\u0e19\u0e20\u0e32\u0e29\u0e35": "before_vat",
        "vat": "vat",
        "\u0e20\u0e32\u0e29\u0e35": "vat",
        "withholding tax": "withholding_tax",
        "wht": "withholding_tax",
        "tax withheld": "withholding_tax",
        "\u0e20\u0e32\u0e29\u0e35\u0e2b\u0e31\u0e01 \u0e13 \u0e17\u0e35\u0e48\u0e08\u0e48\u0e32\u0e22": "withholding_tax",
        "\u0e20\u0e32\u0e29\u0e35\u0e2b\u0e31\u0e01\u0e13\u0e17\u0e35\u0e48\u0e08\u0e48\u0e32\u0e22": "withholding_tax",
        "total": "total",
        "\u0e22\u0e2d\u0e14\u0e23\u0e27\u0e21": "total",
    }
    normalized_aliases = {
        re.sub(r"[\s/_-]+", "", key.lower()): value
        for key, value in aliases.items()
    }
    keyword_pattern = "|".join(re.escape(key) for key in sorted(aliases, key=len, reverse=True))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[\-\*\u2022]\s*", "", line)
        line = re.sub(r"^(แก้ไข|เปลี่ยน)\s*", "", line)
        match = re.match(r"^([^:=:\-]+)\s*[:=:\-]\s*(.+)$", line)
        if not match:
            match = re.match(rf"^({keyword_pattern})\s+(.+)$", line, flags=re.IGNORECASE)
        if not match:
            continue

        raw_key = match.group(1).strip().lower()
        value = match.group(2).strip()
        normalized_key = re.sub(r"[\s/_-]+", "", raw_key)
        key = aliases.get(raw_key) or normalized_aliases.get(normalized_key)
        if not key:
            continue

        if key == "date":
            parsed_date = parse_date(value)
            if not parsed_date:
                continue
            updated[key] = parsed_date
        elif key in {"before_vat", "vat", "withholding_tax", "total"}:
            amount = normalize_amount(value)
            if amount is None:
                continue
            updated[key] = round(amount, 2)
        elif key == "document_type":
            normalized_doc_type = value.strip()
            if normalized_doc_type in {"bill"}:
                normalized_doc_type = "\u0e1a\u0e34\u0e25"
            elif normalized_doc_type in {"receipt"}:
                normalized_doc_type = "\u0e43\u0e1a\u0e40\u0e2a\u0e23\u0e47\u0e08"
            elif normalized_doc_type in {"tax invoice", "invoice"}:
                normalized_doc_type = "\u0e43\u0e1a\u0e01\u0e33\u0e01\u0e31\u0e1a\u0e20\u0e32\u0e29\u0e35"
            updated[key] = normalized_doc_type
        elif key == "transaction_type":
            normalized_type = value.strip().lower()
            if normalized_type in {"revenue", "\u0e23\u0e32\u0e22\u0e23\u0e31\u0e1a", "\u0e1a\u0e34\u0e25\u0e23\u0e32\u0e22\u0e23\u0e31\u0e1a"}:
                updated[key] = "Revenue"
            elif normalized_type in {"expense", "expenses", "\u0e23\u0e32\u0e22\u0e08\u0e48\u0e32\u0e22", "\u0e1a\u0e34\u0e25\u0e23\u0e32\u0e22\u0e08\u0e48\u0e32\u0e22"}:
                updated[key] = "Expense"
            else:
                updated[key] = value
        elif key == "invoice_no":
            updated[key] = normalize_invoice_no(value)
        else:
            updated[key] = value
        changed_fields.add(key)

    if changed_fields:
        before_changed = "before_vat" in changed_fields
        vat_changed = "vat" in changed_fields
        total_changed = "total" in changed_fields
        before = float(updated.get("before_vat") or 0)
        vat = float(updated.get("vat") or 0)
        total = float(updated.get("total") or 0)
        if before_changed and not vat_changed and not total_changed:
            vat = round(before * float(CONFIG.get("vat_rate", 0.07)), 2)
            total = round(before + vat, 2)
            updated["vat"] = vat
            updated["total"] = total
        elif before_changed and vat_changed and not total_changed:
            updated["total"] = round(before + vat, 2)
        elif total_changed and vat_changed and not before_changed:
            updated["before_vat"] = round(total - vat, 2)
        elif total_changed and before_changed and not vat_changed:
            updated["vat"] = round(total - before, 2)
        elif not total_changed and (before_changed or vat_changed):
            updated["total"] = round(before + vat, 2)

    return updated


def find_font(size: int):
    from PIL import ImageFont

    candidates = [
        r"C:\Windows\Fonts\tahoma.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        "/usr/share/fonts/truetype/tlwg/Garuda.ttf",
        "/usr/share/fonts/truetype/tlwg/Garuda-Bold.ttf",
        "/usr/share/fonts/truetype/tlwg/Loma.ttf",
        "/usr/share/fonts/truetype/tlwg/Loma-Bold.ttf",
        "/usr/share/fonts/truetype/tlwg/TlwgTypist.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThaiLooped-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThaiLoopedUI-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThaiUI-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansThaiUI-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    for root in (Path("/usr/share/fonts/truetype/noto"), Path("/usr/share/fonts/opentype/noto")):
        if root.exists():
            for pattern in ("NotoSansThai*.ttf", "NotoSansThai*.otf", "NotoSerifThai*.ttf", "NotoSerifThai*.otf"):
                for candidate in sorted(root.glob(pattern)):
                    return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def wrap_text(text: str, max_chars: int) -> list[str]:
    text = str(text or "-")
    if len(text) <= max_chars:
        return [text]
    lines: list[str] = []
    current = ""
    for part in text.split():
        if len(current) + len(part) + 1 <= max_chars:
            current = f"{current} {part}".strip()
        else:
            if current:
                lines.append(current)
            current = part
    if current:
        lines.append(current)
    if not lines:
        lines = [text[i:i + max_chars] for i in range(0, len(text), max_chars)]
    return lines[:3]


def render_row_summary_image(sheet_name: str, row: int, data: dict[str, Any], heading: str) -> Path:
    from PIL import Image, ImageDraw

    out_dir = reply_image_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"excel_row_{row}_{uuid.uuid4().hex[:8]}.png"
    width, height = 1280, 980
    image = Image.new("RGB", (width, height), "#F8FAFC")
    draw = ImageDraw.Draw(image)
    title_font = find_font(36)
    label_font = find_font(25)
    value_font = find_font(25)
    small_font = find_font(21)

    accent = "#BBF7D0" if data.get("transaction_type") == "Revenue" else "#FECACA"
    draw.rounded_rectangle((28, 28, width - 28, 120), radius=22, fill=accent)
    draw.text((58, 52), heading, fill="#111827", font=title_font)
    draw.text((width - 440, 62), f"{sheet_name}!Row {row}", fill="#374151", font=small_font)

    fields = [
        ("ประเภท", data.get("transaction_type", "-")),
        ("ประเภทเอกสาร", data.get("document_type") or "-"),
        ("วันที่", str(data.get("date", "-"))),
        ("เลขที่บิล", data.get("invoice_no") or "-"),
        ("ชื่อร้าน/คู่ค้า", data.get("vendor") or "-"),
        ("ผู้นำส่งเอกสาร", data.get("submitter_name") or "-"),
        ("หมวด", data.get("category") or "-"),
        ("รายละเอียด", data.get("description") or "-"),
        ("ยอดก่อน VAT", f"{float(data.get('before_vat') or 0):,.2f}"),
        ("VAT", f"{float(data.get('vat') or 0):,.2f}"),
        ("ภาษีหัก ณ ที่จ่าย", f"{float(data.get('withholding_tax') or 0):,.2f}"),
        ("ยอดรวม", f"{float(data.get('total') or 0):,.2f}"),
    ]
    y = 155
    for label, value in fields:
        wrapped_value = wrap_text(str(value), 54)
        box_height = max(58, 30 + (len(wrapped_value) * 28))
        draw.rounded_rectangle((42, y - 8, width - 42, y + box_height), radius=10, fill="#FFFFFF", outline="#E5E7EB")
        draw.text((70, y), label, fill="#475569", font=label_font)
        line_y = y
        for value_line in wrapped_value:
            draw.text((395, line_y), value_line, fill="#111827", font=value_font)
            line_y += 28
        y += box_height + 8
    image.save(path, quality=92)
    return path


def is_substitute_receipt_doc(document_type: Any) -> bool:
    normalized = re.sub(r"\s+", "", str(document_type or "").lower())
    return (
        normalized in {"บิล", "บิลเงินสด", "bill", "cashbill"}
        or "บิล" in normalized
    )


def can_create_substitute_receipt(item: dict[str, Any]) -> bool:
    item_type = str(item.get("type") or "").lower()
    return item_type == "expense" and (
        is_substitute_receipt_doc(item.get("documentType"))
        or not str(item.get("documentType") or "").strip()
    )


def substitute_match_from_pending(data: dict[str, Any], result: dict[str, Any] | None = None) -> dict[str, Any]:
    result = result or {}
    return {
        "row": result.get("row") or "-",
        "sheetName": result.get("sheetName") or result.get("sheet") or "Google Sheet",
        "date": data.get("date") or "-",
        "type": data.get("transaction_type") or data.get("type") or "Expense",
        "invoiceNo": data.get("invoice_no") or data.get("invoiceNo") or "-",
        "vendor": data.get("vendor") or "-",
        "description": data.get("description") or "-",
        "category": data.get("category") or "-",
        "beforeVat": data.get("before_vat") or data.get("beforeVat") or 0,
        "vat": data.get("vat") or 0,
        "withholdingTax": data.get("withholding_tax") or data.get("withholdingTax") or 0,
        "total": data.get("total") or 0,
        "documentType": data.get("document_type") or data.get("documentType") or "-",
        "submitterName": data.get("submitter_name") or data.get("submitterName") or "-",
    }


def substitute_data_from_match(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": item.get("date") or "-",
        "transaction_type": item.get("type") or "Expense",
        "invoice_no": item.get("invoiceNo") or "-",
        "vendor": item.get("vendor") or "-",
        "description": item.get("description") or "ค่าใช้จ่ายตามบิล/บิลเงินสด",
        "category": item.get("category") or "-",
        "before_vat": float(item.get("beforeVat") or 0),
        "vat": float(item.get("vat") or 0),
        "withholding_tax": float(item.get("withholdingTax") or item.get("withholding_tax") or 0),
        "total": float(item.get("total") or 0),
        "document_type": item.get("documentType") or "-",
        "submitter_name": item.get("submitterName") or "-",
        "sheet_name": item.get("sheetName") or "-",
        "row": item.get("row") or "-",
    }


def render_substitute_receipt_image(data: dict[str, Any]) -> Path:
    from PIL import Image, ImageDraw

    out_dir = reply_image_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"substitute_receipt_{data.get('row', '-')}_{uuid.uuid4().hex[:8]}.png"
    width, height = 1280, 1700
    image = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)
    title_font = find_font(42)
    header_font = find_font(30)
    label_font = find_font(25)
    small_font = find_font(21)

    draw.rectangle((38, 38, width - 38, height - 38), outline="#111827", width=3)
    draw.text((width // 2 - 250, 70), "ใบรับรองแทนใบเสร็จรับเงิน", fill="#111827", font=title_font)
    draw.text((width // 2 - 280, 125), "กรณีไม่มีใบเสร็จรับเงิน/ได้รับเพียงบิลหรือบิลเงินสด", fill="#374151", font=small_font)
    draw.line((80, 175, width - 80, 175), fill="#111827", width=2)

    y = 210
    rows = [
        ("วันที่จัดทำใบแทน", dt.date.today().isoformat()),
        ("วันที่ตามเอกสาร/วันที่จ่าย", str(data.get("date") or "-")),
        ("อ้างอิง Google Sheet", f"{data.get('sheet_name')} Row {data.get('row')}"),
        ("ประเภทเอกสารเดิม", data.get("document_type") or "-"),
        ("เลขที่บิล/เอกสาร", data.get("invoice_no") or "-"),
        ("ชื่อร้าน/คู่ค้า/ผู้รับเงิน", data.get("vendor") or "-"),
        ("หมวดค่าใช้จ่าย", data.get("category") or "-"),
        ("รายละเอียดค่าใช้จ่าย", data.get("description") or "-"),
        ("ผู้นำส่งเอกสาร", data.get("submitter_name") or "-"),
    ]
    for label, value in rows:
        draw.text((85, y), label, fill="#374151", font=label_font)
        wrapped = wrap_text(str(value), 48)
        line_y = y
        for line in wrapped:
            draw.text((430, line_y), line, fill="#111827", font=label_font)
            line_y += 32
        y += max(48, len(wrapped) * 34)

    y += 25
    draw.rounded_rectangle((80, y, width - 80, y + 190), radius=12, fill="#F8FAFC", outline="#CBD5E1")
    draw.text((110, y + 28), "ยอดก่อน VAT", fill="#374151", font=header_font)
    draw.text((760, y + 28), f"{float(data.get('before_vat') or 0):,.2f} บาท", fill="#111827", font=header_font)
    draw.text((110, y + 82), "VAT", fill="#374151", font=header_font)
    draw.text((760, y + 82), f"{float(data.get('vat') or 0):,.2f} บาท", fill="#111827", font=header_font)
    draw.text((110, y + 136), "ยอดรวม", fill="#111827", font=header_font)
    draw.text((760, y + 136), f"{float(data.get('total') or 0):,.2f} บาท", fill="#111827", font=header_font)

    y += 240
    note_lines = [
        "ข้าพเจ้าขอรับรองว่าได้จ่ายเงินตามรายการข้างต้นจริง และไม่สามารถเรียก/รับใบเสร็จรับเงิน",
        "หรือเอกสารภาษีที่สมบูรณ์จากผู้รับเงินได้ จึงจัดทำใบรับรองแทนใบเสร็จรับเงินฉบับนี้",
        "เพื่อใช้เป็นหลักฐานประกอบการบันทึกค่าใช้จ่าย โปรดแนบหลักฐานการจ่ายเงิน/รูปบิลเดิมทุกครั้ง",
        "หมายเหตุ: กรณีใช้ยื่นภาษี ควรให้ผู้ทำบัญชีหรือที่ปรึกษาภาษีตรวจสอบความเหมาะสมก่อนยื่น",
    ]
    for line in note_lines:
        draw.text((90, y), line, fill="#111827", font=small_font)
        y += 34

    y += 70
    signature_blocks = [
        ("ผู้จ่ายเงิน/ผู้ขอเบิก", data.get("submitter_name") or ""),
        ("ผู้ตรวจสอบ/ผู้อนุมัติ", ""),
        ("ผู้รับเงิน", data.get("vendor") or ""),
    ]
    block_width = 350
    x_positions = [90, 465, 840]
    for x, (label, name) in zip(x_positions, signature_blocks):
        draw.line((x, y, x + block_width, y), fill="#111827", width=2)
        draw.text((x + 45, y + 15), label, fill="#374151", font=small_font)
        draw.text((x + 35, y + 48), f"({name or '________________'})", fill="#111827", font=small_font)
        draw.text((x + 65, y + 82), "วันที่ ____/____/______", fill="#374151", font=small_font)

    draw.text((90, height - 90), "เอกสารสร้างจากระบบ LINE VAT Bot และข้อมูลใน Google Sheet", fill="#64748B", font=small_font)
    image.save(path, quality=94)
    return path


def render_substitute_receipt_pdf(image_path: Path) -> Path:
    from PIL import Image

    pdf_path = image_path.with_suffix(".pdf")
    with Image.open(image_path) as image:
        image.convert("RGB").save(pdf_path, "PDF", resolution=150.0)
    return pdf_path


def substitute_receipt_messages(matches: list[dict[str, Any]], public_base_url: str, line_user_id: str = "") -> list[dict[str, Any]]:
    data = substitute_data_from_match(matches[0])
    image_path = render_substitute_receipt_image(data)
    pdf_path = render_substitute_receipt_pdf(image_path)
    image_url = public_file_url(public_base_url, image_path)
    pdf_url = public_file_url(public_base_url, pdf_path)
    save_note = "บันทึกประวัติใบแทนลง Google Sheet แล้ว"
    try:
        result = save_substitute_receipt_to_google(data, image_url, pdf_url, line_user_id)
        save_note = f"บันทึกประวัติใบแทนลง Google Sheet แล้ว ({result.get('sheetName')} Row {result.get('row')})"
    except Exception as exc:
        runtime_log(f"Save substitute receipt record failed: {exc}")
        save_note = f"สร้างใบแทนสำเร็จ แต่บันทึกประวัติลง Google Sheet ไม่สำเร็จชั่วคราว ({exc})"
    return [
        text_message(
            "สร้างใบแทนเรียบร้อย\n"
            f"อ้างอิง {data.get('sheet_name')} Row {data.get('row')}\n"
            f"{save_note}\n"
            "โปรดตรวจสอบและให้ผู้มีอำนาจลงนามก่อนใช้เป็นเอกสารประกอบบัญชี\n"
            f"PDF สำหรับพิมพ์: {pdf_url}"
        ),
        image_message(image_url),
    ]


def public_file_url(base_url: str, path: Path) -> str:
    return f"{base_url.rstrip('/')}/files/{path.name}"


def download_binary(url: str, timeout: int = 45) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "LineExpenseBot/2.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def render_pdf_first_page_to_jpeg(pdf_bytes: bytes, label: str = "schedule") -> Path:
    import fitz
    from PIL import Image

    out_dir = reply_image_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_") or "schedule"
    path = out_dir / f"{safe_label}_{uuid.uuid4().hex[:8]}.jpg"
    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = document.load_page(0)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2), alpha=False)
        png_bytes = pixmap.tobytes("png")
    finally:
        document.close()
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    image.save(path, "JPEG", quality=92, optimize=True)
    return path


def to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        amount = normalize_amount(value)
        if amount is not None:
            return float(amount)
    return 0.0


def transaction_row_data(sheet, row: int) -> dict[str, Any]:
    data = {
        "date": sheet.cell(row=row, column=1).value,
        "transaction_type": sheet.cell(row=row, column=2).value,
        "invoice_no": sheet.cell(row=row, column=3).value,
        "document_type": sheet.cell(row=row, column=18).value,
        "vendor": sheet.cell(row=row, column=4).value,
        "description": sheet.cell(row=row, column=5).value,
        "category": sheet.cell(row=row, column=6).value,
        "before_vat": sheet.cell(row=row, column=7).value or 0,
        "confidence": sheet.cell(row=row, column=14).value or 0,
    }
    before_vat = to_float(data["before_vat"])
    vat_rate = float(CONFIG.get("vat_rate", 0.07))
    data["vat"] = round(before_vat * vat_rate, 2)
    data["total"] = round(before_vat + data["vat"], 2)
    return data


def find_bill_in_transactions(bill_no: str) -> tuple[str, int, dict[str, Any]] | None:
    workbook_path = resolve_path(CONFIG["workbook"])
    wb = load_workbook(workbook_path, data_only=False)
    if "Transactions_12M" not in wb.sheetnames:
        return None
    sheet = wb["Transactions_12M"]
    target = bill_no.strip().lower()
    for row in range(4, sheet.max_row + 1):
        current = sheet.cell(row=row, column=3).value
        if current and str(current).strip().lower() == target:
            return "Transactions_12M", row, transaction_row_data(sheet, row)
    return None


def search_transactions(query: str, max_results: int = 5) -> list[tuple[str, int, dict[str, Any]]]:
    workbook_path = resolve_path(CONFIG["workbook"])
    wb = load_workbook(workbook_path, data_only=False)
    if "Transactions_12M" not in wb.sheetnames:
        return []
    sheet = wb["Transactions_12M"]
    q = query.strip()
    q_lower = q.lower()
    q_amount = normalize_amount(q)
    results: list[tuple[int, str, int, dict[str, Any]]] = []

    for row in range(4, sheet.max_row + 1):
        if not sheet.cell(row=row, column=1).value or not sheet.cell(row=row, column=2).value:
            continue
        data = transaction_row_data(sheet, row)
        if not data.get("transaction_type") in {"Revenue", "Expense"}:
            continue
        invoice = str(data.get("invoice_no") or "").lower()
        vendor = str(data.get("vendor") or "").lower()
        before_vat = float(data.get("before_vat") or 0)
        total = float(data.get("total") or 0)
        score = 0
        if q_lower and invoice and q_lower == invoice:
            score += 100
        if q_lower and invoice and q_lower in invoice:
            score += 60
        if q_lower and vendor and q_lower in vendor:
            score += 50
        if q_amount is not None:
            if abs(total - q_amount) <= 1:
                score += 80
            elif abs(before_vat - q_amount) <= 1:
                score += 55
        if score:
            results.append((score, "Transactions_12M", row, data))

    results.sort(key=lambda item: (item[0], item[2]), reverse=True)
    return [(sheet_name, row, data) for _, sheet_name, row, data in results[:max_results]]


def find_transaction_by_row(row: int) -> tuple[str, int, dict[str, Any]] | None:
    workbook_path = resolve_path(CONFIG["workbook"])
    wb = load_workbook(workbook_path, data_only=False)
    if "Transactions_12M" not in wb.sheetnames:
        return None
    sheet = wb["Transactions_12M"]
    if row < 4 or row > sheet.max_row:
        return None
    if not sheet.cell(row=row, column=1).value or not sheet.cell(row=row, column=2).value:
        return None
    return "Transactions_12M", row, transaction_row_data(sheet, row)


def format_lookup_results(results: list[tuple[str, int, dict[str, Any]]], query: str) -> str:
    lines = [f"พบหลายรายการจากคำค้น: {query}", "กรุณาพิมพ์ Row ที่ต้องการ เช่น Row 170"]
    for sheet_name, row, data in results:
        lines.append(
            f"Row {row}: {data.get('date')} | {data.get('vendor') or '-'} | "
            f"{data.get('invoice_no') or '-'} | Total {float(data.get('total') or 0):,.2f}"
        )
    return "\n".join(lines)


def reset_transaction_formulas(sheet, row: int) -> None:
    sheet.cell(row=row, column=8, value=f'=IF(A{row}="","",Settings!$B$4)')
    sheet.cell(row=row, column=9, value=f'=IF(G{row}="","",IF(OR(B{row}="Revenue",K{row}="Yes"),ROUND(G{row}*H{row},2),0))')
    sheet.cell(row=row, column=10, value=f'=IF(G{row}="","",G{row}+ROUND(G{row}*H{row},2))')
    sheet.cell(row=row, column=12, value=f'=IF(A{row}="","",DATE(YEAR(A{row}),MONTH(A{row}),1))')
    sheet.cell(row=row, column=15, value=f'=IF(B{row}="Revenue",G{row},0)')
    sheet.cell(row=row, column=16, value=f'=IF(B{row}="Expense",G{row},0)')


def clear_transaction_row(sheet, row: int) -> None:
    # Clear user/OCR-entered cells but keep the formula columns ready for reuse.
    for col in (1, 2, 3, 4, 5, 6, 7, 11, 13, 14, 17, 18):
        sheet.cell(row=row, column=col).value = None
    reset_transaction_formulas(sheet, row)


def clear_legacy_expense_row(sheet, row: int) -> None:
    for col in (1, 2, 3, 4, 5, 6, 10, 12, 13, 14, 15):
        sheet.cell(row=row, column=col).value = None
    sheet.cell(row=row, column=7, value=f'=IF(A{row}="","",Settings!$B$4)')
    sheet.cell(row=row, column=8, value=f'=IF(F{row}="","",IF(J{row}="Yes",ROUND(F{row}*G{row},2),0))')
    sheet.cell(row=row, column=9, value=f'=IF(F{row}="","",F{row}+ROUND(F{row}*G{row},2))')
    sheet.cell(row=row, column=11, value=f'=IF(A{row}="","",DATE(YEAR(A{row}),MONTH(A{row}),1))')


def delete_bill_from_excel(bill_no: str, line_user_id: str = "") -> str:
    bill_no = bill_no.strip()
    if not bill_no:
        return "กรุณาพิมพ์เลขที่บิล เช่น แก้ไขบิล+INV001"

    workbook_path = resolve_path(CONFIG["workbook"])
    wb = load_workbook(workbook_path)
    deleted: list[tuple[str, int]] = []

    if "Transactions_12M" in wb.sheetnames:
        sheet = wb["Transactions_12M"]
        for row in range(5, sheet.max_row + 1):
            value = str(sheet.cell(row=row, column=3).value or "").strip()
            if value and value.lower() == bill_no.lower():
                clear_transaction_row(sheet, row)
                deleted.append(("Transactions_12M", row))

    if "Expenses" in wb.sheetnames:
        sheet = wb["Expenses"]
        for row in range(4, sheet.max_row + 1):
            value = str(sheet.cell(row=row, column=2).value or "").strip()
            if value and value.lower() == bill_no.lower():
                clear_legacy_expense_row(sheet, row)
                deleted.append(("Expenses", row))

    log = wb["Import_Log"]
    log_row = next_empty_row(log)
    log.cell(row=log_row, column=1, value=dt.datetime.now())
    log.cell(row=log_row, column=2, value="")
    log.cell(row=log_row, column=3, value="Deleted" if deleted else "DeleteNotFound")
    log.cell(row=log_row, column=4, value=dt.date.today())
    log.cell(row=log_row, column=5, value=bill_no)
    log.cell(row=log_row, column=6, value=0)
    log.cell(row=log_row, column=7, value=0)
    log.cell(row=log_row, column=8, value=f"LINE delete command bill_no={bill_no} rows={deleted} LINE user: {line_user_id}")

    wb.save(workbook_path)

    if not deleted:
        runtime_log(f"Delete command: bill_no={bill_no!r} not found")
        return f"ไม่พบเลขที่บิล: {bill_no}\nกรุณาตรวจเลขที่เอกสารในชีต Transactions_12M"

    runtime_log(f"Delete command: bill_no={bill_no!r} cleared rows={deleted}")
    row_text = ", ".join(f"{sheet}!row {row}" for sheet, row in deleted)
    return (
        "==== บิลยกเลิก ====\n"
        "สถานะ: ยกเลิกบิลเรียบร้อย\n"
        f"เลขที่บิล: {bill_no}\n"
        f"ตำแหน่ง: {row_text}\n"
        "สามารถส่งรูปบิลใหม่หรือกรอกใหม่ได้เลย"
    )


def delete_excel_row(row_text: str, line_user_id: str = "") -> str:
    row_text = row_text.strip()
    if not row_text.isdigit():
        return "กรุณาพิมพ์เลขแถว เช่น Delete Row 173"

    row = int(row_text)
    workbook_path = resolve_path(CONFIG["workbook"])
    wb = load_workbook(workbook_path)

    if "Transactions_12M" not in wb.sheetnames:
        return "ไม่พบชีต Transactions_12M ในไฟล์ Excel"

    sheet = wb["Transactions_12M"]
    if row < 5 or row > sheet.max_row:
        return f"เลขแถว {row} อยู่นอกช่วงข้อมูลที่ลบได้"

    current_date = sheet.cell(row=row, column=1).value
    current_type = sheet.cell(row=row, column=2).value
    current_doc = sheet.cell(row=row, column=3).value
    current_party = sheet.cell(row=row, column=4).value
    current_amount = sheet.cell(row=row, column=7).value

    if not any([current_date, current_type, current_doc, current_party, current_amount]):
        return f"Row {row} ไม่มีข้อมูลให้ลบ"

    clear_transaction_row(sheet, row)

    log = wb["Import_Log"]
    log_row = next_empty_row(log)
    log.cell(row=log_row, column=1, value=dt.datetime.now())
    log.cell(row=log_row, column=2, value="")
    log.cell(row=log_row, column=3, value="DeletedRow")
    log.cell(row=log_row, column=4, value=dt.date.today())
    log.cell(row=log_row, column=5, value=f"Row {row}")
    log.cell(row=log_row, column=6, value=current_amount or 0)
    log.cell(row=log_row, column=7, value=0)
    log.cell(
        row=log_row,
        column=8,
        value=(
            f"LINE Delete Row command row={row} "
            f"old_doc={current_doc} old_party={current_party} LINE user: {line_user_id}"
        ),
    )

    wb.save(workbook_path)
    runtime_log(f"Delete Row command: cleared Transactions_12M!row {row}")
    return (
        "==== บิลยกเลิก ====\n"
        "สถานะ: ยกเลิกบิลเรียบร้อย\n"
        f"Row: {row}\n"
        f"เลขที่เอกสารเดิม: {current_doc or '-'}\n"
        f"ชื่อเดิม: {current_party or '-'}\n"
        "สามารถส่งรูปบิลใหม่หรือกรอกใหม่ได้เลย"
    )


def append_transaction_to_excel(image_path: Path, data: dict[str, Any], status: str, message: str, line_user_id: str = "") -> tuple[str, int]:
    workbook_path = resolve_path(CONFIG["workbook"])
    wb = load_workbook(workbook_path)
    data = apply_transaction_type_defaults(data, data.get("transaction_type", "Expense"))

    if "Transactions_12M" in wb.sheetnames:
        target_sheet = "Transactions_12M"
        sheet = wb[target_sheet]
        row = find_transaction_row(sheet, data["date"])
        append_to_transactions(sheet, row, image_path, data)
        if data.get("transaction_type") == "Expense" and "Expenses" in wb.sheetnames:
            legacy_row = next_empty_row(wb["Expenses"])
            append_to_legacy_expenses(wb["Expenses"], legacy_row, image_path, data)
        elif data.get("transaction_type") == "Revenue" and "Revenue" in wb.sheetnames:
            legacy_row = next_empty_row(wb["Revenue"])
            append_to_legacy_revenue(wb["Revenue"], legacy_row, data)
    else:
        target_sheet = "Expenses"
        sheet = wb[target_sheet]
        row = next_empty_row(sheet)
        append_to_legacy_expenses(sheet, row, image_path, data)

    append_import_log(wb["Import_Log"], image_path, data, status, message, line_user_id)
    wb.save(workbook_path)
    return target_sheet, row


def append_expense_to_excel(image_path: Path, data: dict[str, Any], status: str, message: str, line_user_id: str = "") -> tuple[str, int]:
    data = apply_transaction_type_defaults(data, "Expense")
    return append_transaction_to_excel(image_path, data, status, message, line_user_id)


def build_google_sheet_payload(data: dict[str, Any], image_path: Path, line_user_id: str, public_base_url: str, message: str) -> dict[str, Any]:
    before_vat = float(data.get("before_vat") or 0)
    vat_rate = float(CONFIG.get("vat_rate", 0.07))
    vat = float(data.get("vat") or round(before_vat * vat_rate, 2))
    total = float(data.get("total") or round(before_vat + vat, 2))
    image_url = ""
    if image_path and image_path.name and public_base_url:
        image_url = f"{public_base_url.rstrip()}/files/{image_path.name}"
    date_value = data.get("date") or dt.date.today()
    if isinstance(date_value, (dt.datetime, dt.date)):
        date_text = date_value.isoformat()[:10]
        month_text = f"{date_value.year:04d}-{date_value.month:02d}"
    else:
        date_text = str(date_value)
        month_text = str(date_value)[:7]
    return {
        "secret": CONFIG.get("google_apps_script_secret", ""),
        "date": date_text,
        "type": data.get("transaction_type", "Expense"),
        "transaction_type": data.get("transaction_type", "Expense"),
        "invoiceNo": normalize_invoice_no(data.get("invoice_no")),
        "invoice_no": normalize_invoice_no(data.get("invoice_no")),
        "vendor": data.get("vendor", ""),
        "description": data.get("description", ""),
        "category": data.get("category", ""),
        "beforeVat": before_vat,
        "before_vat": before_vat,
        "vatRate": vat_rate,
        "vat_rate": vat_rate,
        "vat": vat,
        "withholdingTax": float(data.get("withholding_tax") or data.get("withholdingTax") or 0),
        "withholding_tax": float(data.get("withholding_tax") or data.get("withholdingTax") or 0),
        "total": total,
        "claimable": data.get("claimable", "Yes"),
        "month": month_text,
        "imageUrl": image_url,
        "image_url": image_url,
        "confidence": data.get("confidence", ""),
        "rawText": data.get("raw_text", ""),
        "raw_text": data.get("raw_text", ""),
        "documentType": data.get("document_type", ""),
        "document_type": data.get("document_type", ""),
        "submitterName": data.get("submitter_name", ""),
        "submitter_name": data.get("submitter_name", ""),
        "lineUserId": line_user_id,
        "line_user_id": line_user_id,
        "message": message,
    }


def send_to_google_sheet(data: dict[str, Any], image_path: Path, line_user_id: str, public_base_url: str) -> dict[str, Any] | None:
    url = CONFIG.get("google_apps_script_url")
    if not url:
        return None
    payload = build_google_sheet_payload(data, image_path, line_user_id, public_base_url, "Confirmed LINE OCR import")
    result = post_json(url, payload)
    runtime_log(f"Google Apps Script response: {result}")
    if result.get("status") != "ok":
        raise RuntimeError(f"Google Apps Script save failed: {result}")
    return result


def google_sheet_action(action: str, **kwargs: Any) -> dict[str, Any]:
    url = CONFIG.get("google_apps_script_url")
    if not url:
        raise RuntimeError("missing google_apps_script_url")
    payload = {"secret": CONFIG.get("google_apps_script_secret", ""), "action": action}
    payload.update(kwargs)
    result = post_json(url, payload)
    runtime_log(f"Google Apps Script action {action} response: {result}")
    if result.get("status") != "ok":
        raise RuntimeError(f"Google Apps Script action {action} failed: {result}")
    return result


def search_google_sheet_by_total(total: Any) -> list[dict[str, Any]]:
    result = google_sheet_action("searchByTotal", total=to_float(total))
    return list(result.get("matches") or [])


def search_stock_product(branch: str, query: str) -> list[dict[str, Any]]:
    source = str(CONFIG.get("stock_source") or "auto").lower()
    if source in {"qashier", "auto"}:
        try:
            return search_qashier_stock(branch, query)
        except Exception as exc:
            runtime_log(f"Qashier stock search skipped/failed: {exc}")
            if source == "qashier":
                raise

    result = google_sheet_action("searchStock", branch=branch, query=query)
    matches = list(result.get("matches") or [])
    for item in matches:
        item.setdefault("source", "Google Sheet")
    return matches


def qashier_config() -> dict[str, Any]:
    return dict(CONFIG.get("qashier") or {})


def qashier_env(config_key: str, default: str = "") -> str:
    env_name = qashier_config().get(config_key)
    if not env_name:
        return default
    return os.getenv(str(env_name), default)


def search_qashier_stock(branch: str, query: str) -> list[dict[str, Any]]:
    cfg = qashier_config()
    url = qashier_env("inventory_url_env")
    token = qashier_env("api_token_env")
    store_id = qashier_env("store_id_env")
    if not url or not token:
        raise RuntimeError("missing Qashier inventory API URL or token")

    params = {
        "q": query,
        "query": query,
        "keyword": query,
        "barcode": query,
        "branch": branch,
        "storeId": store_id,
        "store_id": store_id,
    }
    clean_params = {k: v for k, v in params.items() if v}
    separator = "&" if "?" in url else "?"
    endpoint = f"{url}{separator}{urllib.parse.urlencode(clean_params)}"
    request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "LineVatBot/2.0",
        },
        method="GET",
    )
    timeout = int(cfg.get("timeout_seconds") or 20)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return normalize_qashier_stock_items(payload, branch)


def normalize_qashier_stock_items(payload: Any, branch: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_items = (
            payload.get("items")
            or payload.get("products")
            or payload.get("inventory")
            or payload.get("results")
            or payload.get("data")
            or []
        )
    else:
        raw_items = payload
    if isinstance(raw_items, dict):
        raw_items = (
            raw_items.get("items")
            or raw_items.get("products")
            or raw_items.get("inventory")
            or raw_items.get("results")
            or []
        )
    if not isinstance(raw_items, list):
        return []

    def pick(item: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = item.get(key)
            if value not in (None, ""):
                return value
        return ""

    normalized: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        stock_value = pick(
            item,
            "stock",
            "quantity",
            "qty",
            "availableQty",
            "available_quantity",
            "inventoryQuantity",
        )
        normalized.append(
            {
                "name": pick(item, "name", "productName", "product_name", "title"),
                "price": pick(item, "price", "sellingPrice", "selling_price", "retailPrice"),
                "barcode": pick(item, "barcode", "sku", "code", "productCode"),
                "stock": stock_value,
                "branch": pick(item, "branch", "storeName", "store_name") or branch,
                "source": "Qashier HQ",
            }
        )
    return normalized


def format_stock_results(branch: str, query: str, matches: list[dict[str, Any]]) -> str:
    if not matches:
        return (
            f"ไม่พบสินค้าในสาขา {branch}\n"
            f"คำค้น: {query}\n\n"
            "กรุณาตรวจสอบชื่อสินค้า/บาร์โค้ด/SKU แล้วค้นหาอีกครั้งค่ะ"
        )
    lines = [
        f"พบสินค้าในสาขา {branch}",
        f"คำค้น: {query}",
        "",
    ]
    for idx, item in enumerate(matches[:10], 1):
        lines.extend(
            [
                f"{idx}. ชื่อสินค้า: {item.get('name') or '-'}",
                f"ราคาสินค้า: {item.get('price') or '-'}",
                f"บาร์โค้ด: {item.get('barcode') or '-'}",
                "",
            ]
        )
    if len(matches) > 10:
        lines.append(f"แสดง 10 รายการแรกจากทั้งหมด {len(matches)} รายการ")
    return "\n".join(lines).strip()


def parse_stock_queries(text: str, limit: int = 10) -> tuple[list[str], int]:
    raw_parts = re.split(r"[\n,;]+", text)
    queries: list[str] = []
    for part in raw_parts:
        query = part.strip()
        if query and query not in queries:
            queries.append(query)
    return queries[:limit], len(queries)


def format_stock_check_results(branch: str, queries: list[str], results: dict[str, list[dict[str, Any]]], total_entered: int) -> str:
    lines = [
        f"ผลตรวจสอบสต็อค สาขา {branch}",
        f"ตรวจสอบ {len(queries)} รายการ" + (" จาก 10 รายการแรก" if total_entered > 10 else ""),
        "",
    ]
    for idx, query in enumerate(queries, 1):
        matches = results.get(query) or []
        lines.append(f"{idx}. คำค้น/บาร์โค้ด: {query}")
        if not matches:
            lines.extend(["ไม่พบสินค้า", ""])
            continue
        for item in matches[:3]:
            qty = item.get("quantity")
            lines.extend(
                [
                    f"ชื่อสินค้า: {item.get('name') or '-'}",
                    f"จำนวนคงเหลือ: {qty if qty not in [None, ''] else '-'}",
                    f"ราคา: {item.get('price') or '-'}",
                    f"บาร์โค้ด: {item.get('barcode') or '-'}",
                    "",
                ]
            )
        if len(matches) > 3:
            lines.extend([f"พบทั้งหมด {len(matches)} รายการ แสดง 3 รายการแรก", ""])
    lines.append("ข้อมูลอ้างอิงจาก Product List/Qashier HQ ที่ซิงก์ไว้ใน Google Sheet")
    return "\n".join(lines).strip()


def delete_google_sheet_row(row: int, sheet_name: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"row": int(row)}
    if sheet_name:
        payload["sheetName"] = sheet_name
    return google_sheet_action("deleteRow", **payload)


def update_google_sheet_document_type(row: int, document_type: str, sheet_name: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {"row": int(row), "documentType": document_type}
    if sheet_name:
        payload["sheetName"] = sheet_name
    return google_sheet_action("updateDocumentType", **payload)


def save_hr_request_to_google(data: dict[str, Any], line_user_id: str) -> dict[str, Any]:
    payload = dict(data)
    payload["lineUserId"] = line_user_id
    return google_sheet_action("saveHrRequest", data=payload)


def get_hr_schedule_link(month_offset: int = 0) -> dict[str, Any]:
    return google_sheet_action("getHrSchedule", monthOffset=month_offset)


def save_medical_certificate_to_google(request_id: str, image_path: Path, line_user_id: str) -> dict[str, Any]:
    raw = image_path.read_bytes()
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    return google_sheet_action(
        "saveMedicalCertificate",
        requestId=request_id,
        lineUserId=line_user_id,
        fileName=image_path.name,
        mimeType=mime_type,
        data=base64.b64encode(raw).decode("ascii"),
    )


def save_substitute_receipt_to_google(data: dict[str, Any], image_url: str, pdf_url: str, line_user_id: str) -> dict[str, Any]:
    payload = {
        "sheetName": data.get("sheet_name") or "",
        "transactionRow": data.get("row") or "",
        "date": data.get("date") or "",
        "documentType": data.get("document_type") or "",
        "invoiceNo": data.get("invoice_no") or "",
        "vendor": data.get("vendor") or "",
        "submitterName": data.get("submitter_name") or "",
        "category": data.get("category") or "",
        "beforeVat": data.get("before_vat") or 0,
        "vat": data.get("vat") or 0,
        "withholdingTax": data.get("withholding_tax") or data.get("withholdingTax") or 0,
        "total": data.get("total") or 0,
        "imageUrl": image_url,
        "pdfUrl": pdf_url,
        "lineUserId": line_user_id,
    }
    return google_sheet_action("saveSubstituteReceipt", data=payload)


def update_hr_approval_to_google(request_id: str, approved: bool, approver_line_id: str) -> dict[str, Any]:
    return google_sheet_action(
        "updateHrApproval",
        requestId=request_id,
        approved=approved,
        note=f"Updated from LINE by {approver_line_id}",
    )


def format_google_sheet_matches(matches: list[dict[str, Any]], heading: str) -> str:
    lines = [heading]
    for item in matches[:10]:
        lines.append(
            f"{item.get('sheetName') or 'Sheet'} Row {item.get('row')}: {item.get('date') or '-'} | "
            f"{item.get('type') or '-'} | {item.get('vendor') or '-'} | "
            f"เลขที่บิล {item.get('invoiceNo') or '-'} | "
            f"ก่อน VAT {float(item.get('beforeVat') or 0):,.2f} | "
            f"ยอดรวม {float(item.get('total') or 0):,.2f} | "
            f"เอกสาร {item.get('documentType') or '-'}"
        )
    return "\n".join(lines)


def process_line_event(event: dict[str, Any]) -> str | None:
    runtime_log(f"Received LINE event type={event.get('type')} message_type={event.get('message', {}).get('type')}")
    if event.get("type") != "message":
        return None
    message = event.get("message", {})
    line_user_id = event.get("source", {}).get("userId", "")

    if message.get("type") == "text":
        text = str(message.get("text") or "").strip()
        if text.startswith("แก้ไขบิล+"):
            bill_no = text.split("+", 1)[1].strip()
            return delete_bill_from_excel(bill_no, line_user_id)
        delete_row_match = re.match(r"^delete\s+row\s+(\d+)$", text, flags=re.IGNORECASE)
        if delete_row_match:
            return delete_excel_row(delete_row_match.group(1), line_user_id)
        return (
            "Please send a receipt image.\n"
            "หากต้องการลบ/แก้ไขบิล ให้พิมพ์: แก้ไขบิล+เลขที่บิล\n"
            "หากต้องการยกเลิกตามแถว ให้พิมพ์: Delete Row 173"
        )

    if message.get("type") not in {"image", "file"}:
        return "Please send a receipt image."

    token = os.getenv(CONFIG["line"]["channel_access_token_env"])
    if not token:
        raise RuntimeError(f"Missing {CONFIG['line']['channel_access_token_env']}")

    try:
        runtime_log("Downloading LINE image content")
        image_path = download_line_content(message["id"], token, resolve_path(CONFIG["image_archive_dir"]))
        runtime_log(f"Downloaded LINE image to {image_path}")
        runtime_log("OCR started")
        text = ocr_image(image_path)
        runtime_log(f"OCR completed characters={len(text)}")
    except Exception as exc:
        runtime_log(f"OCR failed: {exc}")
        draft = blank_manual_entry("Expense")
        draft["submitter_name"] = get_line_display_name(line_user_id)
        set_user_state(
            line_user_id,
            {
                "mode": "awaiting_correction",
                "transaction_type": draft["transaction_type"],
                "image_path": str(image_path) if "image_path" in locals() else "",
                "pending_data": serialize_data(draft),
            },
        )
        return manual_entry_form(draft)
        return (
            "OCR อ่านเอกสารไม่สำเร็จหรือใช้เวลานานเกินไปค่ะ\n"
            "กรุณาถ่ายรูปใหม่ให้เห็นเฉพาะเอกสารเต็มหน้า ตัวหนังสือชัด และไม่เอียงมาก\n"
            "จากนั้นส่งรูปเข้ามาอีกครั้งค่ะ"
        )
    parsed = parse_receipt_text(text, float(CONFIG.get("vat_rate", 0.07)))
    sheet_name, row = append_expense_to_excel(image_path, parsed, "Imported", "Imported from LINE OCR", line_user_id)
    runtime_log(
        f"Imported LINE receipt -> {sheet_name}!row {row} "
        f"date={parsed['date']} total={parsed['total']} vendor={parsed['vendor']!r}"
    )

    return (
        "==== บิลนำเข้า ====\n"
        "สถานะ: บันทึกเอกสารเรียบร้อย\n"
        f"Sheet: {sheet_name}\n"
        f"Row: {row}\n"
        f"Date: {parsed['date']}\n"
        f"Vendor: {parsed['vendor'] or '-'}\n"
        f"Category: {parsed['category']}\n"
        f"Before VAT: {parsed['before_vat']:,.2f}\n"
        f"VAT: {parsed['vat']:,.2f}\n"
        f"Total: {parsed['total']:,.2f}\n"
        f"Confidence: {parsed['confidence']:.0%}\n"
        "Please review before filing VAT."
    )


def process_line_event_menu(event: dict[str, Any], public_base_url: str) -> str | dict[str, Any] | list[dict[str, Any]] | None:
    runtime_log(f"Received LINE event type={event.get('type')} message_type={event.get('message', {}).get('type')}")
    line_user_id = event.get("source", {}).get("userId", "")
    state = get_user_state(line_user_id)
    if event.get("type") in {"follow", "join", "memberJoined"}:
        if not state.get("mode"):
            return menu_message()
        return None
    if event.get("type") != "message":
        return None
    message = event.get("message", {})

    if message.get("type") == "text":
        text = str(message.get("text") or "").strip()
        if state.get("mode") == "awaiting_hr_medical_certificate" and text not in {"เมนู", "menu", "Menu", "MENU", "ยกเลิก"}:
            return "กรุณาส่งรูปใบรับรองแพทย์สำหรับวันที่ลาป่วยค่ะ หรือพิมพ์ ยกเลิก เพื่อเริ่มใหม่"
        if state.get("mode") == "awaiting_hr_medical_certificate" and text == "ยกเลิก":
            clear_user_state(line_user_id)
            return hr_menu_message()
        if text in {"เมนู", "menu", "Menu", "MENU", "บัญชี"}:
            clear_user_state(line_user_id)

            return menu_message()
        approval_match = re.match(r"^HR_(APPROVE|REJECT):(.+)$", text, flags=re.IGNORECASE)
        if approval_match:
            approved = approval_match.group(1).upper() == "APPROVE"
            request_id = approval_match.group(2).strip()
            try:
                result = update_hr_approval_to_google(request_id, approved, line_user_id)
            except Exception as exc:
                runtime_log(f"Update HR approval failed: {exc}")
                return abort_flow_message(f"\u0e2d\u0e31\u0e1b\u0e40\u0e14\u0e15\u0e1c\u0e25\u0e2d\u0e19\u0e38\u0e21\u0e31\u0e15\u0e34\u0e44\u0e21\u0e48\u0e2a\u0e33\u0e40\u0e23\u0e47\u0e08\u0e04\u0e48\u0e30 ({exc})")
            status = result.get("approvalStatus") or ("\u0e2d\u0e19\u0e38\u0e21\u0e31\u0e15\u0e34" if approved else "\u0e44\u0e21\u0e48\u0e2d\u0e19\u0e38\u0e21\u0e31\u0e15\u0e34")
            requester_id = str(result.get("lineUserId") or "")
            request_type = result.get("requestType") or "\u0e04\u0e33\u0e02\u0e2d HR"
            employee_name = result.get("employeeName") or "-"
            if requester_id:
                push_line_messages(
                    requester_id,
                    [
                        text_message(
                            f"\u0e1c\u0e25\u0e01\u0e32\u0e23\u0e2d\u0e19\u0e38\u0e21\u0e31\u0e15\u0e34 {request_type}\n"
                            f"Request ID: {request_id}\n"
                            f"\u0e0a\u0e37\u0e48\u0e2d\u0e1e\u0e19\u0e31\u0e01\u0e07\u0e32\u0e19: {employee_name}\n"
                            f"\u0e2a\u0e16\u0e32\u0e19\u0e30: {status}"
                        )
                    ],
                )
            return (
                f"\u0e1a\u0e31\u0e19\u0e17\u0e36\u0e01\u0e1c\u0e25\u0e2d\u0e19\u0e38\u0e21\u0e31\u0e15\u0e34\u0e40\u0e23\u0e35\u0e22\u0e1a\u0e23\u0e49\u0e2d\u0e22\u0e04\u0e48\u0e30\n"
                f"Request ID: {request_id}\n"
                f"\u0e0a\u0e37\u0e48\u0e2d\u0e1e\u0e19\u0e31\u0e01\u0e07\u0e32\u0e19: {employee_name}\n"
                f"\u0e2a\u0e16\u0e32\u0e19\u0e30: {status}"
            )
        if text in {"สต็อค", "สต๊อค", "stock", "Stock", "STOCK"}:
            clear_user_state(line_user_id)
            return stock_menu_message()
        if text in {"นำเข้าสต็อค", "นำออกสต็อค", "เช็คสต็อค"}:
            clear_user_state(line_user_id)
            return buttons_template_message(
                f"เมนู {text}\n\n"
                "ระบบส่วนนี้กำลังเตรียมใช้งานค่ะ",
                [
                    ("กลับเมนูสต็อค", "สต็อค"),
                    ("กลับเมนูบัญชี", "บัญชี"),
                ],
            )
        if text == "ค้นหาสินค้า":
            clear_user_state(line_user_id)
            return stock_branch_menu_message()
        stock_branch_match = re.match("^(?:\u0e04\u0e49\u0e19\u0e2b\u0e32\u0e2a\u0e15\u0e47\u0e2d\u0e04|\u0e04\u0e49\u0e19\u0e2b\u0e32\u0e2a\u0e34\u0e19\u0e04\u0e49\u0e32):(.+)$", text)
        if stock_branch_match:
            branch = stock_branch_match.group(1).strip()
            set_user_state(line_user_id, {"mode": "awaiting_stock_product_query", "stock_branch": branch})
            return (
                f"เลือกสาขา {branch} แล้วค่ะ\n"
                "กรุณาพิมพ์ชื่อสินค้า/บาร์โค้ด หรือสแกนบาร์โค้ดได้เลย\n"
                "ส่งได้สูงสุด 10 รายการต่อครั้ง โดยขึ้นบรรทัดใหม่หรือคั่นด้วย comma\n"
                "ตัวอย่าง: 8851234567890, ตุ๊กตา, ABC001"
            )
        if state.get("mode") == "awaiting_stock_product_query":
            branch = str(state.get("stock_branch") or "-")
            queries, total_entered = parse_stock_queries(text, 10)
            if not queries:
                return "กรุณาพิมพ์ชื่อสินค้าหรือบาร์โค้ดที่ต้องการตรวจสอบสต็อคค่ะ"
            results: dict[str, list[dict[str, Any]]] = {}
            try:
                for query in queries:
                    results[query] = search_stock_product(branch, query)
            except Exception as exc:
                runtime_log(f"Stock search failed: {exc}")
                clear_user_state(line_user_id)
                return abort_flow_message(f"ค้นหาสต็อคไม่สำเร็จค่ะ ({exc})")
            set_user_state(line_user_id, {"mode": "awaiting_stock_product_query", "stock_branch": branch})
            return [
                text_message(format_stock_check_results(branch, queries, results, total_entered)),
                buttons_template_message(
                    "ต้องการตรวจสอบต่อไหมคะ",
                    [
                        ("ตรวจสาขาเดิม", f"ค้นหาสต็อค:{branch}"),
                        ("เลือกสาขาใหม่", "สต็อค"),
                    ],
                ),
            ]
        if text in {"HR", "hr", "Hr", "ฝ่ายบุคคล"}:
            clear_user_state(line_user_id)
            return hr_menu_message()
        if text in {"สินค้า", "product", "Product", "PRODUCT"}:
            clear_user_state(line_user_id)
            return stock_branch_menu_message()
        if text == "ตารางงาน":
            clear_user_state(line_user_id)
            return schedule_month_menu_message()
        schedule_match = re.match(r"^ตารางงาน:([+-]?\d+)$", text)
        if schedule_match:
            clear_user_state(line_user_id)
            month_offset = max(-1, min(1, int(schedule_match.group(1))))
            try:
                result = get_hr_schedule_link(month_offset)
            except Exception as exc:
                runtime_log(f"Get HR schedule failed: {exc}")
                return abort_flow_message(f"เปิดตารางงานไม่สำเร็จค่ะ ({exc})")
            url = result.get("url") or result.get("spreadsheetUrl") or ""
            pdf_url = result.get("pdfUrl") or result.get("pdf_url") or ""
            pdf_download_url = result.get("pdfDownloadUrl") or result.get("pdf_download_url") or pdf_url
            sheet_name = result.get("sheetName") or "HR_Work_Schedule"
            if pdf_download_url:
                try:
                    pdf_bytes = download_binary(str(pdf_download_url))
                    image_path = render_pdf_first_page_to_jpeg(pdf_bytes, f"hr_schedule_{month_offset}")
                    return [
                        text_message(f"ตารางงาน\n{sheet_name}"),
                        image_message(public_file_url(public_base_url, image_path)),
                    ]
                except Exception as exc:
                    runtime_log(f"Schedule PDF to JPEG failed: {exc}")
                return (
                    "ตารางงาน\n"
                    f"{sheet_name}\n"
                    f"{pdf_url or url}\n\n"
                    "ระบบแปลงเป็นรูปไม่สำเร็จชั่วคราว จึงส่งลิงก์สำรองให้ค่ะ\n"
                    f"ลิงก์ Google Sheet: {url}"
                )
            return f"ตารางงาน\n{sheet_name}\n{url}"
        if text in {"ลาป่วย", "ลากิจ", "แจ้งขอวันหยุดล่วงหน้า", "แจ้งเปลี่ยนเวลาเข้า-ออกงาน", "แจ้งเปลี่ยนวันทำงาน"}:
            draft = hr_request_blank(text, line_user_id)
            set_user_state(
                line_user_id,
                {
                    "mode": "awaiting_hr_form",
                    "hr_request": draft,
                },
            )
            return hr_request_form(draft)
        if state.get("mode") == "awaiting_substitute_receipt_decision":
            if text == "1":
                match = state.get("substitute_match")
                if not isinstance(match, dict) or not can_create_substitute_receipt(match):
                    clear_user_state(line_user_id)
                    return "ไม่พบข้อมูลสำหรับสร้างใบแทนค่ะ กรุณาเริ่มรายการใหม่อีกครั้ง"
                clear_user_state(line_user_id)
                return substitute_receipt_messages([match], public_base_url, line_user_id)
            if text == "2":
                clear_user_state(line_user_id)
                return [
                    text_message("รับทราบค่ะ ไม่สร้างใบแทนสำหรับรายการนี้\nสามารถเริ่มทำรายการใหม่ได้เลยค่ะ"),
                    menu_message(),
                ]
            return quick_reply_text_message(
                "กรุณาเลือกว่าต้องการสร้างใบแทนหรือไม่คะ\n\n"
                "1 = ต้องการสร้างใบแทน\n"
                "2 = ไม่ต้องการ",
                [
                    ("🧾 1 สร้างใบแทน", "1"),
                    ("ไม่สร้างใบแทน", "2"),
                ],
            )
        if state.get("mode") == "awaiting_substitute_select":
            row_match = re.match(r"^(?:row\s*)?(\d+)$", text, flags=re.IGNORECASE)
            if not row_match:
                return "กรุณาพิมพ์เลข Row ที่ต้องการสร้างใบแทน เช่น Row 12"
            wanted_row = int(row_match.group(1))
            matches = [item for item in state.get("substitute_matches", []) if int(item.get("row", 0)) == wanted_row]
            if not matches:
                return "ไม่พบ Row นี้จากรายการที่ค้นหา กรุณาพิมพ์ Row ใหม่ค่ะ"
            clear_user_state(line_user_id)
            return substitute_receipt_messages(matches, public_base_url, line_user_id)
        substitute_match = re.match(r"^ใบแทน\s+(.+)$", text, flags=re.IGNORECASE)
        if substitute_match:
            amount = normalize_amount(substitute_match.group(1))
            if amount is None:
                return "กรุณาพิมพ์คำสั่ง เช่น ใบแทน 59 หรือ ใบแทน 12705.18"
            try:
                matches = search_google_sheet_by_total(amount)
            except Exception as exc:
                runtime_log(f"Substitute receipt search failed: {exc}")
                clear_user_state(line_user_id)
                return abort_flow_message(f"ค้นหารายการเพื่อสร้างใบแทนไม่สำเร็จค่ะ ({exc})")
            matches = [item for item in matches if can_create_substitute_receipt(item)]
            if not matches:
                return (
                    f"ไม่พบรายการค่าใช้จ่ายยอด {amount:,.2f} ที่สามารถสร้างใบแทนได้ค่ะ\n"
                    "ตรวจสอบว่ายอดตรงกับ Google Sheet หรือพิมพ์ยอดรวมสุทธิของรายการนั้นอีกครั้ง"
                )
            if len(matches) == 1:
                return substitute_receipt_messages(matches, public_base_url, line_user_id)
            set_user_state(line_user_id, {"mode": "awaiting_substitute_select", "substitute_matches": matches[:10]})
            return format_google_sheet_matches(matches, "พบหลายรายการที่สามารถสร้างใบแทนได้") + "\n\nกรุณาพิมพ์เลข Row ที่ต้องการสร้างใบแทน"
        if state.get("mode") == "awaiting_confirmation" and text in {"1", "ตรวจสอบและยืนยัน"}:
            if not state.get("pending_data"):
                return "ยังไม่มีบิลที่รอยืนยันค่ะ กรุณาเลือกเมนู บิลรายรับ หรือ บิลรายจ่าย ก่อน"
            return confirm_pending_to_google(line_user_id, state, public_base_url)
        if state.get("mode") == "awaiting_submitter_name":
            pending = deserialize_data(state.get("pending_data", {}))
            pending["submitter_name"] = text
            state["mode"] = "awaiting_confirmation"
            state["pending_data"] = serialize_data(pending)
            set_user_state(line_user_id, state)
            return confirmation_prompt(pending)
        if state.get("mode") == "awaiting_cancel_total":
            amount = normalize_amount(text)
            if amount is None:
                return "กรุณาพิมพ์ยอดรวมสุทธิหรือยอดก่อน VAT ที่ต้องการยกเลิก เช่น 2251.72"
            try:
                matches = search_google_sheet_by_total(amount)
            except Exception as exc:
                runtime_log(f"Cancel search failed: {exc}")
                clear_user_state(line_user_id)
                return abort_flow_message(f"ค้นหารายการไม่สำเร็จค่ะ ({exc})")
            if not matches:
                clear_user_state(line_user_id)
                return f"ไม่พบรายการที่มียอด {amount:,.2f} ค่ะ กรุณาตรวจสอบว่ายอดนี้ตรงกับยอดรวมสุทธิหรือยอดก่อน VAT ใน Google Sheet"
            if len(matches) == 1:
                state["mode"] = "awaiting_cancel_confirm"
                state["cancel_row"] = int(matches[0]["row"])
                state["cancel_sheet"] = str(matches[0].get("sheetName") or "")
                set_user_state(line_user_id, state)
                return format_google_sheet_matches(matches, "พบรายการที่ต้องการยกเลิก") + "\n\nตอบ 1 = ยืนยันยกเลิก\nตอบ 2 = ไม่ยกเลิก"
            state["mode"] = "awaiting_cancel_select"
            state["cancel_matches"] = matches[:10]
            set_user_state(line_user_id, state)
            return format_google_sheet_matches(matches, "พบหลายรายการที่มียอดรวมนี้") + "\n\nกรุณาพิมพ์เลข Row ที่ต้องการยกเลิก"
        if state.get("mode") == "awaiting_cancel_select":
            row_match = re.match(r"^(?:row\s*)?(\d+)$", text, flags=re.IGNORECASE)
            if not row_match:
                return "กรุณาพิมพ์เลข Row ที่ต้องการยกเลิก เช่น Row 12"
            wanted_row = int(row_match.group(1))
            matches = [item for item in state.get("cancel_matches", []) if int(item.get("row", 0)) == wanted_row]
            if not matches:
                return "ไม่พบ Row นี้จากรายการที่ค้นหา กรุณาพิมพ์ Row ใหม่ค่ะ"
            state["mode"] = "awaiting_cancel_confirm"
            state["cancel_row"] = wanted_row
            state["cancel_sheet"] = str(matches[0].get("sheetName") or "")
            set_user_state(line_user_id, state)
            return format_google_sheet_matches(matches, "ยืนยันรายการที่ต้องการยกเลิก") + "\n\nตอบ 1 = ยืนยันยกเลิก\nตอบ 2 = ไม่ยกเลิก"
        if state.get("mode") == "awaiting_cancel_confirm":
            if text == "1":
                row = int(state.get("cancel_row", 0))
                try:
                    delete_google_sheet_row(row, str(state.get("cancel_sheet") or ""))
                except Exception as exc:
                    runtime_log(f"Cancel delete failed: {exc}")
                    clear_user_state(line_user_id)
                    return abort_flow_message(f"ยกเลิกรายการไม่สำเร็จค่ะ ({exc})")
                clear_user_state(line_user_id)
                return "ยกเลิกบิลเรียบร้อย"
            if text == "2":
                clear_user_state(line_user_id)
                return "ยกเลิกคำสั่งแล้วค่ะ"
            return "กรุณาตอบ 1 เพื่อยืนยันยกเลิก หรือ 2 เพื่อไม่ยกเลิก"
        if state.get("mode") == "awaiting_duplicate_confirmation":
            if text == "1":
                state["mode"] = "awaiting_duplicate_edit_choice"
                set_user_state(line_user_id, state)
                return "ต้องการแก้ไขประเภทเอกสารของรายการเดิมหรือไม่?\nตอบ 1 = แก้ไขประเภทเอกสาร\nตอบ 2 = ไม่แก้ไขและไม่บันทึกซ้ำ"
            if text == "2":
                state["mode"] = "awaiting_confirmation"
                state["duplicate_checked"] = True
                set_user_state(line_user_id, state)
                return confirm_pending_to_google(line_user_id, state, public_base_url)
            return "กรุณาตอบ 1 = เป็นรายการเดียวกัน หรือ 2 = บันทึกเป็นรายการใหม่"
        if state.get("mode") == "awaiting_duplicate_edit_choice":
            if text == "1":
                state["mode"] = "awaiting_duplicate_doc_type"
                set_user_state(line_user_id, state)
                return "กรุณาพิมพ์ประเภทเอกสารใหม่ที่ต้องการแก้ไข เช่น ใบกำกับภาษี / ใบเสร็จ / บิล"
            if text == "2":
                clear_user_state(line_user_id)
                return "ยกเลิกการบันทึกซ้ำแล้วค่ะ"
            return "กรุณาตอบ 1 เพื่อแก้ไขประเภทเอกสาร หรือ 2 เพื่อไม่แก้ไข"
        if state.get("mode") == "awaiting_duplicate_doc_type":
            state["new_document_type"] = text
            state["mode"] = "awaiting_duplicate_update_confirm"
            set_user_state(line_user_id, state)
            matches = state.get("duplicate_matches", [])
            return format_google_sheet_matches(matches, "รายการเดิมที่จะถูกแก้ไข") + f"\n\nประเภทเอกสารใหม่: {text}\nตอบ 1 = ยืนยันแก้ไข\nตอบ 2 = แก้ไขประเภทเอกสารอีกครั้ง"
        if state.get("mode") == "awaiting_duplicate_update_confirm":
            if text == "1":
                matches = state.get("duplicate_matches", [])
                if not matches:
                    clear_user_state(line_user_id)
                    return "ไม่พบรายการเดิมสำหรับแก้ไขค่ะ"
                target = matches[0]
                row = int(target["row"])
                sheet_name = str(target.get("sheetName") or "")
                document_type = str(state.get("new_document_type") or "")
                try:
                    update_google_sheet_document_type(row, document_type, sheet_name)
                except Exception as exc:
                    runtime_log(f"Duplicate doc type update failed: {exc}")
                    clear_user_state(line_user_id)
                    return abort_flow_message(f"แก้ไขประเภทเอกสารไม่สำเร็จค่ะ ({exc})")
                clear_user_state(line_user_id)
                return "แก้ไขประเภทเอกสารเรียบร้อย"
            if text == "2":
                state["mode"] = "awaiting_duplicate_doc_type"
                set_user_state(line_user_id, state)
                return "กรุณาพิมพ์ประเภทเอกสารใหม่อีกครั้งค่ะ"
            return "กรุณาตอบ 1 เพื่อยืนยันแก้ไข หรือ 2 เพื่อแก้ไขประเภทเอกสารอีกครั้ง"
        if state.get("mode") == "awaiting_confirmation" and text in {"2", "แก้ไข"}:
            if not state.get("pending_data"):
                return "ยังไม่มีบิลที่รอแก้ไขค่ะ"
            state["mode"] = "awaiting_correction"
            set_user_state(line_user_id, state)
            return manual_entry_form(deserialize_data(state.get("pending_data", {})))
        if state.get("mode") == "awaiting_hr_form":
            draft = dict(state.get("hr_request") or {})
            updated = parse_hr_request_text(text, draft)
            state["mode"] = "awaiting_hr_confirm"
            state["hr_request"] = updated
            set_user_state(line_user_id, state)
            return hr_confirm_message(updated)
        if state.get("mode") == "awaiting_hr_confirm":
            if text == "1":
                request_data = dict(state.get("hr_request") or {})
                if not str(request_data.get("employee_name") or "").strip():
                    state["mode"] = "awaiting_hr_form"
                    set_user_state(line_user_id, state)
                    return "กรุณาระบุชื่อพนักงานก่อนส่งคำขอค่ะ\n\n" + hr_request_form(request_data)
                try:
                    result = save_hr_request_to_google(request_data, line_user_id)
                except Exception as exc:
                    runtime_log(f"Save HR request failed: {exc}")
                    clear_user_state(line_user_id)
                    return abort_flow_message(f"บันทึกคำขอ HR ไม่สำเร็จค่ะ ({exc})")
                request_id = str(result.get("requestId") or result.get("row") or uuid.uuid4().hex[:8])
                request_data["request_id"] = request_id
                approver_id = str(CONFIG.get("hr_approver_line_id") or os.getenv("HR_APPROVER_LINE_ID") or "Ud260925c43fb0823fea42224a2929393")
                push_line_messages(
                    approver_id,
                    [
                        text_message(
                            "มีคำขอ HR รออนุมัติ\n\n"
                            + format_hr_request(request_data)
                            + f"\n\nRequest ID: {request_id}"
                        ),
                        approval_buttons_message(request_id),
                    ],
                )
                if request_data.get("request_type") == "ลาป่วย":
                    set_user_state(
                        line_user_id,
                        {
                            "mode": "awaiting_hr_medical_certificate",
                            "hr_request": request_data,
                            "hr_request_id": request_id,
                        },
                    )
                    return (
                        "ส่งคำขอลาป่วยเรียบร้อยค่ะ\n"
                        f"Request ID: {request_id}\n\n"
                        "กรุณาส่งรูปใบรับรองแพทย์สำหรับวันที่ลาป่วยเข้ามาได้เลยค่ะ"
                    )
                clear_user_state(line_user_id)
                return f"ส่งคำขอ HR เรียบร้อยค่ะ\nRequest ID: {request_id}\nสถานะ: รออนุมัติ"
            if text == "2":
                state["mode"] = "awaiting_hr_form"
                set_user_state(line_user_id, state)
                return hr_request_form(dict(state.get("hr_request") or {}))
            return "กรุณาตอบ 1 เพื่อยืนยันส่งคำขอ หรือ 2 เพื่อแก้ไขข้อมูลค่ะ"
        if text in {"1", "บิลรายรับ"}:
            set_user_state(line_user_id, {"mode": "awaiting_image", "transaction_type": "Revenue"})
            return "ส่งเอกสารเพื่อลงรายละเอียดในระบบได้เลยค่ะ"
        if text in {"2", "บิลรายจ่าย"}:
            set_user_state(line_user_id, {"mode": "awaiting_image", "transaction_type": "Expense"})
            return "ส่งเอกสารเพื่อลงรายละเอียดในระบบได้เลยค่ะ"
        if text in {"3", "เรียกดูรายละเอียดบัญชี"}:
            set_user_state(line_user_id, {"mode": "awaiting_lookup_bill_no"})
            return "กรุณาพิมพ์เลขที่บิล ชื่อร้าน/คู่ค้า หรือยอดรวมที่ต้องการตรวจสอบค่ะ"
        if text in {"4", "ยกเลิกการทำรายการ"}:
            set_user_state(line_user_id, {"mode": "awaiting_cancel_total"})
            return "กรุณาพิมพ์ยอดรวมสุทธิหรือยอดก่อน VAT ของรายการที่ต้องการยกเลิกค่ะ เช่น 2251.72"
        if state.get("mode") == "awaiting_lookup_bill_no":
            row_match = re.match(r"^(?:row\s*)?(\d+)$", text, flags=re.IGNORECASE)
            if row_match and state.get("lookup_rows"):
                wanted_row = int(row_match.group(1))
                matched_rows = [item for item in state.get("lookup_rows", []) if int(item.get("row", 0)) == wanted_row]
                if not matched_rows:
                    return "ไม่พบ Row ที่เลือกจากผลการค้นหาก่อนหน้า กรุณาพิมพ์ Row ใหม่ค่ะ"
                found = find_transaction_by_row(wanted_row)
                clear_user_state(line_user_id)
                if not found:
                    return f"ไม่พบ Row {wanted_row} ใน Excel ค่ะ"
                sheet_name, row, data = found
                image_path = render_row_summary_image(sheet_name, row, data, "รายละเอียดบัญชี")
                return [
                    text_message(f"พบรายละเอียดที่ {sheet_name}!Row {row}"),
                    image_message(public_file_url(public_base_url, image_path)),
                ]

            results = search_transactions(text, max_results=5)
            if not results:
                clear_user_state(line_user_id)
                return f"ไม่พบข้อมูลจากคำค้น: {text}\nกรุณาตรวจเลขที่บิล ชื่อร้าน หรือยอดรวมอีกครั้งค่ะ"
            if len(results) > 1:
                set_user_state(
                    line_user_id,
                    {
                        "mode": "awaiting_lookup_bill_no",
                        "lookup_rows": [{"sheet": sheet_name, "row": row} for sheet_name, row, _ in results],
                    },
                )
                return format_lookup_results(results, text)

            clear_user_state(line_user_id)
            sheet_name, row, data = results[0]
            image_path = render_row_summary_image(sheet_name, row, data, "รายละเอียดบัญชี")
            return [
                text_message(f"พบรายละเอียดจากคำค้น {text} ที่ {sheet_name}!Row {row}"),
                image_message(public_file_url(public_base_url, image_path)),
            ]
        if text == "ตรวจสอบและยืนยัน":
            if state.get("mode") != "awaiting_confirmation" or not state.get("pending_data"):
                return "ยังไม่มีบิลที่รอยืนยันค่ะ กรุณาเลือกเมนู บิลรายรับ หรือ บิลรายจ่าย ก่อน"
            pending = deserialize_data(state["pending_data"])
            image_path = Path(state.get("image_path", ""))
            if not image_path.exists():
                return "ไม่พบไฟล์รูปเอกสารเดิมค่ะ กรุณาส่งเอกสารใหม่อีกครั้ง"
            sheet_name = "Google Sheet"
            row = "-"
            result = None
            try:
                result = send_to_google_sheet(pending, image_path, line_user_id, public_base_url)
            except Exception as exc:
                runtime_log(f"Google Sheet save failed: {exc}")
                clear_user_state(line_user_id)
                return abort_flow_message(f"Google Sheet: ยังไม่สำเร็จ ({exc})")
            clear_user_state(line_user_id)
            summary_image = render_row_summary_image(sheet_name, row, pending, "บิลนำเข้า")
            messages = [
                text_message("บันทึกเรียบร้อย\nGoogle Sheet: บันทึกสำเร็จ"),
                image_message(public_file_url(public_base_url, summary_image)),
            ]
            substitute_match_data = substitute_match_from_pending(pending, result)
            if can_create_substitute_receipt(substitute_match_data):
                messages.extend(substitute_receipt_messages([substitute_match_data], public_base_url, line_user_id))
            runtime_log(
                f"Confirmed LINE receipt -> Google Sheet "
                f"type={pending.get('transaction_type')} date={pending['date']} total={pending['total']}"
            )
            return messages
        if text == "แก้ไข":
            if state.get("mode") != "awaiting_confirmation" or not state.get("pending_data"):
                return "ยังไม่มีบิลที่รอแก้ไขค่ะ"
            state["mode"] = "awaiting_correction"
            set_user_state(line_user_id, state)
            return correction_form(deserialize_data(state.get("pending_data", {})))
        if state.get("mode") == "awaiting_correction":
            pending = deserialize_data(state.get("pending_data", {}))
            corrected = parse_correction_text_v2(text, pending)
            state["mode"] = "awaiting_confirmation"
            state["pending_data"] = serialize_data(corrected)
            set_user_state(line_user_id, state)
            return confirmation_prompt(corrected)
        if text.startswith("แก้ไขบิล+") or text.startswith("เนเธเนเนเธเธเธดเธฅ+"):
            bill_no = text.split("+", 1)[1].strip()
            return delete_bill_from_excel(bill_no, line_user_id)
        delete_row_match = re.match(r"^delete\s+row\s+(\d+)$", text, flags=re.IGNORECASE)
        if delete_row_match:
            return delete_excel_row(delete_row_match.group(1), line_user_id)
        return menu_message()

    if message.get("type") not in {"image", "file"}:
        return menu_message() if not state.get("mode") else menu_text()

    if state.get("mode") == "awaiting_hr_medical_certificate":
        token = os.getenv(CONFIG["line"]["channel_access_token_env"])
        if not token:
            raise RuntimeError(f"Missing {CONFIG['line']['channel_access_token_env']}")
        try:
            runtime_log("Downloading LINE medical certificate content")
            image_path = download_line_content(message["id"], token, resolve_path(CONFIG["image_archive_dir"]))
            request_id = str(state.get("hr_request_id") or "")
            result = save_medical_certificate_to_google(request_id, image_path, line_user_id)
        except Exception as exc:
            runtime_log(f"Medical certificate save failed: {exc}")
            clear_user_state(line_user_id)
            return abort_flow_message(f"บันทึกใบรับรองแพทย์ไม่สำเร็จค่ะ ({exc})")
        request_data = dict(state.get("hr_request") or {})
        approver_id = str(CONFIG.get("hr_approver_line_id") or os.getenv("HR_APPROVER_LINE_ID") or "Ud260925c43fb0823fea42224a2929393")
        file_url = result.get("fileUrl") or result.get("url") or ""
        push_line_messages(
            approver_id,
            [
                text_message(
                    "ได้รับใบรับรองแพทย์สำหรับคำขอลาป่วย\n\n"
                    + format_hr_request(request_data)
                    + f"\n\nRequest ID: {request_id}\nไฟล์: {file_url}"
                ),
                approval_buttons_message(request_id),
            ],
        )
        clear_user_state(line_user_id)
        return f"บันทึกใบรับรองแพทย์เรียบร้อยค่ะ\nRequest ID: {request_id}\n{file_url}"

    if state.get("mode") != "awaiting_image":
        return [
            text_message("กรุณาเลือกเมนูก่อนส่งรูปค่ะ"),
            menu_message(),
        ]

    token = os.getenv(CONFIG["line"]["channel_access_token_env"])
    if not token:
        raise RuntimeError(f"Missing {CONFIG['line']['channel_access_token_env']}")

    try:
        runtime_log("Downloading LINE image content")
        image_path = download_line_content(message["id"], token, resolve_path(CONFIG["image_archive_dir"]))
        runtime_log(f"Downloaded LINE image to {image_path}")
        runtime_log("OCR started")
        text = ocr_image(image_path)
        runtime_log(f"OCR completed characters={len(text)}")
    except Exception as exc:
        runtime_log(f"OCR failed: {exc}")
        draft = blank_manual_entry(state.get("transaction_type", "Expense"))
        draft["submitter_name"] = get_line_display_name(line_user_id)
        set_user_state(
            line_user_id,
            {
                "mode": "awaiting_correction",
                "transaction_type": draft["transaction_type"],
                "image_path": str(image_path) if "image_path" in locals() else "",
                "pending_data": serialize_data(draft),
            },
        )
        return manual_entry_form(draft)
        return (
            "OCR อ่านเอกสารไม่สำเร็จหรือใช้เวลานานเกินไปค่ะ\n"
            "กรุณาถ่ายรูปใหม่ให้เห็นเฉพาะเอกสารเต็มหน้า ตัวหนังสือชัด และไม่เอียงมาก\n"
            "จากนั้นส่งรูปเข้ามาอีกครั้งค่ะ"
        )
    parsed = parse_receipt_text(text, float(CONFIG.get("vat_rate", 0.07)))
    parsed = apply_transaction_type_defaults(parsed, state.get("transaction_type", "Expense"))
    parsed["submitter_name"] = parsed.get("submitter_name") or get_line_display_name(line_user_id)
    set_user_state(
        line_user_id,
        {
            "mode": "awaiting_confirmation",
            "transaction_type": parsed["transaction_type"],
            "image_path": str(image_path),
            "pending_data": serialize_data(parsed),
        },
    )
    runtime_log(
        f"Parsed LINE receipt pending confirmation type={parsed['transaction_type']} "
        f"date={parsed['date']} total={parsed['total']} vendor={parsed['vendor']!r}"
    )
    return confirmation_prompt(parsed)


class LineWebhookHandler(BaseHTTPRequestHandler):
    server_version = "LineExpenseBot/2.0"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok", "version": self.server_version})
            return
        if self.path.startswith("/files/"):
            filename = Path(unquote(self.path.split("/files/", 1)[1])).name
            file_path = reply_image_dir() / filename
            if not file_path.exists() or not file_path.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "file not found"})
                return
            data = file_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", mimetypes.guess_type(str(file_path))[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            runtime_log(f"POST received path={self.path}")
            if self.path != "/callback":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            runtime_log(f"POST /callback content_length={length} signature_present={bool(self.headers.get('X-Line-Signature'))}")

            secret = os.getenv(CONFIG["line"]["channel_secret_env"])
            if not secret:
                runtime_log(f"Webhook rejected: missing {CONFIG['line']['channel_secret_env']} in this PowerShell session")
                self._send_json(HTTPStatus.OK, {"status": "accepted_with_error", "error": "missing channel secret"})
                return

            signature = self.headers.get("X-Line-Signature", "")
            if not verify_line_signature(body, signature, secret):
                runtime_log("Webhook rejected: invalid X-Line-Signature")
                self._send_json(HTTPStatus.OK, {"status": "accepted_with_error", "error": "invalid signature"})
                return

            payload = json.loads(body.decode("utf-8"))
            runtime_log(f"Webhook payload events={len(payload.get('events', []))}")
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
            proto = self.headers.get("X-Forwarded-Proto") or ("https" if "ngrok" in host else "http")
            public_base_url = f"{proto}://{host}" if host else ""
            for event in payload.get("events", []):
                reply = process_line_event_menu(event, public_base_url)
                if reply and event.get("replyToken"):
                    if isinstance(reply, list):
                        reply_messages(event["replyToken"], reply)
                    elif isinstance(reply, dict):
                        reply_messages(event["replyToken"], [reply])
                    else:
                        reply_text(event["replyToken"], reply)
            self._send_json(HTTPStatus.OK, {"status": "ok"})
        except Exception as exc:
            runtime_log(f"LINE webhook error: {exc}")
            self._send_json(HTTPStatus.OK, {"status": "accepted_with_error", "error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def main() -> int:
    global CONFIG, CONFIG_PATH, STATE_CACHE
    parser = argparse.ArgumentParser(description="LINE webhook service that imports receipt images into VAT Excel.")
    parser.add_argument("--config", required=True, help="Path to line_bot_config.example.json or your copied config.")
    args = parser.parse_args()

    CONFIG_PATH = Path(args.config).resolve()
    CONFIG = load_config(CONFIG_PATH)
    STATE_CACHE = load_state_cache()
    workbook_path = resolve_path(CONFIG["workbook"])
    if not workbook_path.exists():
        raise SystemExit(f"Workbook not found: {workbook_path}")

    secret_name = CONFIG["line"]["channel_secret_env"]
    token_name = CONFIG["line"]["channel_access_token_env"]
    runtime_log(f"Startup workbook={workbook_path}")
    runtime_log(f"Startup {secret_name} set={bool(os.getenv(secret_name))}")
    runtime_log(f"Startup {token_name} set={bool(os.getenv(token_name))}")
    runtime_log(f"Startup google_apps_script_url set={bool(CONFIG.get('google_apps_script_url'))}")
    runtime_log(f"Startup google_apps_script_secret set={bool(CONFIG.get('google_apps_script_secret'))}")

    host = CONFIG.get("host", "0.0.0.0")
    port = int(CONFIG.get("port", 8080))
    server = ThreadingHTTPServer((host, port), LineWebhookHandler)
    print(f"LINE expense bot listening on http://{host}:{port}/callback")
    print(f"Workbook: {workbook_path}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
