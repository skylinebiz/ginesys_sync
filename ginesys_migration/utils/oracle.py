# utils/oracle.py

import oracledb


def get_ginesys_connection():
    return oracledb.connect(
        user="ARCR",
        password="gmpl",
        dsn="""
        (DESCRIPTION=
            (ADDRESS=
                (PROTOCOL=TCP)
                (HOST=192.168.3.3)
                (PORT=1521)
            )
            (CONNECT_DATA=
                (SERVER=DEDICATED)
                (SERVICE_NAME=GINESYS)
            )
        )
        """
    )


def get_adrk_connection():
    return oracledb.connect(
        user="adrk",
        password="adrk",
        dsn="""
        (DESCRIPTION=
            (ADDRESS=
                (PROTOCOL=TCP)
                (HOST=192.168.3.3)
                (PORT=1521)
            )
            (CONNECT_DATA=
                (SERVER=DEDICATED)
                (SERVICE_NAME=ADRK)
            )
        )
        """
    )