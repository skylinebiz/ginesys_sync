import frappe
import oracledb

SETTINGS_DOCTYPE = "Ginesys Sync Setting"

# Used only when neither the caller nor Ginesys Sync Setting gives a value
DEFAULT_HOST = "192.168.3.3"
DEFAULT_PORT = 1521
DEFAULT_USER = "ARCR"
DEFAULT_PASSWORD = "gmpl"
DEFAULT_SERVICE_NAME = "GINESYS"


def get_connection_settings():
    """Server IP, port, username, password and service name from Ginesys Sync Setting."""

    if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        return {}

    settings = frappe.get_single(SETTINGS_DOCTYPE)

    return {
        "host": (settings.server_ip or "").strip(),
        "port": settings.port,
        "user": (settings.username or "").strip(),
        "password": settings.get_password("password", raise_exception=False),
    }  


def get_ginesys_connection(
    host=None,
    port=None,
    user=None,
    password=None,
):
    """
    Connect to the Ginesys Oracle database.

    Each value is taken from the argument if given, else from Ginesys Sync Setting,
    else from the built-in default.
    """

    settings = get_connection_settings()

    host = host or settings.get("host") or DEFAULT_HOST
    port = int(port or settings.get("port") or DEFAULT_PORT)
    user = user or settings.get("user") or DEFAULT_USER
    password = password or settings.get("password") or DEFAULT_PASSWORD

    return oracledb.connect(
        user=user,
        password=password,
        dsn=f"""
        (DESCRIPTION=
            (ADDRESS=
                (PROTOCOL=TCP)
                (HOST={host})
                (PORT={port})
            )
            (CONNECT_DATA=
                (SERVER=DEDICATED)
                (SERVICE_NAME={DEFAULT_SERVICE_NAME})
            )
        )
        """
    )


def get_adrk_connection(
    host="192.168.3.3",
    port=1521,
    user="adrk",
    password="adrk",
):
    return oracledb.connect(
        user=user,
        password=password,
        dsn=f"""
        (DESCRIPTION=
            (ADDRESS=
                (PROTOCOL=TCP)
                (HOST={host})
                (PORT={port})
            )
            (CONNECT_DATA=
                (SERVER=DEDICATED)
                (SERVICE_NAME=ADRK)
            )
        )
        """
    )
