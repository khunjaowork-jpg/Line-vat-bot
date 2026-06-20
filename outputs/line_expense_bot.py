from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sys
import uuid
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


def text_message(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text[:4900]}


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
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})",
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})",
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
            runtime_log("Google Vision OCR returned empty text; falling back to Tesseract")
        except Exception as exc:
            runtime_log(f"Google Vision OCR failed: {exc}; falling back to Tesseract")

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
        timeout = int(CONFIG.get("ocr_timeout_seconds", 25))
        try:
            return pytesseract.image_to_string(image, lang=lang, config=tesseract_config, timeout=timeout)
        except RuntimeError as exc:
            runtime_log(f"OCR primary pass failed: {exc}; retrying fast eng pass")
            fast_config = CONFIG.get("tesseract_fast_config", "--oem 1 --psm 11")
            fast_timeout = int(CONFIG.get("ocr_fast_timeout_seconds", 25))
            return pytesseract.image_to_string(image, lang="eng", config=fast_config, timeout=fast_timeout)


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
    timeout = int(CONFIG.get("google_vision_timeout_seconds", 45))
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
        f"หมวด: {data.get('category') or '-'}\n"
        f"ยอดก่อน VAT: {float(data.get('before_vat') or 0):,.2f}\n"
        f"VAT: {float(data.get('vat') or 0):,.2f}\n"
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
        f"หมวด: {data.get('category') or ''}\n"
        f"ยอดก่อน VAT: {float(data.get('before_vat') or 0):.2f}\n"
        f"VAT: {float(data.get('vat') or 0):.2f}\n"
        f"ยอดรวม: {float(data.get('total') or 0):.2f}"
    )


def menu_text() -> str:
    return (
        "กรุณาเลือกเมนู\n"
        "1. บิลรายรับ\n"
        "2. บิลรายจ่าย\n"
        "3. เรียกดูรายละเอียดบัญชี\n\n"
        "พิมพ์ชื่อเมนูที่ต้องการได้เลยค่ะ"
    )


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
        "category": "category",
        "\u0e2b\u0e21\u0e27\u0e14": "category",
        "description": "description",
        "\u0e23\u0e32\u0e22\u0e25\u0e30\u0e40\u0e2d\u0e35\u0e22\u0e14": "description",
        "before vat": "before_vat",
        "\u0e22\u0e2d\u0e14\u0e01\u0e48\u0e2d\u0e19 vat": "before_vat",
        "\u0e22\u0e2d\u0e14\u0e01\u0e48\u0e2d\u0e19\u0e20\u0e32\u0e29\u0e35": "before_vat",
        "vat": "vat",
        "\u0e20\u0e32\u0e29\u0e35": "vat",
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
        elif key in {"before_vat", "vat", "total"}:
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
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def render_row_summary_image(sheet_name: str, row: int, data: dict[str, Any], heading: str) -> Path:
    from PIL import Image, ImageDraw

    out_dir = reply_image_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"excel_row_{row}_{uuid.uuid4().hex[:8]}.png"
    width, height = 1100, 760
    image = Image.new("RGB", (width, height), "#F8FAFC")
    draw = ImageDraw.Draw(image)
    title_font = find_font(36)
    label_font = find_font(25)
    value_font = find_font(25)
    small_font = find_font(21)

    accent = "#BBF7D0" if data.get("transaction_type") == "Revenue" else "#FECACA"
    draw.rounded_rectangle((28, 28, width - 28, 120), radius=22, fill=accent)
    draw.text((58, 52), heading, fill="#111827", font=title_font)
    draw.text((width - 300, 62), f"{sheet_name}!Row {row}", fill="#374151", font=small_font)

    fields = [
        ("ประเภท", data.get("transaction_type", "-")),
        ("ประเภทเอกสาร", data.get("document_type") or "-"),
        ("วันที่", str(data.get("date", "-"))),
        ("เลขที่บิล", data.get("invoice_no") or "-"),
        ("ชื่อร้าน/คู่ค้า", data.get("vendor") or "-"),
        ("หมวด", data.get("category") or "-"),
        ("รายละเอียด", data.get("description") or "-"),
        ("ยอดก่อน VAT", f"{float(data.get('before_vat') or 0):,.2f}"),
        ("VAT", f"{float(data.get('vat') or 0):,.2f}"),
        ("ยอดรวม", f"{float(data.get('total') or 0):,.2f}"),
    ]
    y = 155
    for label, value in fields:
        draw.rounded_rectangle((42, y - 8, width - 42, y + 48), radius=10, fill="#FFFFFF", outline="#E5E7EB")
        draw.text((70, y), label, fill="#475569", font=label_font)
        draw.text((345, y), str(value)[:58], fill="#111827", font=value_font)
        y += 62
    image.save(path, quality=92)
    return path


def public_file_url(base_url: str, path: Path) -> str:
    return f"{base_url.rstrip('/')}/files/{path.name}"


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


def process_line_event_menu(event: dict[str, Any], public_base_url: str) -> str | list[dict[str, Any]] | None:
    runtime_log(f"Received LINE event type={event.get('type')} message_type={event.get('message', {}).get('type')}")
    if event.get("type") != "message":
        return None
    message = event.get("message", {})
    line_user_id = event.get("source", {}).get("userId", "")
    state = get_user_state(line_user_id)

    if message.get("type") == "text":
        text = str(message.get("text") or "").strip()
        if text in {"เมนู", "menu", "Menu", "MENU"}:
            clear_user_state(line_user_id)
            return menu_text()
        if text in {"1", "บิลรายรับ"}:
            set_user_state(line_user_id, {"mode": "awaiting_image", "transaction_type": "Revenue"})
            return "ส่งเอกสารเพื่อลงรายละเอียดในระบบได้เลยค่ะ"
        if text in {"2", "บิลรายจ่าย"}:
            set_user_state(line_user_id, {"mode": "awaiting_image", "transaction_type": "Expense"})
            return "ส่งเอกสารเพื่อลงรายละเอียดในระบบได้เลยค่ะ"
        if text in {"3", "เรียกดูรายละเอียดบัญชี"}:
            set_user_state(line_user_id, {"mode": "awaiting_lookup_bill_no"})
            return "กรุณาพิมพ์เลขที่บิล ชื่อร้าน/คู่ค้า หรือยอดรวมที่ต้องการตรวจสอบค่ะ"
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
            try:
                send_to_google_sheet(pending, image_path, line_user_id, public_base_url)
            except Exception as exc:
                runtime_log(f"Google Sheet save failed: {exc}")
                return f"Google Sheet: ยังไม่สำเร็จ ({exc})\nข้อมูลยังไม่ถูกล้าง กรุณาลองพิมพ์ ตรวจสอบและยืนยัน อีกครั้งหลังแก้ปัญหา"
            clear_user_state(line_user_id)
            summary_image = render_row_summary_image(sheet_name, row, pending, "บิลนำเข้า")
            runtime_log(
                f"Confirmed LINE receipt -> Google Sheet "
                f"type={pending.get('transaction_type')} date={pending['date']} total={pending['total']}"
            )
            return [
                text_message("บันทึกเรียบร้อย\nGoogle Sheet: บันทึกสำเร็จ"),
                image_message(public_file_url(public_base_url, summary_image)),
            ]
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
        return menu_text()

    if message.get("type") not in {"image", "file"}:
        return menu_text()

    if state.get("mode") != "awaiting_image":
        return "กรุณาเลือกเมนูก่อนส่งรูปค่ะ\n\n" + menu_text()

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
        return (
            "OCR อ่านเอกสารไม่สำเร็จหรือใช้เวลานานเกินไปค่ะ\n"
            "กรุณาถ่ายรูปใหม่ให้เห็นเฉพาะเอกสารเต็มหน้า ตัวหนังสือชัด และไม่เอียงมาก\n"
            "จากนั้นส่งรูปเข้ามาอีกครั้งค่ะ"
        )
    parsed = parse_receipt_text(text, float(CONFIG.get("vat_rate", 0.07)))
    parsed = apply_transaction_type_defaults(parsed, state.get("transaction_type", "Expense"))
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
