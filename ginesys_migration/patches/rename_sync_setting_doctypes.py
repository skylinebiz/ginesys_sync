import frappe

# Old name -> new name
RENAMED_DOCTYPES = {
    "Ginesys Sync Setting": "Ginesys Settings",
    "Sync Setting": "Ginesys Sync Tracker",
}


def execute():
    """
    Rename the settings doctypes, keeping their saved values.

    Runs before the doctypes are synced, so the new doctype takes over the old one's
    values instead of being created empty next to it.
    """

    for old, new in RENAMED_DOCTYPES.items():
        if not frappe.db.exists("DocType", old) or frappe.db.exists("DocType", new):
            continue

        # Moves the single doctype's values (tabSingles) and updates links to it
        frappe.rename_doc("DocType", old, new, force=True)

        # Passwords are stored against the doctype name and are not moved by rename_doc
        frappe.db.sql(
            "UPDATE `__Auth` SET doctype=%s, name=%s WHERE doctype=%s AND name=%s",
            (new, new, old, old),
        )

        frappe.clear_cache(doctype=old)
