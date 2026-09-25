# Apple Ads

Connect Apple Ads so Fluxito can read App Store campaign accounts, campaigns, ad groups, and performance reports.

## Prerequisites

- Apple Ads account access with API permissions.
- An Apple Ads API client ID.
- A signed Apple Ads client secret JWT for OAuth 2 client credentials.

Apple Ads uses OAuth 2 client credentials. There is no redirect URI to register for Fluxito; configure the client ID and client secret in **Admin → OAuth Apps**, then connect Apple Ads from the project connections page.

## Configure credentials

1. In Apple Ads, open **Account Settings > API**.
2. Create or select an API client.
3. Generate the client secret JWT using the private key, key ID, team ID, and client ID from Apple Ads.
4. In Fluxito, go to **Admin → OAuth Apps → Apple Search Ads** (or **Project settings → OAuth apps** for one project).
5. Paste the Apple Ads client ID and the generated client secret JWT.
6. Save, then use **Connect > Apple Ads** to exchange the credentials for an access token.

## What Fluxito can do

- List Apple Ads organizations available to the API credentials.
- Read campaign performance by organization ID.
- Read ad group performance by campaign.
- Update campaign status when write access is granted.

Apple's Campaign Management API requires `X-AP-Context: orgId=<orgId>` for organization-scoped calls. Fluxito uses the organization ID returned from the ACL endpoint as the `account_id`.
