var APP_SECRET = "KJao-VAT-Line-2026-Secret";
var TX = "Transactions_12M";
var EX = "Expenses";
var RV = "Revenue";
var LOG = "Import_Log";
var STOCK_SPREADSHEET_ID = "1Wu3jj8iHu70DKecFuWTu6_yf8T418GBv74Mz_zOtgWQ";
var HR_REQ = "HR_Requests";
var HR_MED = "HR_Medical_Certificates";
var HR_SCHEDULE = "HR_Work_Schedule";
var HR_MEDICAL_FOLDER = "LINE HR Medical Certificates";
var HEADERS = ["Date","Type","Invoice No","Vendor","Description","Category","Before VAT","VAT Rate","VAT","Total","Claimable","Month","Image URL","Confidence","Revenue Before VAT","Expense Before VAT","Raw Text","Document Type","LINE User ID","Submitter Name","Saved At"];
var HR_HEADERS = ["Request ID","Submitted At","Request Type","Employee Name","Start Date","End Date","Work Date","Old Date","New Date","Old Time","New Time","Reason","Note","Status","LINE User ID","Approver Note","Updated At"];
var HR_MED_HEADERS = ["Request ID","Uploaded At","Employee Name","Leave Date","File Name","File URL","LINE User ID"];
var HR_SCHEDULE_HEADERS = ["Date","Branch","Employee Name","Start Time","End Time","Role","Note"];

function doGet() {
  return out({status: "ok", version: "safe-paste-2026-06-21"});
}

function authorizeDriveAccess() {
  var folder = getOrCreateFolder(HR_MEDICAL_FOLDER);
  return "Drive permission ready: " + folder.getName();
}

function doPost(e) {
  try {
    var p = JSON.parse(e.postData.contents || "{}");
    if (p.secret !== APP_SECRET) {
      return out({status: "error", message: "unauthorized"});
    }
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    ensure(ss, TX, HEADERS);
    ensure(ss, EX, HEADERS);
    ensure(ss, RV, HEADERS);
    ensure(ss, LOG, ["Time","Status","Invoice No","Type","Vendor","Total","Note"]);
    ensure(ss, HR_REQ, HR_HEADERS);
    ensure(ss, HR_MED, HR_MED_HEADERS);
    ensure(ss, HR_SCHEDULE, HR_SCHEDULE_HEADERS);
    if (p.action === "searchByTotal") {
      return out({status: "ok", matches: searchByTotal(ss, num(p.total))});
    }
    if (p.action === "deleteRow") {
      return out(deleteRow(ss, Number(p.row), String(p.sheetName || TX)));
    }
    if (p.action === "updateDocumentType") {
      return out(updateDocType(ss, Number(p.row), String(p.documentType || ""), String(p.sheetName || TX)));
    }
    if (p.action === "searchStock") {
      return out({status: "ok", matches: searchStock(String(p.branch || ""), String(p.query || ""))});
    }
    if (p.action === "saveHrRequest") {
      return out(saveHrRequest(ss, p.data || {}));
    }
    if (p.action === "saveMedicalCertificate") {
      return out(saveMedicalCertificate(ss, p));
    }
    if (p.action === "getHrSchedule") {
      var schedule = ss.getSheetByName(HR_SCHEDULE);
      return out({status: "ok", sheetName: HR_SCHEDULE, url: ss.getUrl() + "#gid=" + schedule.getSheetId()});
    }
    var d = p.data || p;
    var row = buildRow(d);
    var type = normType(d.type || "Expense");
    ss.getSheetByName(TX).appendRow(row);
    ss.getSheetByName(type === "Revenue" ? RV : EX).appendRow(row);
    log(ss, "ok", d, "");
    return out({status: "ok", row: ss.getSheetByName(TX).getLastRow(), type: type});
  } catch (err) {
    return out({status: "error", message: String(err && err.message || err)});
  }
}

function ensure(ss, name, headers) {
  var sh = ss.getSheetByName(name);
  if (!sh) sh = ss.insertSheet(name);
  if (sh.getLastRow() === 0) sh.appendRow(headers);
}

function buildRow(d) {
  var type = normType(d.type || "Expense");
  var date = d.date || d.documentDate || "";
  var beforeVat = num(d.beforeVat || d.before_vat || d.subtotal);
  var vatRate = num(d.vatRate || d.vat_rate || 0.07);
  var vat = num(d.vat || d.vatAmount || d.vat_amount);
  var total = num(d.total || d.totalAmount || d.total_amount);
  var month = d.month || monthKey(date);
  return [date,type,d.invoiceNo || d.invoice_no || d.documentNo || d.billNo || "-",d.vendor || d.shopName || d.partner || "",d.description || "",d.category || "",beforeVat,vatRate,vat,total,d.claimable || "Yes",month,d.imageUrl || "",d.confidence || "",type === "Revenue" ? beforeVat : 0,type === "Expense" ? beforeVat : 0,d.rawText || d.raw_text || "",d.documentType || d.document_type || "",d.lineUserId || d.line_user_id || "",d.submitterName || d.submitter_name || "",new Date()];
}

function searchByTotal(ss, total) {
  var matches = [];
  var seen = {};
  [TX, EX, RV].forEach(function(sheetName) {
    var sh = ss.getSheetByName(sheetName);
    if (!sh) return;
    var values = sh.getDataRange().getValues();
    for (var i = 1; i < values.length; i++) {
      var r = values[i];
      var beforeVat = num(r[6]);
      var rowTotal = num(r[9]);
      var matchedBy = Math.abs(rowTotal - total) <= 0.01 ? "Total" : (Math.abs(beforeVat - total) <= 0.01 ? "Before VAT" : "");
      var key = [r[0], r[1], r[2], r[3], beforeVat, rowTotal].join("|");
      if (matchedBy && !(sheetName !== TX && seen[key])) {
        seen[key] = true;
        matches.push({row: i + 1, sheetName: sheetName, date: fmtDate(r[0]), type: r[1], invoiceNo: r[2], vendor: r[3], description: r[4], category: r[5], beforeVat: beforeVat, vat: num(r[8]), total: rowTotal, matchedBy: matchedBy, documentType: r[17], lineUserId: r[18], submitterName: r[19]});
      }
    }
  });
  return matches;
}

function searchStock(branch, query) {
  var tab = stockTab(branch);
  var q = String(query || "").toLowerCase().trim();
  if (!q) return [];
  var ss = SpreadsheetApp.openById(STOCK_SPREADSHEET_ID);
  var sh = ss.getSheetByName(tab);
  if (!sh) throw new Error("stock tab not found: " + tab);
  var values = sh.getDataRange().getValues();
  var outRows = [];
  for (var i = 1; i < values.length; i++) {
    var r = values[i];
    var hay = [r[1], r[2], r[3], r[4], r[5], r[11], r[12]].join(" ").toLowerCase();
    if (hay.indexOf(q) >= 0) {
      outRows.push({
        row: i + 1,
        branch: r[0],
        name: r[1],
        window: r[2],
        category: r[3],
        barcode: r[4],
        sku: r[5],
        quantity: r[6],
        lowStock: r[7],
        price: r[8],
        cost: r[9],
        stockValue: r[10],
        color: r[11],
        size: r[12]
      });
      if (outRows.length >= 20) break;
    }
  }
  return outRows;
}

function stockTab(branch) {
  var b = String(branch || "").trim();
  if (b === "สี่แยก") return "Stock_สี่แยก";
  if (b === "พัสดุสี่แยก") return "Stock_พัสดุสี่แยก";
  if (b === "ทะเล") return "Stock_ทะเล";
  if (b === "เขาใหญ่") return "Stock_เขาใหญ่";
  return "Stock_" + b;
}

function saveHrRequest(ss, d) {
  var sh = ss.getSheetByName(HR_REQ);
  var requestId = d.requestId || d.request_id || ("HR-" + Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyyMMdd-HHmmss") + "-" + Math.floor(Math.random() * 1000));
  var row = [
    requestId,
    new Date(),
    d.requestType || d.request_type || "",
    d.employeeName || d.employee_name || "",
    d.startDate || d.start_date || "",
    d.endDate || d.end_date || "",
    d.workDate || d.work_date || "",
    d.oldDate || d.old_date || "",
    d.newDate || d.new_date || "",
    d.oldTime || d.old_time || "",
    d.newTime || d.new_time || "",
    d.reason || "",
    d.note || "",
    d.status || "รออนุมัติ",
    d.lineUserId || d.line_user_id || "",
    "",
    new Date()
  ];
  sh.appendRow(row);
  return {status: "ok", requestId: requestId, row: sh.getLastRow(), sheetName: HR_REQ};
}

function saveMedicalCertificate(ss, p) {
  var requestId = String(p.requestId || p.request_id || "");
  if (!requestId) throw new Error("missing requestId");
  var bytes = Utilities.base64Decode(String(p.data || ""));
  var mimeType = String(p.mimeType || p.mime_type || "image/jpeg");
  var fileName = String(p.fileName || p.file_name || (requestId + ".jpg"));
  var folder = getOrCreateFolder(HR_MEDICAL_FOLDER);
  var file = folder.createFile(Utilities.newBlob(bytes, mimeType, fileName));
  var request = findHrRequest(ss, requestId);
  var sh = ss.getSheetByName(HR_MED);
  sh.appendRow([
    requestId,
    new Date(),
    request.employeeName || "",
    request.startDate || "",
    fileName,
    file.getUrl(),
    p.lineUserId || p.line_user_id || ""
  ]);
  return {status: "ok", requestId: requestId, fileId: file.getId(), fileUrl: file.getUrl(), sheetName: HR_MED, row: sh.getLastRow()};
}

function findHrRequest(ss, requestId) {
  var sh = ss.getSheetByName(HR_REQ);
  var values = sh.getDataRange().getValues();
  for (var i = 1; i < values.length; i++) {
    if (String(values[i][0]) === String(requestId)) {
      return {employeeName: values[i][3], startDate: fmtDate(values[i][4])};
    }
  }
  return {};
}

function getOrCreateFolder(name) {
  var folders = DriveApp.getFoldersByName(name);
  if (folders.hasNext()) return folders.next();
  return DriveApp.createFolder(name);
}

function deleteRow(ss, row, sheetName) {
  var name = [TX, EX, RV].indexOf(sheetName) >= 0 ? sheetName : TX;
  var sh = ss.getSheetByName(name);
  if (!row || row <= 1 || row > sh.getLastRow()) throw new Error("invalid row");
  var v = sh.getRange(row, 1, 1, HEADERS.length).getValues()[0];
  sh.deleteRow(row);
  var linked = deleteLinkedRows(ss, name, v);
  log(ss, "deleted", {invoiceNo: v[2], type: v[1], vendor: v[3], total: v[9]}, "Deleted row " + row + " from " + name + "; linked=" + linked);
  return {status: "ok", message: "deleted", row: row, sheetName: name, linkedDeleted: linked};
}

function deleteLinkedRows(ss, sourceName, sourceRow) {
  var deleted = 0;
  [TX, EX, RV].forEach(function(name) {
    if (name === sourceName) return;
    var sh = ss.getSheetByName(name);
    if (!sh) return;
    var values = sh.getDataRange().getValues();
    for (var i = values.length - 1; i >= 1; i--) {
      if (sameBill(sourceRow, values[i])) {
        sh.deleteRow(i + 1);
        deleted++;
      }
    }
  });
  return deleted;
}

function sameBill(a, b) {
  var aImage = String(a[12] || "");
  var bImage = String(b[12] || "");
  if (aImage && bImage && aImage === bImage) return true;
  var aSaved = String(a[20] || a[17] || "");
  var bSaved = String(b[20] || b[17] || "");
  if (aSaved && bSaved && aSaved === bSaved && Math.abs(num(a[9]) - num(b[9])) <= 0.01) return true;
  return false;
}

function updateDocType(ss, row, docType, sheetName) {
  var name = [TX, EX, RV].indexOf(sheetName) >= 0 ? sheetName : TX;
  var sh = ss.getSheetByName(name);
  if (!row || row <= 1 || row > sh.getLastRow()) throw new Error("invalid row");
  sh.getRange(row, 18).setValue(docType);
  return {status: "ok", message: "updated", row: row, sheetName: name, documentType: docType};
}

function log(ss, status, d, note) {
  ss.getSheetByName(LOG).appendRow([new Date(), status, d.invoiceNo || d.invoice_no || "-", normType(d.type || "Expense"), d.vendor || "", d.total || "", note || ""]);
}

function normType(v) {
  return String(v || "").toLowerCase().indexOf("revenue") >= 0 ? "Revenue" : "Expense";
}

function num(v) {
  if (v === null || v === undefined || v === "") return 0;
  if (typeof v === "number") return v;
  var n = parseFloat(String(v).replace(/,/g, "").replace(/[^\d.-]/g, ""));
  return isNaN(n) ? 0 : n;
}

function monthKey(v) {
  var d = v ? new Date(v) : new Date();
  return isNaN(d.getTime()) ? "" : d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0");
}

function fmtDate(v) {
  return v instanceof Date ? Utilities.formatDate(v, Session.getScriptTimeZone(), "yyyy-MM-dd") : String(v || "");
}

function out(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
