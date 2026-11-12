"""Loads the 'procurement system': vendor master, purchase orders and goods receipts (CSV stand-ins for an ERP)."""
import csv
from functools import lru_cache

from config import DATA_DIR


@lru_cache(maxsize=1)
def vendors():
    with open(DATA_DIR / "vendors.csv") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["aliases"] = [a for a in r["aliases"].split("|") if a]
    return {r["vendor_id"]: r for r in rows}


@lru_cache(maxsize=1)
def purchase_orders():
    pos = {}
    with open(DATA_DIR / "purchase_orders.csv") as f:
        for r in csv.DictReader(f):
            po = pos.setdefault(r["po_number"], {"po_number": r["po_number"], "vendor_id": r["vendor_id"],
                                                 "po_date": r["po_date"], "status": r["status"],
                                                 "currency": r["currency"], "lines": []})
            po["lines"].append({"line_no": int(r["line_no"]), "item_code": r["item_code"],
                                "description": r["description"], "quantity": float(r["quantity"]),
                                "unit_price": float(r["unit_price"])})
    for po in pos.values():
        po["value"] = round(sum(l["quantity"] * l["unit_price"] for l in po["lines"]), 2)
    return pos


@lru_cache(maxsize=1)
def goods_receipts():
    grn = {}
    with open(DATA_DIR / "goods_receipts.csv") as f:
        for r in csv.DictReader(f):
            grn.setdefault(r["po_number"], {})
            grn[r["po_number"]][r["item_code"]] = grn[r["po_number"]].get(r["item_code"], 0) + float(r["received_qty"])
    return grn
