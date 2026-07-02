# ec/ec/scripts/test_oracle.py

import frappe
from ginesys_migration.utils.oracle import get_ginesys_connection, get_adrk_connection


@frappe.whitelist()
def test_ginesys_connection(host="192.168.3.3", port=1521):
    """
    Test Oracle connection and fetch first 10 records from INVITEM
    """

    conn = None
    cursor = None

    try:
        conn = get_ginesys_connection(
            host=host,
            port=int(port),
        )
        cursor = conn.cursor()

        cursor.execute("""
            SELECT *
            FROM INVGRP
            FETCH FIRST 5 ROWS ONLY
        """)

        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()

        frappe.logger().info(f"Fetched {len(rows)} rows from INVITEM")

        result = []

        for row in rows:
            result.append(dict(zip(columns, row)))

        frappe.msgprint(f"Successfully fetched {len(result)} records")

        return result

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "Ginesys Connection Test Failed"
        )
        raise

    finally:
        if cursor:
            cursor.close()

        if conn:
            conn.close()