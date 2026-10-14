"""
Debug sync for a specific set of Ginesys items.

Syncs only the given Style Nos (CNAME1) (same logic as finished_item_sync), in
batches, and groups everything that does not sync by the reason it failed.
Sync Setting timestamps are NOT touched, so this can be run any number of times
alongside the normal sync.

Run from the bench folder:

    bench --site <site> execute ginesys_migration.scripts.debug_item_sync.run \
        --kwargs "{'file_path': r'C:\\Users\\asus\\Downloads\\Sync Issue.txt'}"

or with an explicit list:

    bench --site <site> execute ginesys_migration.scripts.debug_item_sync.run \
        --kwargs "{'items': ['K15348', 'LC0002']}"

`file_path` can be any text file with one code per line. Lines like
"Sync Error : K15348" are accepted as they are; blank / other lines are ignored.
"""

import csv
import os
import re
import time
import traceback
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from datetime import datetime

import frappe
from frappe.utils import cint, flt, now_datetime, strip_html_tags

from ginesys_migration.utils.oracle import get_ginesys_connection
import ginesys_migration.scripts.finished_item_sync as fis
from ginesys_migration.scripts.finished_item_sync import (
    COST_PRICE_LIST,
    DEF_FIELDS,
    DEFAULT_NON_FINISHED_HSN,
    format_too_long,
    get_definition_values,
    get_item_group_map,
    log_too_long,
    move_barcode_to_item,
    update_price,
)

# Detail keys written to the CSV, and the fields shown for every not-synced item in the report
COLUMN_KEYS = ["style_no", "icode", "colour", "colour_code", "size", "item_group", "hsn", "material"]

BLOCK_FIELDS = [
    ("ICODE", "icode"),
    ("Material", "material"),
    ("Style No", "style_no"),
    ("Item Group", "item_group"),
    ("HSN", "hsn"),
    ("Colour", "colour"),
    ("Colour Code", "colour_code"),
    ("Size", "size"),
]

ERROR_PREFIX = re.compile(r"^\s*Sync Error\s*:\s*", re.IGNORECASE)

# Oracle allows at most 1000 expressions in an IN list
MAX_BIND_VARS = 900

SELECT_SQL = """
    SELECT
        GRPCODE,
        LAST_CHANGED,
        CNAME1,
        CNAME2,
        CNAME3,
        CNAME4,
        CNAME5,
        CNAME6,
        DESC1,
        DESC2,
        DESC3,
        DESC4,
        DESC5,
        DESC6,
        MRP,
        WSP,
        ICODE,
        BARCODE,
        MATERIAL_TYPE
    FROM INVITEM
    WHERE CNAME1 IN ({binds})
    ORDER BY LAST_CHANGED, ICODE
"""


@contextmanager
def cached_helpers():
    """
    A full export of these items is ~9000 rows, mostly variants of the same few
    hundred styles, and finished_item_sync re-checks the same Item Attributes,
    Attribute Values, templates and Price Lists for every row (loading the whole
    Item Attribute document each time). Here those lookups are cached for the
    run; every create / save still goes through the original functions, and a
    value is only cached after the original call succeeded, so failures are still
    raised and reported for every row they affect.
    """

    names = (
        "ensure_item_attribute",
        "ensure_attribute_value",
        "get_attribute_value",
        "ensure_template",
        "ensure_variant",
        "ensure_price_list",
    )
    original = {name: getattr(fis, name) for name in names}

    attributes = set()
    values = set()
    templates = {}
    price_lists = {}
    attribute_rows = {}

    def ensure_item_attribute(attribute_name):
        if attribute_name in attributes:
            return

        original["ensure_item_attribute"](attribute_name)
        attributes.add(attribute_name)

    def ensure_attribute_value(attribute_name, value):
        key = (attribute_name, str(value or "").strip().casefold())

        if not key[1] or key in values:
            return

        original["ensure_attribute_value"](attribute_name, value)

        values.add(key)
        attribute_rows.pop(attribute_name, None)

    def get_attribute_value(attribute_name, value):
        if not value:
            return value

        value = str(value).strip()
        rows = attribute_rows.get(attribute_name)

        if rows is None:
            doc = frappe.get_doc("Item Attribute", attribute_name)
            rows = attribute_rows[attribute_name] = [
                ((d.attribute_value or "").strip(), (d.abbr or "").strip())
                for d in doc.item_attribute_values
            ]

        for attribute_value, abbr in rows:
            if value.casefold() in (attribute_value.casefold(), abbr.casefold()):
                return attribute_value

        return value

    def ensure_template(style_no, item_group):
        if style_no not in templates:
            templates[style_no] = original["ensure_template"](style_no, item_group)

        return templates[style_no]

    def ensure_variant(template, colour, colour_code, size, item_group):
        args = {}

        if colour:
            args["Colour"] = get_attribute_value("Colour", colour)

        if colour_code:
            args["Colour Code"] = get_attribute_value("Colour Code", colour_code)

        if size:
            args["Size"] = get_attribute_value("Size", size)

        variant = fis.get_variant(template, args)

        if variant:
            # The original saves here and again after the definitions / barcodes
            # are set; the second save is enough
            item = frappe.get_doc("Item", variant)
            item.item_group = item_group

            return item

        return original["ensure_variant"](template, colour, colour_code, size, item_group)

    def ensure_price_list(price_list_name, buying=0, selling=1):
        if price_list_name not in price_lists:
            price_lists[price_list_name] = original["ensure_price_list"](
                price_list_name, buying=buying, selling=selling
            )

        return price_lists[price_list_name]

    patched = {
        "ensure_item_attribute": ensure_item_attribute,
        "ensure_attribute_value": ensure_attribute_value,
        "get_attribute_value": get_attribute_value,
        "ensure_template": ensure_template,
        "ensure_variant": ensure_variant,
        "ensure_price_list": ensure_price_list,
    }

    for name, fn in patched.items():
        setattr(fis, name, fn)

    try:
        yield
    finally:
        for name, fn in original.items():
            setattr(fis, name, fn)


def run(
    file_path=None,
    items=None,
    batch_size=50,
    wait_seconds=0,
    report_path=None,
    host=None,
    port=None,
    use_cache=True,
):
    """
    Sync the given items and log why the ones that fail do not sync.

    :param file_path:    text file with one Style No (CNAME1) per line
    :param items:        list of Style Nos (CNAME1) (used with or instead of file_path)
    :param batch_size:   Style Nos per batch; committed after every batch
    :param wait_seconds: pause between batches
    :param report_path:  where to write the text report (default: site private files)
    :param use_cache:    False runs finished_item_sync's own helpers un-cached, exactly as
                         the Sync button does (slower; use it to compare with live)
    """

    codes = get_codes(file_path, items)

    if not codes:
        print("No items to sync.")
        return {}

    batch_size = max(cint(batch_size), 1)
    batches = [codes[i : i + batch_size] for i in range(0, len(codes), batch_size)]

    print(f"{len(codes)} unique items, {len(batches)} batch(es) of up to {batch_size}")

    conn = get_ginesys_connection(host=host, port=port)
    cursor = conn.cursor()

    stats = {"rows": 0, "synced": 0, "failed": 0, "skipped": 0}
    # reason -> {"count": n, "items": OrderedDict[item -> count], "sample": str}
    reasons = OrderedDict()
    found_codes = set()

    started = time.time()

    try:
        item_group_map = get_item_group_map(cursor)
        too_long_items = []

        with cached_helpers() if use_cache else nullcontext():
            for number, batch in enumerate(batches, start=1):
                rows = fetch_rows(cursor, batch)

                for row in rows:
                    stats["rows"] += 1

                    found_codes.add(str(row[2] or "").strip())

                    process_row(row, item_group_map, stats, reasons, too_long_items)

                log_too_long(too_long_items)
                too_long_items = []

                frappe.db.commit()

                elapsed = time.time() - started

                print(
                    f"Batch {number}/{len(batches)}: {len(batch)} items, {len(rows)} rows | "
                    f"total {stats['rows']} rows, synced {stats['synced']}, "
                    f"failed {stats['failed']}, skipped {stats['skipped']} | "
                    f"{elapsed:.0f}s, {stats['rows'] / max(elapsed, 1):.1f} rows/s"
                )

                if wait_seconds and number < len(batches):
                    time.sleep(cint(wait_seconds))

    finally:
        cursor.close()
        conn.close()

    for code in codes:
        if code not in found_codes:
            add_reason(
                reasons,
                "Not found in Ginesys INVITEM (no row with this CNAME1)",
                code,
                detail={"style_no": code},
            )

    return report(codes, stats, reasons, report_path)


def get_codes(file_path, items):
    codes = []

    if file_path:
        with open(file_path, encoding="utf-8-sig") as f:
            for line in f:
                code = ERROR_PREFIX.sub("", line).strip()

                if code:
                    codes.append(code)

    codes.extend(str(x).strip() for x in (items or []) if str(x).strip())

    # Unique, in the order given
    return list(dict.fromkeys(codes))


def fetch_rows(cursor, codes):
    rows = []

    for i in range(0, len(codes), MAX_BIND_VARS):
        chunk = codes[i : i + MAX_BIND_VARS]
        names = [f":c{n}" for n in range(len(chunk))]

        cursor.execute(
            SELECT_SQL.format(binds=", ".join(names)),
            {f"c{n}": code for n, code in enumerate(chunk)},
        )

        rows.extend(cursor.fetchall())

    return rows


def process_row(row, item_group_map, stats, reasons, too_long_items):
    """Same steps as finished_item_sync, with every failure recorded by reason."""

    style_no = colour = colour_code = size = icode = material_type = ""
    item_group = hsn_code = ""
    savepoint = "debug_item_sync_row"
    savepoint_set = False

    def detail():
        # Read when called, so it reflects however far the row got
        return {
            "icode": icode,
            "material": material_type,
            "style_no": style_no,
            "colour": colour,
            "colour_code": colour_code,
            "size": size,
            "item_group": item_group,
            "hsn": hsn_code,
        }

    try:
        (
            grpcode,
            last_changed,
            style_no,
            colour,
            size,
            colour_code,
            vendor_part_no,
            cname6,
            desc1,
            desc2,
            desc3,
            desc4,
            desc5,
            desc6,
            mrp,
            wsp,
            icode,
            barcode,
            material_type,
        ) = row

        cnames = [style_no, colour, size, colour_code, vendor_part_no]

        style_no = str(style_no or "").strip()
        colour = str(colour or "").strip()
        colour_code = str(colour_code or "").strip()
        size = str(size or "").strip()
        vendor_part_no = str(vendor_part_no or "").strip()
        icode = str(icode or "").strip()
        material_type = str(material_type or "").strip().upper()

        is_finished = material_type == "F"
        label = style_no if is_finished else (icode or style_no)

        if is_finished and not style_no:
            stats["skipped"] += 1
            add_reason(reasons, "Skipped: finished item (MATERIAL_TYPE F) without Style No (CNAME1)", icode, detail=detail())
            return

        if not is_finished and not icode:
            stats["skipped"] += 1
            add_reason(reasons, "Skipped: non-finished item without ICODE", style_no, detail=detail())
            return

        group_info = item_group_map.get(grpcode)

        if not group_info:
            stats["failed"] += 1
            add_reason(
                reasons,
                f"Item Group not found in ERPNext for Ginesys GRPCODE {grpcode} (sync Item Groups first)",
                label,
                detail=detail(),
            )
            return

        if is_finished and not group_info.gst_hsn_code:
            stats["failed"] += 1
            add_reason(
                reasons,
                f"Item Group '{group_info.name}' has no HSN/SAC (required for finished items)",
                label,
                detail=detail(),
            )
            return

        item_group = group_info.name
        hsn_code = group_info.gst_hsn_code

        frappe.db.savepoint(savepoint)
        savepoint_set = True

        if is_finished:
            item = fis.sync_finished_item(
                style_no=style_no,
                colour=colour,
                colour_code=colour_code,
                size=size,
                vendor_part_no=vendor_part_no,
                descs=[desc1, desc2, desc3, desc4, desc5, desc6],
                item_group=item_group,
            )
        else:
            item = fis.sync_non_finished_item(
                icode=icode,
                cnames=cnames,
                item_group=item_group,
                hsn_code=hsn_code or DEFAULT_NON_FINISHED_HSN,
            )

        def_fields = DEF_FIELDS
        def_sources = [desc1, desc2, desc3, desc4, desc5, desc6]

        if not is_finished:
            def_fields = DEF_FIELDS + ["custom_def_7"]
            def_sources = def_sources + [cname6]

        def_values, too_long = get_definition_values(def_sources, fields=def_fields)

        item.update(def_values)

        if too_long:
            too_long_items.append(format_too_long(item.name, icode, too_long))

        new_barcodes = []

        if icode:
            new_barcodes.append(icode)

        if barcode:
            new_barcodes.append(str(barcode).strip())

        new_barcodes = list(dict.fromkeys(new_barcodes))
        existing_barcodes = [d.barcode for d in item.barcodes]

        if existing_barcodes != new_barcodes:
            for b in new_barcodes:
                move_barcode_to_item(b, item)

            item.set("barcodes", [])

            for b in new_barcodes:
                item.append("barcodes", {"barcode": b})

        item.save(ignore_permissions=True)

        if is_finished:
            fis.ensure_price_list("MRP")
            fis.ensure_price_list("WSP")

            update_price(item.name, "MRP", mrp)
            update_price(item.name, "WSP", wsp)
        else:
            fis.ensure_price_list(COST_PRICE_LIST, buying=1, selling=0)

            update_price(item.name, COST_PRICE_LIST, mrp if flt(mrp) > 0 else wsp)

        stats["synced"] += 1

        try:
            frappe.db.release_savepoint(savepoint)
        except Exception:
            # A commit inside the sync already released it
            pass

    except Exception as e:
        stats["failed"] += 1

        # Undo this row only, so it can't leave a half-synced item behind
        if savepoint_set:
            try:
                frappe.db.rollback(save_point=savepoint)
            except Exception:
                # A commit inside the sync released the savepoint
                frappe.db.rollback()

        add_reason(
            reasons,
            f"{type(e).__name__}: {clean_message(e)}",
            style_no or icode,
            detail=detail(),
            sample="\n".join(
                [
                    f"ICODE       : {icode}",
                    f"Material    : {material_type}",
                    f"Style No    : {style_no}",
                    f"Item Group  : {item_group}",
                    f"HSN         : {hsn_code}",
                    f"Colour      : {colour}",
                    f"Colour Code : {colour_code}",
                    f"Size        : {size}",
                    "",
                    traceback.format_exc(),
                ]
            ),
        )


def clean_message(exc):
    message = strip_html_tags(str(exc))
    message = " ".join(message.split())

    return message[:300] or "(no message)"


def add_reason(reasons, reason, item, sample=None, detail=None):
    entry = reasons.setdefault(
        reason,
        {"count": 0, "items": OrderedDict(), "sample": sample, "rows": []},
    )

    entry["count"] += 1
    entry["rows"].append(detail or {"style_no": item})
    entry["items"][item] = entry["items"].get(item, 0) + 1

    if sample and not entry["sample"]:
        entry["sample"] = sample


def report(codes, stats, reasons, report_path):
    lines = [
        f"Ginesys item sync debug - {now_datetime()}",
        "",
        f"Items requested : {len(codes)}",
        f"Ginesys rows    : {stats['rows']}",
        f"Synced          : {stats['synced']}",
        f"Failed          : {stats['failed']}",
        f"Skipped         : {stats['skipped']}",
        f"Reasons         : {len(reasons)}",
    ]

    for number, (reason, entry) in enumerate(
        sorted(reasons.items(), key=lambda kv: -kv[1]["count"]), start=1
    ):
        lines += [
            "",
            "=" * 80,
            f"{number}. {reason}",
            f"   {entry['count']} row(s), {len(entry['items'])} item(s)",
        ]

        # Every occurrence: its details, then why it did not sync
        for row in entry["rows"]:
            lines.append("")

            for label, key in BLOCK_FIELDS:
                lines.append(f"   | {label:<12}: {row.get(key) or ''}")

            lines.append(reason)

        if entry["sample"]:
            lines += ["", "   First occurrence (traceback):", indent(entry["sample"])]

    text = "\n".join(lines)

    print("\n" + text)

    if not report_path:
        report_path = frappe.get_site_path(
            "private",
            "files",
            f"ginesys_item_sync_debug_{datetime.now():%Y%m%d_%H%M%S}.txt",
        )

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(text)

    # Every not-synced row, one per line, to filter / sort in a spreadsheet
    csv_path = os.path.splitext(report_path)[0] + ".csv"

    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Reason"] + COLUMN_KEYS)

        for reason, entry in reasons.items():
            for row in entry["rows"]:
                writer.writerow([reason] + [row.get(key) or "" for key in COLUMN_KEYS])

    print(f"Rows written to {csv_path}")

    print(f"\nReport written to {report_path}")

    if reasons:
        frappe.log_error(
            title=f"Ginesys Item Sync Debug - {len(reasons)} Reason(s), {stats['failed'] + stats['skipped']} Not Synced",
            message=text,
        )
        frappe.db.commit()

    return {
        "requested": len(codes),
        "rows": stats["rows"],
        "synced": stats["synced"],
        "failed": stats["failed"],
        "skipped": stats["skipped"],
        "reasons": {r: e["count"] for r, e in reasons.items()},
        "report": report_path,
        "csv": csv_path,
    }


def indent(text, prefix="   | "):
    return "\n".join(prefix + line for line in text.splitlines())
