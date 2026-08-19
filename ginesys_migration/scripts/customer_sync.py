import frappe
from frappe.utils import cstr, cint
from frappe.contacts.doctype.contact.contact import get_contact_name
from frappe.contacts.doctype.address.address import get_address_display
import traceback
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


def customer_sync(host="192.168.3.3", port=1521, limit=50):

    conn = None
    cursor = None

    try:
        conn = get_ginesys_connection(
            host=host,
            port=int(port),
        )

        cursor = conn.cursor()

        limit = min(cint(limit), 10000)

        PRINT_EVERY = max(1, limit // 10)

        sql = """
            SELECT *
            FROM (
                SELECT
                    f.SLCODE AS CODE,
                    f.SLNAME AS NAME,
                    f.BADDR AS ADDRESS,
                    f.BCTNAME AS CTNAME,
                    f.BPIN AS PIN,
                    f.BPH1 AS MOBILE,
                    f.BPH1 AS OPH1,
                    f.BPH2 AS OPH2,
                    f.BPH3 AS OPH3,
                    f.BPH4 AS RPH1,
                    f.BFX1 AS FAX,
                    f.BEMAIL AS EMAIL1,
                    f.BEMAIL2 AS EMAIL2,
                    f.BWEBSITE AS WEBSITE,
                    f.BCP AS CONTACT_PERSON,
                    f.PAN,
                    f.CP_GSTIN_NO,
                    f.CP_GSTIN_STATE_CODE
                FROM FINSL f
                JOIN ADMCLS c
                    ON f.CLSCODE = c.CLSCODE
                WHERE UPPER(c.CLSNAME) = 'CUSTOMER'
                ORDER BY f.SLCODE
            )
            WHERE ROWNUM <= :limit
            """

        cursor.execute(sql, {"limit": limit})

        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchall()

        if not rows:
            frappe.msgprint("No customers to sync.")
            return

        print(f"Found {len(rows)} customers.")

        synced = 0
        failed = 0
        failed_customers = []

        for idx, row in enumerate(rows, start=1):

            data = dict(zip(columns, row))

            try:
                frappe.db.savepoint("customer_sync")

                sync_customer(data)

                synced += 1

            except Exception:

                frappe.db.rollback(save_point="customer_sync")

                failed += 1

                # print(f"Sync Error : {data.get('NAME')}")

                failed_customers.append(
                    "\n".join([
                        f"Code      : {data.get('CODE')}",
                        f"Customer  : {data.get('NAME')}",
                        "",
                        frappe.get_traceback(),
                        "-" * 80,
                    ])
                )

            if idx % COMMIT_EVERY == 0:
                frappe.db.commit()
                print(f"{idx} customers committed...")

            if idx % PRINT_EVERY == 0 or idx == len(rows):
                print(
                    f"Processed {idx}/{len(rows)} | "
                    f"Success: {synced} | Failed: {failed}"
                )

        frappe.db.commit()

        if failed_customers:
            frappe.log_error(
                title=f"Oracle Customer Sync - {failed} Failed Customer(s)",
                message="\n\n".join(failed_customers),
            )

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
            title="Oracle Customer Sync Failed",
            message=frappe.get_traceback(),
        )

        raise

    finally:

        if cursor:
            cursor.close()

        if conn:
            conn.close()


def sync_customer(data):
    customer = get_or_create_customer(data)

    create_or_update_contact(customer, data)

    create_or_update_address(customer, data)


def get_or_create_customer(data):
    customer_name = cstr(data.get("NAME")).strip()

    if not customer_name:
        return None

    existing = frappe.db.get_value(
        "Customer",
        {"customer_name": customer_name},
        "name"
    )

    if existing:
        doc = frappe.get_doc("Customer", existing)
    else:
        doc = frappe.new_doc("Customer")
        doc.customer_name = customer_name
        doc.customer_type = "Company"

    if data.get("CP_GSTIN_NO"):
        doc.gstin = cstr(data.get("CP_GSTIN_NO")).strip()

    if cstr(data.get("PAN")).strip():
        doc.pan = cstr(data.get("PAN")).strip()

    doc.save(ignore_permissions=True)

    return doc


def create_or_update_contact(customer, data):
    person = (
        cstr(data.get("CONTACT PERSON")).strip()
        or customer.customer_name
    )

    existing = frappe.db.get_value(
        "Dynamic Link",
        {
            "link_doctype": "Customer",
            "link_name": customer.name,
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
                "link_doctype": "Customer",
                "link_name": customer.name,
            },
        )

    contact.first_name = person

    # Clear existing rows so sync is idempotent
    contact.email_ids = []
    contact.phone_nos = []

    # -----------------------
    # Emails
    # -----------------------
    emails = []
    for field in ("EMAIL1", "EMAIL2"):
        value = cstr(data.get(field)).strip()
        if value and value not in emails:
            emails.append(value)

    for i, email in enumerate(emails):
        contact.append(
            "email_ids",
            {
                "email_id": email,
                "is_primary": 1 if i == 0 else 0,
            },
        )

    # -----------------------
    # Phones
    # -----------------------
    phones = []

    for field in ("MOBILE", "OPH1", "OPH2", "OPH3", "RPH1"):
        value = cstr(data.get(field)).strip()

        if value and value not in phones:
            phones.append(value)

    for phone in phones:
        row = {
            "phone": phone,
        }

        if phone == cstr(data.get("MOBILE")).strip():
            row["is_primary_mobile_no"] = 1
        elif not any(p.is_primary_phone for p in contact.phone_nos):
            row["is_primary_phone"] = 1

        contact.append("phone_nos", row)

    contact.save(ignore_permissions=True)


def create_or_update_address(customer, data):
    existing = frappe.db.get_value(
        "Dynamic Link",
        {
            "link_doctype": "Customer",
            "link_name": customer.name,
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
                "link_doctype": "Customer",
                "link_name": customer.name,
            },
        )

    # state_code = cstr(data.get("CP_GSTIN_STATE_CODE")).zfill(2)
    state_code = cstr(data.get("CP_GSTIN_STATE_CODE")).strip()

    if state_code:
        state_code = state_code.zfill(2)

    address.address_title = customer.customer_name
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