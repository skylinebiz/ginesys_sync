import oracledb


def get_ginesys_connection(
    host="192.168.3.3",
    port=1521,
    user="ARCR",
    password="gmpl",
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
                (SERVICE_NAME=GINESYS)
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