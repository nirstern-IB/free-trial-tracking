import os

import snowflake.connector


ACCOUNT = "lalywxa-ja51028"
USER = os.environ.get("SNOWFLAKE_USER", "nir.stern@incredibuild.com")
# Programmatic access token; when set it replaces the browser login
TOKEN = os.environ.get("SNOWFLAKE_TOKEN")


def get_connection(database: str = None, schema: str = None, warehouse: str = "USER_WH", role: str = None):
    params = {
        "account": ACCOUNT,
        "user": USER,
        "authenticator": "externalbrowser",
        "warehouse": warehouse,
    }
    if TOKEN:
        params["authenticator"] = "PROGRAMMATIC_ACCESS_TOKEN"
        params["token"] = TOKEN
    if database:
        params["database"] = database
    if schema:
        params["schema"] = schema
    if role:
        params["role"] = role

    return snowflake.connector.connect(**params)


if __name__ == "__main__":
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_DATABASE()")
    user, role, warehouse, database = cur.fetchone()
    print(f"User:      {user}")
    print(f"Role:      {role}")
    print(f"Warehouse: {warehouse}")
    print(f"Database:  {database}")
    conn.close()
