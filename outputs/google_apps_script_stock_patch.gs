// 1) Add this near the top of Code.gs after var LOG = "Import_Log";
var STOCK_SPREADSHEET_ID = "1Wu3jj8iHu70DKecFuWTu6_yf8T418GBv74Mz_zOtgWQ";

// 2) Add this inside doPost(e), after updateDocumentType action:
// if (p.action === "searchStock") {
//   return out({status: "ok", matches: searchStock(String(p.branch || ""), String(p.query || ""))});
// }

// 3) Add these functions anywhere below searchByTotal(...) and above out(...):
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
