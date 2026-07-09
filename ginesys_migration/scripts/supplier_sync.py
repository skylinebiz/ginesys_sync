
import frappe
from frappe.utils import cstr, cint
from ginesys_migration.utils.oracle import get_ginesys_connection, get_adrk_connection


COMMIT_EVERY = 100

GST_STATE_MAP = {
    "01": "Jammu and Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu",
    "27": "Maharashtra",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
}


def supplier_sync(host="192.168.3.3", port=1521, limit=50):
    conn = None
    cursor = None

    try:
        conn = get_ginesys_connection(host=host, port=int(port))
        cursor = conn.cursor()

        limit = min(cint(limit), 10000)

        sql = """
        SELECT *
        FROM (
            SELECT
                CODE,
                NAME,
                ADDRESS,
                CTNAME,
                PIN,
                OPH1,
                OPH2,
                OPH3,
                RPH1,
                MOBILE,
                FAX,
                EMAIL1,
                EMAIL2,
                WEBSITE,
                CONTACT_PERSON,
                CP_GSTIN_NO,
                CP_GSTIN_STATE_CODE
            FROM ADMSITE
            ORDER BY CODE
        )
        WHERE ROWNUM <= :limit
        """

        cursor.execute(sql, {"limit": limit})

        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()

        if not rows:
            frappe.msgprint("No suppliers to sync.")
            return

        print(f"Found {len(rows)} suppliers.")

        synced = 0
        failed = 0
        failed_suppliers = []

        for idx, row in enumerate(rows, start=1):

            data = dict(zip(columns, row))

            try:
                frappe.db.savepoint("supplier_sync")

                sync_supplier(data)

                synced += 1

            except Exception:

                frappe.db.rollback(save_point="supplier_sync")

                failed += 1

                failed_suppliers.append(
                    "\n".join([
                        f"Code      : {data.get('CODE')}",
                        f"Supplier  : {data.get('NAME')}",
                        "",
                        frappe.get_traceback(),
                        "-" * 80,
                    ])
                )

            if idx % COMMIT_EVERY == 0:
                frappe.db.commit()

            if idx % 10 == 0 or idx == len(rows):
                print(
                    f"Processed {idx}/{len(rows)} | "
                    f"Success: {synced} | Failed: {failed}"
                )

        frappe.db.commit()

        if failed_suppliers:
            frappe.log_error(
                title=f"Oracle Supplier Sync - {failed} Failed Supplier(s)",
                message="\n\n".join(failed_suppliers),
            )

        print(f"\nSync Completed | Success: {synced} | Failed: {failed}")

    except Exception:
        frappe.db.rollback()

        frappe.log_error(
            title="Oracle Supplier Sync Failed",
            message=frappe.get_traceback(),
        )

        raise

    finally:
        if cursor:
            cursor.close()

        if conn:
            conn.close()



def sync_supplier(data):
    supplier = get_or_create_supplier(data)

    create_or_update_supplier_contact(supplier, data)

    create_or_update_supplier_address(supplier, data)


# Create Supplier
def get_or_create_supplier(data):
    supplier_name = cstr(data.get("NAME")).strip()

    if not supplier_name:
        return None

    existing = frappe.db.get_value(
        "Supplier",
        {"supplier_name": supplier_name},
        "name",
    )

    if existing:
        doc = frappe.get_doc("Supplier", existing)
    else:
        doc = frappe.new_doc("Supplier")
        doc.supplier_name = supplier_name
        doc.supplier_type = "Company"

    if data.get("CP_GSTIN_NO"):
        doc.gstin = cstr(data.get("CP_GSTIN_NO")).strip()

    doc.save(ignore_permissions=True)

    return doc


# Supplier Contact
def create_or_update_supplier_contact(supplier, data):
    person = (
        cstr(data.get("CONTACT_PERSON")).strip()
        or supplier.supplier_name
    )

    existing = frappe.db.get_value(
        "Dynamic Link",
        {
            "link_doctype": "Supplier",
            "link_name": supplier.name,
            "parenttype": "Contact",
        },
        "parent",
    )

    if existing:
        contact = frappe.get_doc("Contact", existing)
    else:
        contact = frappe.new_doc("Contact")
        contact.append(
            "links",
            {
                "link_doctype": "Supplier",
                "link_name": supplier.name,
            },
        )

    contact.first_name = person

    contact.email_ids = []
    contact.phone_nos = []

    emails = []

    for field in ("EMAIL1", "EMAIL2"):
        email = cstr(data.get(field)).strip()
        if email and email not in emails:
            emails.append(email)

    for i, email in enumerate(emails):
        contact.append(
            "email_ids",
            {
                "email_id": email,
                "is_primary": i == 0,
            },
        )

    phones = []

    for field in ("MOBILE", "OPH1", "OPH2", "OPH3", "RPH1"):
        phone = cstr(data.get(field)).strip()

        if phone and phone not in phones:
            phones.append(phone)

    for phone in phones:
        row = {"phone": phone}

        if phone == cstr(data.get("MOBILE")).strip():
            row["is_primary_mobile_no"] = 1
        elif not any(p.is_primary_phone for p in contact.phone_nos):
            row["is_primary_phone"] = 1

        contact.append("phone_nos", row)

    contact.save(ignore_permissions=True)


# Supplier Address
def create_or_update_supplier_address(supplier, data):
    existing = frappe.db.get_value(
        "Dynamic Link",
        {
            "link_doctype": "Supplier",
            "link_name": supplier.name,
            "parenttype": "Address",
        },
        "parent",
    )

    if existing:
        address = frappe.get_doc("Address", existing)
    else:
        address = frappe.new_doc("Address")
        address.append(
            "links",
            {
                "link_doctype": "Supplier",
                "link_name": supplier.name,
            },
        )

    # state_code = cstr(data.get("CP_GSTIN_STATE_CODE")).zfill(2)
    state_code = cstr(data.get("CP_GSTIN_STATE_CODE")).strip()

    if state_code:
        state_code = state_code.zfill(2)


    address.address_title = supplier.supplier_name
    address.address_type = "Billing"

    address.address_line1 = cstr(data.get("ADDRESS")).strip()
    address.city = cstr(data.get("CTNAME")).strip()
    address.state = GST_STATE_MAP.get(state_code, "Rajasthan")
    # address.state = GST_STATE_MAP.get(state_code)
    address.pincode = cstr(data.get("PIN")).strip()

    if data.get("CP_GSTIN_NO"):
        address.gstin = cstr(data.get("CP_GSTIN_NO")).strip()

    address.phone = (
        cstr(data.get("OPH1")).strip()
        or cstr(data.get("MOBILE")).strip()
    )

    address.email_id = (
        cstr(data.get("EMAIL1")).strip()
        or cstr(data.get("EMAIL2")).strip()
    )

    address.save(ignore_permissions=True)

