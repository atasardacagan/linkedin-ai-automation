# LinkedIn member authorization

This release is designed for one operator publishing to their own member profile. LinkedIn credentials are never requested through Telegram and LinkedIn username/password automation is not implemented.

## Required LinkedIn products and permissions

| Capability | Product or approval | OAuth scope | Required |
|---|---|---|---|
| Publish a member post | Share on LinkedIn | `w_member_social` | Yes |
| Resolve the member through OIDC helper | Sign In with LinkedIn using OpenID Connect | `openid profile` | Recommended |
| Collect personal post metrics | Approved member analytics access | `r_member_postAnalytics` | Optional |

Do not request organization, advertising, messaging, or read-social permissions for this system. Keep `LINKEDIN_ANALYTICS_ENABLED=false` unless Token Inspector shows `r_member_postAnalytics` and LinkedIn has approved that access.

## One-time application setup

1. Open the [LinkedIn Developer Portal](https://www.linkedin.com/developers/apps) and create an application.
2. Complete the application's verified association with a LinkedIn Page when the portal requests it. This does not make the system publish as that Page; the current implementation publishes only as the authenticated member.
3. Open the application's **Products** tab and add **Share on LinkedIn**.
4. Also add **Sign In with LinkedIn using OpenID Connect** if available. This enables the `openid profile` scopes and the repository's member-ID verification helper.
5. On the **Auth** tab, verify that `w_member_social` is listed. Verify `openid` and `profile` if the OIDC product was added.
6. Request member post analytics only if needed. It is a separate approval and must not block basic publishing.

## Obtain the member token

LinkedIn documents two appropriate member-token mechanisms:

- its three-legged OAuth authorization-code flow for an application-operated production authorization journey; and
- the Developer Portal Token Generator for manual testing/bootstrap.

This single-operator repository deliberately accepts a manually authorized member token as an environment secret. It does not store a LinkedIn client secret or expose a public OAuth callback. That keeps the runtime attack surface small, but it means the operator must repeat member authorization when the token expires. A multi-user product must add a complete server-side authorization-code flow, encrypted token vault, tenant isolation, consent lifecycle, and privacy controls before use.

For this deployment:

1. In the Developer Portal navigation, open **Docs and Tools → Token Generator**.
2. Select the application.
3. Select the member OAuth flow and `w_member_social`. Also select `openid profile` when the OIDC product is present. Select `r_member_postAnalytics` only when it is explicitly available and approved.
4. Continue to LinkedIn, sign in as the profile owner, review the displayed permissions, and click **Allow**.
5. On **Token Details**, confirm the token is active, confirm `w_member_social` is present, and copy the token into `LINKEDIN_ACCESS_TOKEN` in the server's `.env`.
6. Open **Token Inspector** on the same Developer Portal tools page. Inspect the token and copy its `expires_at` time into `LINKEDIN_ACCESS_TOKEN_EXPIRES_AT` as an ISO-8601 UTC value such as `2026-11-05T12:00:00Z`. Do not guess the date.

The Token Generator is documented as a testing tool. For a controlled personal deployment, this project treats it as a manual bootstrap/reauthorization step; it does not imply that LinkedIn grants refresh-token or broader production program access.

## Resolve and verify the author URN

With the token saved in `.env`, run:

```bash
make verify-linkedin
```

The helper first tries LinkedIn's current-member profile endpoint and then the OpenID Connect `/v2/userinfo` endpoint. It prints only:

```text
LINKEDIN_AUTHOR_URN=urn:li:person:<application-scoped-member-id>
```

Copy that complete line into `.env`. The helper never prints the token.

If both identity endpoints are unavailable, use Developer Portal Token Inspector to identify the authenticated member or obtain the application-scoped current-member `id` through the Profile API access available to your app. Construct `urn:li:person:<id>` exactly. Do not use a public profile slug, email address, numeric guess, or another application's member ID; LinkedIn member IDs are application-scoped.

## Safe activation

1. Leave `DRY_RUN=true`.
2. Run `make preflight`. It must finish without a configuration error.
3. Start the service and request a draft in Telegram.
4. Review and approve the exact content, then choose **HEMEN PAYLAŞ**. Confirm the bot reports a dry run and that no LinkedIn network request or post occurred.
5. Set `DRY_RUN=false` and restart only when an actual public test post is acceptable.
6. Generate, inspect, approve, and explicitly publish one low-risk test post.
7. Confirm the returned link opens the expected post on the expected profile.
8. Leave analytics disabled until its separate permission has been verified.

`DRY_RUN=false` never removes the approval gate. It only enables the final LinkedIn call after the current human approval and separate publish/schedule action.

## Expiry, renewal, and revocation

The service sends one Telegram warning when the configured expiry enters the final seven days. LinkedIn refresh-token access depends on approved programs and is not assumed here.

To rotate safely:

1. Send `/pause` in Telegram.
2. Generate/authorize the replacement token using the same LinkedIn application and member.
3. Inspect its status, scopes, and exact expiry.
4. Replace `LINKEDIN_ACCESS_TOKEN` and `LINKEDIN_ACCESS_TOKEN_EXPIRES_AT` in the deployment secret store.
5. Run `make verify-linkedin` and confirm the resulting author URN is unchanged.
6. Run `make preflight` and restart the application container.
7. Send `/resume`.

If a token or client secret may have leaked, revoke/rotate it immediately in LinkedIn's portal, rotate the deployment secret, and inspect the immutable audit log plus LinkedIn profile for unexpected activity.

## Official references

- [Getting access to LinkedIn APIs](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access)
- [Share on LinkedIn](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin)
- [Three-legged OAuth flow](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow)
- [Developer Portal tools](https://learn.microsoft.com/en-us/linkedin/shared/authentication/developer-portal-tools)
- [Token introspection](https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection)
- [Sign In with LinkedIn using OpenID Connect](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2)
- [Current member Profile API](https://learn.microsoft.com/en-us/linkedin/shared/integrations/people/profile-api)
