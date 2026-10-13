import frappe

ATTRIBUTE = "Size"
ABBREVIATIONS = ("XS", "S", "M", "L", "XL")


def execute():
    """
    Item Attribute "Size": use the abbreviation as the attribute value
    (Extra Small -> XS, Small -> S, Medium -> M, Large -> L, Extra Large -> XL).

    """

    if not frappe.db.exists("Item Attribute", ATTRIBUTE):
        return

    rows = frappe.get_all(
        "Item Attribute Value",
        filters={"parent": ATTRIBUTE, "parenttype": "Item Attribute"},
        fields=["name", "attribute_value", "abbr"],
    )

    existing = {(row.attribute_value or "").strip().casefold() for row in rows}

    for row in rows:
        abbr = (row.abbr or "").strip().upper()
        old_value = row.attribute_value

        if abbr not in ABBREVIATIONS or old_value == abbr:
            continue

        # The abbreviation is already a value of its own: merging would create a duplicate
        if abbr.casefold() in existing:
            print(f"Size '{old_value}' not renamed: a value '{abbr}' already exists")
            continue

        frappe.db.set_value(
            "Item Attribute Value",
            row.name,
            {"attribute_value": abbr, "abbr": abbr},
            update_modified=False,
        )

        frappe.db.sql(
            """
            UPDATE `tabItem Variant Attribute`
            SET attribute_value = %(new)s
            WHERE attribute = %(attribute)s
            AND attribute_value = %(old)s
            """,
            {"new": abbr, "old": old_value, "attribute": ATTRIBUTE},
        )

        existing.discard(old_value.strip().casefold())
        existing.add(abbr.casefold())

    frappe.clear_cache(doctype="Item Attribute")
    frappe.clear_cache(doctype="Item")
