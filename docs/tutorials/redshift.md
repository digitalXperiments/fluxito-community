# Amazon Redshift setup

Redshift is a credential connector — no OAuth app required. Each Fluxito user goes to `/connect/redshift` and saves their own database credentials directly.

**Time:** ~20 minutes
**You'll need:** admin access to your Redshift cluster (to create a database user) and the ability to modify the cluster's VPC security group (to allow inbound connections from Fluxito's server).

---

## 1. Find your cluster endpoint

**Provisioned cluster:**
1. In the AWS console, go to **Amazon Redshift → Clusters**.
2. Click your cluster name.
3. Under **General information**, find the **Endpoint** — it looks like:
   `my-cluster.abc123xyz.us-east-1.redshift.amazonaws.com:5439`

**Redshift Serverless:**
1. Go to **Serverless dashboard**, click your workgroup.
2. Find the **Endpoint** under **General information**.

Note the hostname (everything before the colon) and port (`5439`) separately.

---

## 2. Create a dedicated database user

Connect to your cluster as an admin user, then run:

```sql
CREATE GROUP fluxito_group;

CREATE USER fluxito_user
    PASSWORD '<choose-a-strong-password>';

ALTER GROUP fluxito_group ADD USER fluxito_user;

-- Grant access to schemas (repeat for each schema Fluxito should read):
GRANT USAGE ON SCHEMA public TO GROUP fluxito_group;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO GROUP fluxito_group;
```

Unlike Snowflake, Redshift has no "GRANT ON FUTURE TABLES" syntax. When new tables are created, re-run `GRANT SELECT ON ALL TABLES IN SCHEMA <name> TO GROUP fluxito_group;`.

---

## 3. Open the security group

Redshift clusters in a VPC are private by default. You need to allow inbound connections from your Fluxito server.

1. In the Redshift console, find the **VPC security group** for your cluster.
2. In the AWS console, go to **EC2 → Security Groups** and open that security group.
3. Click **Edit inbound rules → Add rule**:
   - **Type:** Custom TCP (or Redshift)
   - **Port:** `5439`
   - **Source:** your Fluxito server's public IP in CIDR notation, e.g. `203.0.113.45/32`
4. Click **Save rules**.

---

## 4. Save in Fluxito

1. Go to `/connect` in Fluxito (not `/settings/integrations`).
2. Click **Connect Redshift**.
3. Fill in the form:

| Field | Value |
|---|---|
| **Host** | Cluster endpoint hostname (without the port) |
| **Port** | `5439` |
| **Database** | Database name (e.g. `dev` or `analytics`) |
| **Username** | `fluxito_user` |
| **Password** | The password from Step 2 |

4. Click **Save**.

---

## Troubleshooting

| Error | Fix |
|---|---|
| Connection refused / timeout | The security group is blocking the connection. Verify the inbound rule covers port 5439 for your server's IP. |
| `password authentication failed for user "fluxito_user"` | Wrong password, or the user doesn't exist in this database. Verify and reset with `ALTER USER fluxito_user PASSWORD '<new>';`. |
| `permission denied for schema` | Run `GRANT USAGE ON SCHEMA <name> TO GROUP fluxito_group;`. |
| Serverless workgroup is slow on first connection | Serverless auto-pauses after inactivity. The first connection after a pause takes 30–60 seconds while it warms up — this is normal. |
