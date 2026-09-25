# Snowflake setup

Snowflake is a credential connector — no OAuth app to register at the install level. Each Fluxito user goes to `/connect/snowflake` and saves their own database credentials directly.

**Time:** ~15 minutes
**You'll need:** SYSADMIN or SECURITYADMIN access to your Snowflake account to create a dedicated user and role.

---

## 1. Find your Snowflake account identifier

Your account identifier is everything before `.snowflakecomputing.com` in the URL you use to log in.

- **Modern format:** `mycompany-analytics` (org name + account name)
- **Legacy format:** `xy12345.us-east-1` (account locator + region)

In Fluxito's connection form, enter the identifier **without** the `.snowflakecomputing.com` suffix.

---

## 2. Create a dedicated user and role

Run this SQL in a Snowflake worksheet as a SYSADMIN or SECURITYADMIN user. Replace the angle-bracket placeholders:

```sql
CREATE ROLE IF NOT EXISTS FLUXITO_ROLE;

CREATE USER IF NOT EXISTS FLUXITO_USER
    PASSWORD = '<choose-a-strong-password>'
    DEFAULT_ROLE = FLUXITO_ROLE
    DEFAULT_WAREHOUSE = '<your-warehouse-name>'
    DEFAULT_NAMESPACE = '<your-database>.<your-schema>'
    MUST_CHANGE_PASSWORD = FALSE;

GRANT ROLE FLUXITO_ROLE TO USER FLUXITO_USER;
```

---

## 3. Grant warehouse and database access

```sql
GRANT USAGE ON WAREHOUSE <your-warehouse-name> TO ROLE FLUXITO_ROLE;
GRANT USAGE ON DATABASE <your-database> TO ROLE FLUXITO_ROLE;
GRANT USAGE ON ALL SCHEMAS IN DATABASE <your-database> TO ROLE FLUXITO_ROLE;
GRANT SELECT ON ALL TABLES IN DATABASE <your-database> TO ROLE FLUXITO_ROLE;
GRANT SELECT ON ALL VIEWS IN DATABASE <your-database> TO ROLE FLUXITO_ROLE;

-- Grant access to tables created in the future:
GRANT SELECT ON FUTURE TABLES IN SCHEMA <your-database>.<your-schema> TO ROLE FLUXITO_ROLE;
GRANT SELECT ON FUTURE VIEWS IN SCHEMA <your-database>.<your-schema> TO ROLE FLUXITO_ROLE;
```

For access to multiple databases, repeat the `GRANT USAGE ON DATABASE` and `GRANT SELECT ON ALL TABLES` statements for each additional database.

---

## 4. Save in Fluxito

1. Go to `/connect` in Fluxito (not `/settings/integrations`).
2. Click **Connect Snowflake**.
3. Fill in the form:

| Field | Value |
|---|---|
| **Account** | Your account identifier (without `.snowflakecomputing.com`) |
| **Username** | `FLUXITO_USER` |
| **Password** | The password from Step 2 |
| **Warehouse** | Your virtual warehouse name |
| **Database** | Your default database |
| **Schema** | Your default schema (optional) |
| **Role** | `FLUXITO_ROLE` |

4. Click **Save** (or **Test Connection** if available, then Save).

---

## Troubleshooting

| Error | Fix |
|---|---|
| `Incorrect username or password` | Verify the credentials. Reset with `ALTER USER FLUXITO_USER SET PASSWORD = '<new>';` in Snowflake. |
| `Object '<database>' does not exist` | Run `GRANT USAGE ON DATABASE <name> TO ROLE FLUXITO_ROLE;` with the correct name. |
| `Insufficient privileges on warehouse` | Run `GRANT USAGE ON WAREHOUSE <name> TO ROLE FLUXITO_ROLE;`. |
| Connection timeout | Check for a Snowflake network policy blocking your server's IP. Go to **Admin → Security → Network Policy** and add your server's IP to the allowed ranges. |
