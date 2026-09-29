import frappe
from datetime import datetime
from frappe.utils import get_datetime, cint
from ginesys_migration.utils.oracle import get_ginesys_connection

COMMIT_EVERY = 500

# Ginesys DESC1..DESC6 -> Item custom_def_1..custom_def_6 (ec app "Definitions" section)
DEF_FIELDS = [f"custom_def_{i}" for i in range(1, 7)]


@frappe.whitelist()
def sync_item_definitions(host="192.168.3.3", port=1521, limit=500):
    """
    Copy DESC1-DESC6 of Ginesys finished items (INVITEM) into custom_def_1-6
    of the matching ERPNext Item variant.

    Matched on Style No (CNAME1) template + Colour (CNAME2), Colour Code (CNAME4)
    and Size (CNAME3) attributes, falling back to item code
    "STYLE-COLOUR-COLOURCODE-SIZE". Unmatched rows are skipped and logged.
    Progress is tracked in Sync Setting.last_item_desc_sync.
    """

    conn = None
    cursor = None

    try:
        conn = get_ginesys_connection(
            host=host,
            port=int(port),
        )
        cursor = conn.cursor()

        last_item_desc_sync = frappe.db.get_single_value(
            "Sync Setting",
            "last_item_desc_sync",
        )

        if last_item_desc_sync:
            sync_from = get_datetime(last_item_desc_sync)
        else:
            sync_from = datetime(1970, 1, 1)

        print(f"Oracle Item Description Sync Started from {sync_from}")

        limit = cint(limit)

        if limit <= 0:
            frappe.throw("Count must be greater than 0.")

        limit = min(limit, 10000)
        PRINT_EVERY = max(1, limit // 10)

        # Fetch Records

        sql = """
        SELECT *
        FROM (
            SELECT
                LAST_CHANGED,
                ICODE,
                CNAME1,
                CNAME2,
                CNAME3,
                CNAME4,
                DESC1,
                DESC2,
                DESC3,
                DESC4,
                DESC5,
                DESC6
            FROM INVITEM
            WHERE LAST_CHANGED >= :sync_from
            AND MATERIAL_TYPE = 'F'
            ORDER BY LAST_CHANGED
        )
        WHERE ROWNUM <= :limit
        """

        cursor.execute(
            sql,
            {
                "sync_from": sync_from,
                "limit": limit,
            },
        )

        rows = cursor.fetchall()

        if not rows:
            frappe.msgprint("No records to sync.")
            return

        print(f"{len(rows)} records fetched from Oracle.")

        variant_map = get_variant_map()

        updated = 0
        unchanged = 0
        not_found = []
        failed_items = []
        too_long_items = []

        # Process Records

        for i, row in enumerate(rows, start=1):
            (
                _last_changed,
                icode,
                style_no,
                colour,
                size,
                colour_code,
                *descs,
            ) = row

            style_no = clean(style_no)
            colour = clean(colour)
            size = clean(size)
            colour_code = clean(colour_code)

            if not style_no:
                continue

            try:
                item_code = find_item(
                    variant_map,
                    style_no,
                    colour,
                    colour_code,
                    size,
                )

                if not item_code:
                    not_found.append(
                        f"{icode}: {style_no} - {colour} - {colour_code} - {size}"
                    )
                    continue

                new_values, too_long = get_definition_values(descs)

                if too_long:
                    too_long_items.append(
                        format_too_long(item_code, icode, too_long)
                    )

                current = frappe.db.get_value(
                    "Item",
                    item_code,
                    DEF_FIELDS,
                    as_dict=True,
                )

                changes = {
                    field: value
                    for field, value in new_values.items()
                    if (current.get(field) or "") != value
                }

                if not changes:
                    unchanged += 1
                else:
                    frappe.db.set_value("Item", item_code, changes)
                    updated += 1

                    if updated % COMMIT_EVERY == 0:
                        frappe.db.commit()
                        print(f"{updated} items commited...")

            except Exception:
                print(f"Sync Error : {style_no}")

                failed_items.append(
                    "\n".join([
                        f"ICODE       : {icode}",
                        f"Style No    : {style_no}",
                        f"Colour      : {colour}",
                        f"Colour Code : {colour_code}",
                        f"Size        : {size}",
                        "",
                        frappe.get_traceback(),
                        "-" * 80,
                    ])
                )

            if i % PRINT_EVERY == 0 or i == len(rows):
                print(f"Processed {i}/{len(rows)} rows...")

        # Save Sync Time
        last_changed = get_datetime(rows[-1][0])

        print("Saved in Sync Setting:", last_changed)

        frappe.db.set_single_value(
            "Sync Setting",
            "last_item_desc_sync",
            last_changed,
        )

        if not_found:
            frappe.log_error(
                title=f"Oracle Item Description Sync - {len(not_found)} Item(s) Not Found",
                message="\n".join(not_found),
            )

        if failed_items:
            frappe.log_error(
                title=f"Oracle Item Description Sync - {len(failed_items)} Failed Item(s)",
                message="\n\n".join(failed_items),
            )

        log_too_long(too_long_items)

        frappe.db.commit()

        summary = (
            f"Updated: {updated} | Unchanged: {unchanged} | "
            f"Not Found: {len(not_found)} | Failed: {len(failed_items)} | "
            f"Too Long: {len(too_long_items)}"
        )

        print(f"\nItem Description Sync Completed | {summary}")

        frappe.msgprint(f"Item Description Sync Completed<br>{summary}")

    except Exception:

        frappe.db.rollback()

        frappe.log_error(
            title="Oracle Item Description Sync Failed",
            message=frappe.get_traceback(),
        )

        raise

    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()


def get_variant_map():
    """{(template, colour, colour_code, size): item_code}, keys casefolded."""

    rows = frappe.db.sql(
        """
        SELECT
            item.name,
            item.variant_of,
            attr.attribute,
            attr.attribute_value
        FROM `tabItem` item
        INNER JOIN `tabItem Variant Attribute` attr
            ON attr.parent = item.name
            AND attr.parenttype = 'Item'
        WHERE item.variant_of IS NOT NULL
        AND item.variant_of != ''
        AND attr.attribute IN ('Colour', 'Colour Code', 'Size')
        """,
        as_dict=True,
    )

    variants = {}

    for row in rows:
        variant = variants.setdefault(row.name, {"template": row.variant_of})
        variant[row.attribute] = row.attribute_value

    return {
        make_key(
            v["template"],
            v.get("Colour"),
            v.get("Colour Code"),
            v.get("Size"),
        ): item_code
        for item_code, v in variants.items()
    }


def find_item(variant_map, style_no, colour, colour_code, size):
    item_code = variant_map.get(
        make_key(style_no, colour, colour_code, size)
    )

    if item_code:
        return item_code

    # Fallback: item code built as Style No - Colour - Colour Code - Size
    item_code = "-".join(
        x for x in [style_no, colour, colour_code, size] if x
    )

    return frappe.db.exists("Item", item_code)


def make_key(template, colour, colour_code, size):
    return tuple(
        clean(x).casefold()
        for x in [template, colour, colour_code, size]
    )


def clean(value):
    return str(value or "").strip()


def get_definition_values(descs):
    """
    Map DESC1..DESC6 to custom_def_1..custom_def_6.

    Values longer than the field allows are left empty so the rest of the Item
    still saves. Returns (values, too_long) where too_long is
    [(field, label, max_length, value)].
    """

    meta = frappe.get_meta("Item")

    values = {}
    too_long = []

    for field, desc in zip(DEF_FIELDS, descs):
        value = clean(desc)
        df = meta.get_field(field)
        max_length = get_max_length(df)

        if max_length and len(value) > max_length:
            too_long.append((field, df.label if df else field, max_length, value))
            value = ""

        values[field] = value

    return values, too_long


def get_max_length(df):
    """Same limit Frappe enforces on save (BaseDocument._validate_length); 0 = no limit."""

    if not df:
        return 0

    column_type, default_length = (frappe.db.type_map.get(df.fieldtype) or (None, None))[:2]

    if column_type != "varchar":
        return 0

    return cint(df.length) or cint(default_length)


def format_too_long(item_code, icode, too_long):
    return "\n".join(
        [f"Item : {item_code} (ICODE {icode})"]
        + [
            f"  {label} ({field}) - {len(value)}/{max_length} chars: {value}"
            for field, label, max_length, value in too_long
        ]
    )


def log_too_long(too_long_items):
    if not too_long_items:
        return

    frappe.log_error(
        title=f"Item Definition Too Long - {len(too_long_items)} Item(s)",
        message="Left empty because value exceeds field length:\n\n"
        + "\n\n".join(too_long_items),
    )
