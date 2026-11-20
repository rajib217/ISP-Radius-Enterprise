"""Inventory: stock items and every movement in and out."""
from datetime import date

from flask import abort, flash, g, redirect, render_template, request, url_for

import core
from core import admin_required, audit, commit, get_db, insert, pager, parse_date, q, route, x

KINDS = [("in", "Stock in (purchase)"), ("out", "Stock out (used / sold)"), ("assign", "Assign to a customer"),
         ("return", "Returned from a customer"), ("adjust", "Set counted quantity")]
CATEGORIES = ["Router", "ONU / ONT", "Switch", "Fibre cable", "LAN cable", "Connector", "Splitter", "Patch cord",
              "Power adapter", "Tool", "Other"]


@route("/inventory", module="inventory")
def inventory():
    where, params = [], []
    text = request.args.get("q", "").strip()
    if text:
        where.append("(i.name LIKE %s OR i.sku LIKE %s OR i.location LIKE %s)")
        params += ["%" + text + "%"] * 3
    if request.args.get("category"):
        where.append("i.category = %s")
        params.append(request.args["category"])
    low = request.args.get("low") == "1"
    if low:
        where.append("i.min_qty > 0 AND i.qty <= i.min_qty")
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    tot = q("SELECT COUNT(*) AS c, COALESCE(SUM(i.qty * i.unit_cost), 0) AS stock_value, "
            "COALESCE(SUM(i.min_qty > 0 AND i.qty <= i.min_qty), 0) AS low FROM inventory_items i" + cond,
            params, one=True)
    pg = pager(tot["c"])
    rows = q("SELECT i.* FROM inventory_items i" + cond + " ORDER BY i.name LIMIT %s OFFSET %s",
             params + [pg["per"], pg["offset"]])
    cats = sorted(set(CATEGORIES) | {r["category"] for r in q("SELECT DISTINCT category FROM inventory_items")})
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("inventory.html", rows=rows, tot=tot, pg=pg, cats=cats, keep=keep, kinds=KINDS, low=low)


@route("/inventory/save", module="inventory", methods=["POST"])
def inventory_save():
    f = request.form
    iid = int(f["id"]) if f.get("id", "").isdigit() else 0
    try:
        name, category = f.get("name", "").strip(), f.get("category", "").strip() or "Other"
        unit = f.get("unit", "").strip() or "pcs"
        min_qty = int(f.get("min_qty") or 0)
        cost = round(float(f.get("unit_cost") or 0), 2)
        qty = int(f.get("qty") or 0)
        assert 1 <= len(name) <= 128 and len(category) <= 64 and len(unit) <= 16
        assert 0 <= min_qty < 10 ** 7 and 0 <= cost < 10 ** 8 and 0 <= qty < 10 ** 7
    except (ValueError, AssertionError):
        flash("Check the item: a name, whole-number quantities and a valid cost are required.", "error")
        return redirect(url_for("inventory"))
    sku = f.get("sku", "").strip()[:64] or None
    if sku and q("SELECT id FROM inventory_items WHERE sku = %s AND id <> %s", (sku, iid), one=True):
        flash("Another item already uses the code %s." % sku, "error")
        return redirect(url_for("inventory"))
    loc, note = f.get("location", "").strip()[:128], f.get("note", "").strip()[:255]
    if iid:
        x("UPDATE inventory_items SET name = %s, category = %s, sku = %s, unit = %s, min_qty = %s, unit_cost = %s, "
          "location = %s, note = %s WHERE id = %s", (name, category, sku, unit, min_qty, cost, loc, note, iid))
    else:
        iid = insert("INSERT INTO inventory_items (name, category, sku, unit, qty, min_qty, unit_cost, location, note) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (name, category, sku, unit, qty, min_qty, cost, loc, note))
        if qty:
            insert("INSERT INTO inventory_moves (item_id, kind, qty, note, by_user) VALUES (%s, 'in', %s, "
                   "'Opening stock', %s)", (iid, qty, g.me["username"]))
    audit("inventory_saved", name)
    commit()
    flash("Item saved.", "success")
    return redirect(url_for("inventory"))


@route("/inventory/move", module="inventory", methods=["POST"])
def inventory_move():
    f = request.form
    back = core.safe_next(f.get("next")) or url_for("inventory")
    kind = f.get("kind", "")
    try:
        qty = int(f.get("qty", "0"))
        iid = int(f.get("item_id", "0"))
        assert kind in dict(KINDS) and 0 <= qty < 10 ** 7 and (qty > 0 or kind == "adjust")
    except (ValueError, AssertionError):
        flash("Choose an item, a movement type and a whole-number quantity.", "error")
        return redirect(back)
    item = q("SELECT * FROM inventory_items WHERE id = %s FOR UPDATE", (iid,), one=True)
    if not item:
        flash("That item no longer exists.", "error")
        return redirect(back)
    sub = None
    username = f.get("username", "").strip()
    if username:
        sub = q("SELECT id, username FROM subscribers WHERE username = %s", (username,), one=True)
        if not sub:
            flash("No subscriber has the username %s." % username, "error")
            return redirect(back)
    if kind == "assign" and not sub:
        flash("Enter the customer's username to assign this item.", "error")
        return redirect(back)
    note = f.get("note", "").strip()[:255]
    if kind in ("in", "return"):
        x("UPDATE inventory_items SET qty = qty + %s WHERE id = %s", (qty, iid))
        logged = qty
    elif kind in ("out", "assign"):
        if not x("UPDATE inventory_items SET qty = qty - %s WHERE id = %s AND qty >= %s", (qty, iid, qty)):
            flash("Only %d %s of %s in stock." % (item["qty"], item["unit"], item["name"]), "error")
            return redirect(back)
        logged = qty
    else:
        logged = abs(qty - item["qty"])
        note = ("Counted %d, was %d. " % (qty, item["qty"]) + note).strip()
        x("UPDATE inventory_items SET qty = %s WHERE id = %s", (qty, iid))
    insert("INSERT INTO inventory_moves (item_id, kind, qty, subscriber_id, username, serial, note, by_user) "
           "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
           (iid, kind, logged, sub["id"] if sub else None, sub["username"] if sub else "",
            f.get("serial", "").strip()[:128], note, g.me["username"]))
    audit("stock_" + kind, item["name"], "%d" % logged)
    commit()
    flash("Stock updated.", "success")
    return redirect(back)


@route("/inventory/<int:iid>/delete", module="inventory", methods=["POST"])
@admin_required
def inventory_delete(iid):
    item = q("SELECT name FROM inventory_items WHERE id = %s", (iid,), one=True) or abort(404)
    x("DELETE FROM inventory_moves WHERE item_id = %s", (iid,))
    x("DELETE FROM inventory_items WHERE id = %s", (iid,))
    audit("inventory_deleted", item["name"])
    commit()
    flash("%s and its stock history were deleted." % item["name"], "success")
    return redirect(url_for("inventory"))


@route("/inventory/log", module="inventory")
def inventory_moves():
    where, params = [], []
    text = request.args.get("q", "").strip()
    if text:
        where.append("(m.username LIKE %s OR m.serial LIKE %s OR i.name LIKE %s)")
        params += ["%" + text + "%"] * 3
    if request.args.get("kind") in dict(KINDS):
        where.append("m.kind = %s")
        params.append(request.args["kind"])
    if request.args.get("item", "").isdigit():
        where.append("m.item_id = %s")
        params.append(int(request.args["item"]))
    try:
        start, end = parse_date(request.args.get("from")), parse_date(request.args.get("to"))
    except ValueError:
        start = end = None
    if start:
        where.append("m.created_at >= %s")
        params.append(start)
    if end:
        where.append("m.created_at < %s + INTERVAL 1 DAY")
        params.append(end)
    cond = (" WHERE " + " AND ".join(where)) if where else ""
    total = q("SELECT COUNT(*) AS c FROM inventory_moves m JOIN inventory_items i ON i.id = m.item_id" + cond,
              params, one=True)["c"]
    pg = pager(total)
    rows = q("SELECT m.*, i.name AS item_name, i.unit, s.id AS sid FROM inventory_moves m "
             "JOIN inventory_items i ON i.id = m.item_id LEFT JOIN subscribers s ON s.id = m.subscriber_id" + cond +
             " ORDER BY m.id DESC LIMIT %s OFFSET %s", params + [pg["per"], pg["offset"]])
    items = q("SELECT id, name FROM inventory_items ORDER BY name")
    keep = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("inventory_moves.html", rows=rows, pg=pg, items=items, kinds=KINDS, keep=keep)
