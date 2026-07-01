from frappe.custom.doctype.custom_field.custom_field import create_custom_fields as make_custom_fields


def create_custom_fields():

    custom_fields = {
        "Item Group": [
            {
                "fieldname": "identifier_id",
                "label": "Identifier ID",
                "fieldtype": "Data",
                "insert_after": "item_group_name",
                "unique": 1,
                # "read_only": 1,
            }
        ],
        "Customer": [
            {
                "fieldname": "identifier_id",
                "label": "Identifier ID",
                "fieldtype": "Data",
                "insert_after": "customer_name",
                "unique": 1,
                # "read_only": 1,
            }
        ],
        "Supplier": [
            {
                "fieldname": "identifier_id",
                "label": "Identifier ID",
                "fieldtype": "Data",
                "insert_after": "supplier_name",
                "unique": 1,
                # "read_only": 1,
            }
        ]
    }

    make_custom_fields(custom_fields, update=True)