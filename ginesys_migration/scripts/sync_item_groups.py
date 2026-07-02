# scripts/sync_item_groups.py

import frappe
from ginesys_migration.utils.oracle import get_ginesys_connection, get_adrk_connection


def create_group(group_name, parent, grpcode, is_group=1):
    if not group_name:
        return None

    group_name = str(group_name).strip()

    doc_name = f"{group_name} ({grpcode})" if grpcode else group_name

    existing = frappe.db.get_value(
        "Item Group",
        {"name": doc_name},
        ["name", "is_group", "parent_item_group"],
        as_dict=True,
    )

    if existing:
        updates = {}

        if is_group and not existing.is_group:
            updates["is_group"] = 1

        if existing.parent_item_group != parent:
            updates["parent_item_group"] = parent

        if updates:
            frappe.db.set_value(
                "Item Group",
                existing.name,
                updates,
                update_modified=False,
            )

        return existing.name

    # display_name = group_name
    # doc_name = f"{group_name} ({grpcode})"

    doc = frappe.get_doc({
        "doctype": "Item Group",
        "item_group_name": doc_name,
        "parent_item_group": parent,
        "is_group": is_group,
    })

    doc.insert(ignore_permissions=True)

    print(f"Created: {parent} -> {doc_name}")

    return doc.name


@frappe.whitelist()
def sync_item_groups(host="192.168.3.3", port=1521):

    ROOT_GROUP = "EC Item Group"

    if not frappe.db.exists("Item Group", ROOT_GROUP):
        frappe.get_doc({
            "doctype": "Item Group",
            "item_group_name": ROOT_GROUP,
            "parent_item_group": "All Item Groups",
            "is_group": 1,
        }).insert(ignore_permissions=True)

    conn = get_ginesys_connection(
        host=host,
        port=int(port),
    )
    cursor = conn.cursor()

    try:

        cursor.execute("""
            SELECT DISTINCT
                GRPCODE,
                TRIM(GRPNAME) AS GRPNAME,
                TRIM(LEV1GRPNAME) AS LEV1GRPNAME,
                TRIM(LEV2GRPNAME) AS LEV2GRPNAME
            FROM INVGRP
            ORDER BY
                LEV1GRPNAME,
                LEV2GRPNAME,
                GRPNAME
        """)

        rows = cursor.fetchall()

        # Build lookup dictionaries

        level1_codes = {}
        level2_codes = {}
        leaf_codes = {}

        for grpcode, grpname, lev1, lev2 in rows:

            grpname = (grpname or "").strip()
            lev1 = (lev1 or "").strip()
            lev2 = (lev2 or "").strip()

            if not lev1:
                level1_codes[grpname] = grpcode

            elif lev1 and not lev2:
                level2_codes[(lev1, grpname)] = grpcode

            else:
                leaf_codes[(lev1, lev2, grpname)] = grpcode

        # Create hierarchy
        for grpcode, grpname, lev1, lev2 in rows:

            grpname = (grpname or "").strip()
            lev1 = (lev1 or "").strip()
            lev2 = (lev2 or "").strip()

            parent = ROOT_GROUP

            # Level 1
            if lev1:

                level1 = create_group(
                    lev1,
                    ROOT_GROUP,
                    level1_codes.get(lev1),
                    is_group=1,
                )

                parent = level1

            # Level 2
            if lev2:

                level2 = create_group(
                    lev2,
                    parent,
                    level2_codes.get((lev1, lev2)),
                    is_group=1,
                )

                parent = level2

            # Leaf
            if lev1 and lev2:
                leaf_code = leaf_codes.get((lev1, lev2, grpname))

            elif lev1:
                leaf_code = level2_codes.get((lev1, grpname))

            else:
                leaf_code = level1_codes.get(grpname)

            create_group(
                grpname,
                parent,
                leaf_code,
                is_group=0,
            )

        frappe.db.commit()

        print("Item Group sync completed")

    except Exception:
        frappe.db.rollback()

        frappe.log_error(
            frappe.get_traceback(),
            "Ginesys Item Group Sync",
        )

        raise

    finally:
        cursor.close()
        conn.close()


@frappe.whitelist()
def test_sync_item_groups():

    conn = get_ginesys_connection()
    cursor = conn.cursor()

    logs = []

    try:

        cursor.execute("""
            SELECT DISTINCT
                GRPCODE,
                TRIM(GRPNAME) AS GRPNAME,
                TRIM(LEV1GRPNAME) AS LEV1GRPNAME,
                TRIM(LEV2GRPNAME) AS LEV2GRPNAME
            FROM INVGRP
            ORDER BY
                LEV1GRPNAME,
                LEV2GRPNAME,
                GRPNAME
        """)

        rows = cursor.fetchall()

        for grpcode, grpname, lev1, lev2 in rows:

            logs.append({
                "grpcode": grpcode,
                "level1": lev1,
                "level2": lev2,
                "group": grpname
            })

        message = []

        for row in logs:

            output = [
                f"GRPCODE : {row['grpcode']}",
                "",
                "Hierarchy:",
                "EC Item Group (is_group = 1)"
            ]

            indent = ""

            if row["level1"]:
                output.append(
                    f"└── {row['level1']} (is_group = 1)"
                )
                indent = "    "

            if row["level2"]:
                output.append(
                    f"{indent}└── {row['level2']} (is_group = 1)"
                )
                indent += "    "

            if row["group"]:
                output.append(
                    f"{indent}└── {row['group']} (is_group = 0)"
                )

            output.append("-" * 80)

            message.append("\n".join(output))

        frappe.log_error(
            title="Ginesys Item Group Dry Run",
            message="\n\n".join(message)
        )

        return {
            "total_groups": len(logs),
            "sample": logs[:20]
        }

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "Ginesys Item Group Dry Run Failed"
        )
        raise

    finally:
        cursor.close()
        conn.close()