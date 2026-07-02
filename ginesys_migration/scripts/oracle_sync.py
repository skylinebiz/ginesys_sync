import frappe
from datetime import datetime, timedelta
from frappe.utils import get_datetime
from erpnext.controllers.item_variant import create_variant
from ginesys_migration.utils.oracle import get_ginesys_connection, get_adrk_connection
from erpnext.controllers.item_variant import get_variant

BATCH_SIZE = 10000
COMMIT_EVERY = 500


@frappe.whitelist()
def sync_item_data(host="192.168.3.3", port=1521):

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

        frappe.logger().info(
            f"Oracle Item Sync Started from {sync_from}"
        )

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
                DESC1,
                DESC2,
                DESC3,
                DESC4,
                DESC5,
                DESC6,
                MRP,
                WSP,
                ICODE,
                BARCODE
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
                "limit": BATCH_SIZE,
            },
        )

        rows = cursor.fetchall()

        if not rows:
            frappe.msgprint("No records to sync.")
            return

        frappe.logger().info(
            f"{len(rows)} records fetched from Oracle."
        )

        synced = 0
        failed = 0

        
        # Process Records

        for row in rows:

            try:

                (
                    grpcode,
                    last_changed,
                    style_no,
                    colour,
                    size,
                    colour_code,
                    vendor_part_no,
                    desc1,
                    desc2,
                    desc3,
                    desc4,
                    desc5,
                    desc6,
                    mrp,
                    wsp,
                    icode,
                    barcode
                ) = row

                # Validation

                if not style_no:
                    continue

                style_no = str(style_no).strip()

                colour = str(colour or "").strip()
                colour_code = str(colour_code or "").strip()
                size = str(size or "").strip()

                vendor_part_no = str(vendor_part_no or "").strip()

                # Description

                description = "\n".join(
                    str(x).strip()
                    for x in [
                        desc1,
                        desc2,
                        desc3,
                        desc4,
                        desc5,
                        desc6,
                    ]
                    if x
                )

                supplier_part_no = " ".join(
                    str(x).strip()
                    for x in [
                        vendor_part_no,
                        colour,
                        size,
                    ]
                    if x
                )

                # Resolve Item Group

                item_group = get_item_group(
                    cursor,
                    grpcode,
                )

                if not item_group:

                    failed += 1

                    frappe.log_error(
                        title=f"Missing Item Group ({grpcode})",
                        message=f"Style : {style_no}",
                    )

                    continue

                # Ensure Item Attributes

                ensure_item_attribute("Colour")
                ensure_item_attribute("Colour Code")
                ensure_item_attribute("Size")

                # Ensure Attribute Values

                ensure_attribute_value(
                    "Colour",
                    colour,
                )

                ensure_attribute_value(
                    "Colour Code",
                    colour_code,
                )

                ensure_attribute_value(
                    "Size",
                    size,
                )

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

                item.description = description

                item.custom_vendor_part_number = supplier_part_no
                item.item_group = item_group

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

                    item.set("barcodes", [])

                    for b in new_barcodes:
                        item.append(
                            "barcodes",
                            {
                                "barcode": b,
                            },
                        )

                item.save(ignore_permissions=True)

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

                synced += 1

                # Commit

                if synced and synced % COMMIT_EVERY == 0:
                    frappe.db.commit()

                    frappe.logger().info(
                        f"{synced} items synced..."
                    )

            except Exception:

                failed += 1

                frappe.log_error(
                    title=f"Oracle Sync : {style_no}",
                    message=frappe.get_traceback(),
                )

        # Save Sync Time

        last_changed = get_datetime(rows[-1][1]) - timedelta(hours=1)

        frappe.db.set_single_value(
            "Sync Setting",
            "last_item_sync",
            last_changed,
        )

        frappe.db.commit()

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

    exists = False

    for d in attribute.item_attribute_values:

        if d.attribute_value == value:
            exists = True
            break

    if exists:
        return

    attribute.append(
        "item_attribute_values",
        {
            "attribute_value": value,
            "abbr": value,
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
        return

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
        args["Colour"] = colour

    if colour_code:
        args["Colour Code"] = colour_code

    if size:
        args["Size"] = size

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
def ensure_price_list(price_list_name):

    if frappe.db.exists("Price List", price_list_name):
        return price_list_name

    doc = frappe.get_doc(
        {
            "doctype": "Price List",
            "price_list_name": price_list_name,
            "enabled": 1,
            "selling": 1,
            "buying": 0,
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


# Utility

# def safe_str(value):
#     if value is None:
#         return ""

#     return str(value).strip()


# def build_description(*values):
#     return "\n".join(
#         safe_str(v)
#         for v in values
#         if safe_str(v)
#     )


# def build_vendor_part_no(vendor_part, colour, size):
#     return " ".join(
#         safe_str(v)
#         for v in [
#             vendor_part,
#             colour,
#             size,
#         ]
#         if safe_str(v)
#     )


# Logger
def log_sync_error(title, exc=None):
    frappe.log_error(
        title=title,
        message=exc or frappe.get_traceback(),
    )