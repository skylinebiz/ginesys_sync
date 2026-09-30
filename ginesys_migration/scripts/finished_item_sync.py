import frappe
from datetime import datetime, timedelta
from frappe.utils import get_datetime, cint, flt
from erpnext.controllers.item_variant import create_variant
from ginesys_migration.utils.oracle import get_ginesys_connection, get_adrk_connection
from erpnext.controllers.item_variant import get_variant
from ginesys_migration.scripts.sync_item_groups import ensure_hsn_code
from ginesys_migration.scripts.item_definition_sync import (
    DEF_FIELDS,
    get_definition_values,
    format_too_long,
    log_too_long,
)

COMMIT_EVERY = 500
COST_PRICE_LIST = "Cost Price"
# HSN for non-finished Items whose Item Group has no HSN/SAC
DEFAULT_NON_FINISHED_HSN = "520829"

@frappe.whitelist()
def sync_finished_item_data(host="192.168.3.3", port=1521, limit=50):

    conn = None
    cursor = None

    try:
        conn = get_ginesys_connection(
            host=host,
            port=int(port),
        )
        cursor = conn.cursor()

        last_item_sync = frappe.db.get_single_value(
            "Sync Setting",
            "last_item_sync",
        )

        if last_item_sync:
            sync_from = get_datetime(last_item_sync)
        else:
            sync_from = datetime(1970, 1, 1)

        print(f"Oracle Item Sync Started from {sync_from}")

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
            WHERE LAST_CHANGED >= :sync_from
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

        item_group_map = get_item_group_map(cursor)
        synced = 0
        failed = 0
        failed_items = []
        too_long_items = []
        missing_item_groups = {}

        # Process Records
        for row in rows:
            style_no = colour = colour_code = size = icode = material_type = ""
            item_group = hsn_code = ""

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

                # Validation

                if is_finished and not style_no:
                    continue

                if not is_finished and not icode:
                    continue

                # Resolve Item Group
                group_info = item_group_map.get(grpcode)

                # Finished items still require an HSN on the Item Group
                if not group_info or (is_finished and not group_info.gst_hsn_code):
                    failed += 1

                    missing_item_groups.setdefault(
                        grpcode,
                        {
                            "group_name": "",
                            "hsn_code": "",
                            "styles": [],
                        },
                    )["styles"].append(style_no if is_finished else icode)

                    continue

                item_group = group_info.name
                hsn_code = group_info.gst_hsn_code

                if is_finished:
                    item = sync_finished_item(
                        style_no=style_no,
                        colour=colour,
                        colour_code=colour_code,
                        size=size,
                        vendor_part_no=vendor_part_no,
                        descs=[desc1, desc2, desc3, desc4, desc5, desc6],
                        item_group=item_group,
                    )
                else:
                    # Non-finished material: plain Item (no variant), keyed by ICODE
                    item = sync_non_finished_item(
                        icode=icode,
                        cnames=cnames,
                        item_group=item_group,
                        hsn_code=hsn_code or DEFAULT_NON_FINISHED_HSN,
                    )

                # Definitions (DESC1..DESC6 -> custom_def_1..custom_def_6)
                # Non-finished: CNAME6 -> custom_def_7
                # Too-long values are left empty so the rest of the Item still saves
                def_fields = DEF_FIELDS
                def_sources = [desc1, desc2, desc3, desc4, desc5, desc6]

                if not is_finished:
                    def_fields = DEF_FIELDS + ["custom_def_7"]
                    def_sources = def_sources + [cname6]

                def_values, too_long = get_definition_values(
                    def_sources,
                    fields=def_fields,
                )

                item.update(def_values)

                if too_long:
                    too_long_items.append(
                        format_too_long(item.name, icode, too_long)
                    )

                new_barcodes = []

                if icode:
                    new_barcodes.append(str(icode).strip())

                if barcode:
                    new_barcodes.append(str(barcode).strip())

                # Remove duplicates while preserving order
                new_barcodes = list(dict.fromkeys(new_barcodes))
                existing_barcodes = [
                    d.barcode
                    for d in item.barcodes
                ]

                if existing_barcodes != new_barcodes:
                    for b in new_barcodes:
                        move_barcode_to_item(b, item)

                    item.set("barcodes", [])

                    for b in new_barcodes:
                        item.append(
                            "barcodes",
                            {
                                "barcode": b,
                            },
                        )

                item.save(ignore_permissions=True)

                if is_finished:
                    # Price Lists

                    ensure_price_list("MRP")
                    ensure_price_list("WSP")

                    # Prices

                    update_price(
                        item.name,
                        "MRP",
                        mrp,
                    )

                    update_price(
                        item.name,
                        "WSP",
                        wsp,
                    )
                else:
                    # Non-finished: MRP (or WSP when MRP is empty/0) goes to
                    # the buying "Cost Price" list
                    ensure_price_list(COST_PRICE_LIST, buying=1, selling=0)

                    update_price(
                        item.name,
                        COST_PRICE_LIST,
                        mrp if flt(mrp) > 0 else wsp,
                    )

                synced += 1

                # Commit

                if synced and synced % COMMIT_EVERY == 0:
                    frappe.db.commit()

                    print(f"{synced} items commited...")
                    # frappe.logger().info(
                    #     f"{synced} items synced..."
                    # )

                if synced and (synced % PRINT_EVERY == 0 or synced == limit):
                    print(f"Synced {synced}/{limit} items...")

            except Exception:

                failed += 1
                print(f"Sync Error : {style_no or icode}")

                failed_items.append(
                    "\n".join([
                        f"ICODE       : {icode}",
                        f"Material    : {material_type}",
                        f"Style No    : {style_no}",
                        f"Item Group  : {item_group}",
                        f"HSN         : {hsn_code}",
                        f"Colour      : {colour}",
                        f"Colour Code : {colour_code}",
                        f"Size        : {size}",
                        "",
                        frappe.get_traceback(),
                        "-" * 80,
                    ])
                )

        # Save Sync Time
        last_changed = get_datetime(rows[-1][1])

        print("Last row timestamp:", last_changed)
        print("Saved in Sync Setting:", last_changed)

        frappe.db.set_single_value(
            "Sync Setting",
            "last_item_sync",
            last_changed,
        )

        if failed_items:
            frappe.log_error(
                title=f"Oracle Item Sync - {failed} Failed Item(s)",
                message="\n\n".join(failed_items),
            )

        log_too_long(too_long_items)

        if missing_item_groups:
            message = []

            for grpcode, data in sorted(missing_item_groups.items()):
                message.append(
                    f"Group Code : {grpcode}\n"
                    f"Group Name : {data['group_name']}\n"
                    f"HSN Code   : {data['hsn_code']}\n"
                    f"Count      : {len(data['styles'])}\n"
                    f"Styles     : {', '.join(data['styles'])}"
                )

            frappe.log_error(
                title=f"Missing Item Groups: ({len(missing_item_groups)})",
                message="\n\n" + ("-" * 80) + "\n\n".join(message),
            )

        frappe.db.commit()
        print(f"\nSync Completed | Success: {synced} | Failed: {failed}")

        frappe.msgprint(
            f"""
            Sync Completed

            Success : {synced}

            Failed : {failed}
            """
        )

    except Exception:

        frappe.db.rollback()

        frappe.log_error(
            title="Oracle Item Sync Failed",
            message=frappe.get_traceback(),
        )

        raise

    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()


def get_item_group_map(cursor):
    cursor.execute("""
        SELECT GRPCODE, GRPNAME
        FROM INVGRP
    """)

    group_map = {}

    for grpcode, grpname in cursor.fetchall():
        item_group_name = f"{str(grpname).strip()} ({grpcode})"

        item_group = frappe.db.get_value(
            "Item Group",
            item_group_name,
            ["name", "gst_hsn_code"],
            as_dict=True,
        )

        # Groups without HSN are kept too: finished items skip them (see caller),
        # non-finished items fall back to DEFAULT_NON_FINISHED_HSN
        if item_group:
            group_map[grpcode] = item_group

    return group_map

# Item Group
def get_item_group(cursor, grpcode):
    cursor.execute(
        """
        SELECT GRPNAME
        FROM INVGRP
        WHERE GRPCODE = :grpcode
        """,
        {"grpcode": grpcode},
    )

    result = cursor.fetchone()

    if not result:
        return None

    grpname = str(result[0]).strip()

    item_group_name = f"{grpname} ({grpcode})"

    item_group = frappe.db.exists(
        "Item Group",
        item_group_name,
    )

    return item_group


# Finished Item (MATERIAL_TYPE = 'F'): Style No template + Colour / Colour Code / Size variant
def sync_finished_item(
    style_no,
    colour,
    colour_code,
    size,
    vendor_part_no,
    descs,
    item_group,
):
    # Ensure Item Attributes

    ensure_item_attribute("Colour")
    ensure_item_attribute("Colour Code")
    ensure_item_attribute("Size")

    # Ensure Attribute Values

    ensure_attribute_value("Colour", colour)
    ensure_attribute_value("Colour Code", colour_code)
    ensure_attribute_value("Size", size)

    # Template

    template = ensure_template(
        style_no=style_no,
        item_group=item_group,
    )

    # Variant

    item = ensure_variant(
        template=template,
        colour=colour,
        colour_code=colour_code,
        size=size,
        item_group=item_group,
    )

    # Update Item

    item.description = "\n".join(
        str(x).strip()
        for x in descs
        if x
    )

    item.custom_vendor_part_number = " ".join(
        str(x).strip()
        for x in [
            vendor_part_no,
            colour,
            size,
        ]
        if x
    )

    item.item_group = item_group

    return item


# Non-finished Item (MATERIAL_TYPE != 'F'): plain Item keyed by ICODE (no variant),
# CNAME1-5 joined as item name
def sync_non_finished_item(icode, cnames, item_group, hsn_code):
    item_name = " ".join(
        str(x).strip()
        for x in cnames
        if x and str(x).strip()
    )

    if frappe.db.exists("Item", icode):
        item = frappe.get_doc("Item", icode)
    else:
        item = frappe.get_doc(
            {
                "doctype": "Item",
                "item_code": icode,
                "stock_uom": "Nos",
            }
        )

    # Item Name is a 140-char Data field
    item.item_name = (item_name or icode)[:140]
    item.item_group = item_group
    item.gst_hsn_code = ensure_hsn_code(hsn_code)

    # Saved (inserted if new) by the caller along with definitions and barcodes
    return item


# Item Attribute
def ensure_item_attribute(attribute_name):

    if frappe.db.exists("Item Attribute", attribute_name):
        return

    doc = frappe.get_doc(
        {
            "doctype": "Item Attribute",
            "attribute_name": attribute_name,
            "numeric_values": 0,
        }
    )

    doc.insert(ignore_permissions=True)


# Attribute Value
def ensure_attribute_value(attribute_name, value):
    if not value:
        return

    value = str(value).strip()

    if not value:
        return

    ensure_item_attribute(attribute_name)

    attribute = frappe.get_doc(
        "Item Attribute",
        attribute_name,
    )

    normalized_value = value.casefold()

    for d in attribute.item_attribute_values:
        if (d.attribute_value or "").strip().casefold() == normalized_value:
            return

    attribute.append(
        "item_attribute_values",
        {
            "attribute_value": value.strip(),
            "abbr": value.strip().upper(),
        },
    )

    attribute.save(ignore_permissions=True)

# Template
def ensure_template(style_no, item_group):
    template = frappe.db.exists(
        "Item",
        style_no,
    )

    if template:
        return template

        # doc = frappe.get_doc(
        #     "Item",
        #     template,
        # )

        # doc.item_group = item_group
        # doc.has_variants = 1
        # doc.variant_based_on = "Item Attribute"

        # existing = {
        #     d.attribute
        #     for d in doc.attributes
        # }

        # for attribute in [
        #     "Colour",
        #     "Colour Code",
        #     "Size",
        # ]:

        #     if attribute not in existing:

        #         doc.append(
        #             "attributes",
        #             {
        #                 "attribute": attribute,
        #             },
        #         )

        # doc.save(ignore_permissions=True)

        # return doc.name

    doc = frappe.get_doc(
        {
            "doctype": "Item",
            "item_code": style_no,
            "item_name": style_no,
            "item_group": item_group,
            "stock_uom": "Nos",
            "has_variants": 1,
            "variant_based_on": "Item Attribute",
            "attributes": [
                {
                    "attribute": "Colour",
                },
                {
                    "attribute": "Colour Code",
                },
                {
                    "attribute": "Size",
                },
            ],
        }
    )

    doc.insert(ignore_permissions=True)

    return doc.name


# Variant
def ensure_variant(
    template,
    colour,
    colour_code,
    size,
    item_group,
):
    args = {}

    if colour:
        args["Colour"] = get_attribute_value("Colour", colour)

    if colour_code:
        args["Colour Code"] = get_attribute_value("Colour Code", colour_code)

    if size:
        args["Size"] = get_attribute_value("Size", size)

    # Existing Variant

    variant = get_variant(
        template,
        args,
    )

    if variant:

        item = frappe.get_doc(
            "Item",
            variant,
        )

        item.item_group = item_group
        item.save(ignore_permissions=True)

        return item

    # Create Variant

    variant = create_variant(
        template,
        args,
    )

    if isinstance(variant, frappe.model.document.Document):
        item = variant
    else:
        item = frappe.get_doc(
            "Item",
            variant,
        )

    item.item_group = item_group

    item.save(ignore_permissions=True)

    return item


# Price List
def ensure_price_list(price_list_name, buying=0, selling=1):

    if frappe.db.exists("Price List", price_list_name):
        return price_list_name

    doc = frappe.get_doc(
        {
            "doctype": "Price List",
            "price_list_name": price_list_name,
            "enabled": 1,
            "selling": selling,
            "buying": buying,
            "currency": frappe.defaults.get_global_default("currency") or "INR",
        }
    )

    doc.insert(ignore_permissions=True)

    return doc.name


# Item Price
def update_price(item_code, price_list, rate):
    if rate in (None, ""):
        return

    try:
        rate = float(rate)
    except Exception:
        return

    if rate < 0:
        return

    price = frappe.db.exists(
        "Item Price",
        {
            "item_code": item_code,
            "price_list": price_list,
        },
    )

    if price:

        doc = frappe.get_doc(
            "Item Price",
            price,
        )

        if doc.price_list_rate != rate:

            doc.price_list_rate = rate
            doc.save(ignore_permissions=True)

        return

    doc = frappe.get_doc(
        {
            "doctype": "Item Price",
            "item_code": item_code,
            "price_list": price_list,
            "price_list_rate": rate,
            "currency": frappe.defaults.get_global_default("currency") or "INR",
        }
    )

    doc.insert(ignore_permissions=True)


def get_attribute_value(attribute_name, value):
    if not value:
        return value

    value = str(value).strip()

    attribute = frappe.get_doc("Item Attribute", attribute_name)

    for d in attribute.item_attribute_values:
        attribute_value = (d.attribute_value or "").strip()
        abbr = (d.abbr or "").strip()

        if (
            attribute_value.casefold() == value.casefold()
            or abbr.casefold() == value.casefold()
        ):
            return attribute_value

    return value


# Logger
def log_sync_error(title, exc=None):
    frappe.log_error(
        title=title,
        message=exc or frappe.get_traceback(),
    )


# Utility
def move_barcode_to_item(barcode, new_item):
    if not barcode:
        return

    barcode = str(barcode).strip()

    old_item_name = frappe.db.get_value(
        "Item Barcode",
        {"barcode": barcode},
        "parent",
    )

    if not old_item_name or old_item_name == new_item.name:
        return

    old_item = frappe.get_doc("Item", old_item_name)

    # Remove barcode from previous Item
    old_item.set(
        "barcodes",
        [
            row for row in old_item.barcodes
            if row.barcode != barcode
        ],
    )

    old_item.save(ignore_permissions=True)

    frappe.db.commit()

    print(
        f"Barcode {barcode} moved: "
        f"{old_item_name} -> {new_item.name}"
    )