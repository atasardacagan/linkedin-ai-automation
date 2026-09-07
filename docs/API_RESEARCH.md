# Official API research — 2026-09-06

This note records the official capabilities used by the repository and separates self-service features from approval-gated features. It should be reviewed quarterly because LinkedIn publishes monthly Marketing API versions and OpenAI model availability can change by account.

## LinkedIn conclusions

| Need | Official capability | Permission/access | Implementation decision |
|---|---|---|---|
| Post as the authenticated member | Share on LinkedIn + versioned Posts API | `w_member_social`; self-service product | Use `POST /rest/posts` only after immutable human approval |
| Attach one image | Images API | `w_member_social` for a member owner | Initialize with `POST /rest/images?action=initializeUpload`, upload bytes, reference image URN |
| Identify current member | Current Member Profile API or OIDC user info | Profile permission or `openid profile` | Bootstrap helper tries `/v2/me` then `/v2/userinfo`; store application-scoped Person URN |
| Measure member posts | Member Creator Post Analytics | `r_member_postAnalytics`; approved access | Disabled by default; no scraping fallback |
| Version stability | Marketing API version header | Monthly versions, minimum one-year support | Default `LinkedIn-Version: 202608` and quarterly review |

### Posting

LinkedIn's [Getting Access](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access) and [Share on LinkedIn](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin) documentation identify **Share on LinkedIn** as the self-service product granting `w_member_social` to act for the authenticated member.

The current [Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api) creates an organic post with:

- `POST https://api.linkedin.com/rest/posts`;
- bearer member token;
- `LinkedIn-Version: YYYYMM`;
- `X-Restli-Protocol-Version: 2.0.0`;
- a Person URN author, commentary, public visibility, main-feed distribution, and `PUBLISHED` lifecycle state.

A successful create returns HTTP 201 and the created post URN in `x-restli-id`. Because a transport failure after the request body is sent can hide that successful response, the repository treats such outcomes as ambiguous and blocks automatic replay.

### Images

The [Images API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/images-api) supports a member owner with `w_member_social`. The repository initializes an upload, PUTs the exact approved PNG to LinkedIn's returned URL, and references the resulting image URN in the Posts API request. No media URL scraping or browser upload is used.

### API version

LinkedIn's [versioning page](https://learn.microsoft.com/en-us/linkedin/marketing/versioning) lists August 2026 / `202608` as the latest version on the research date. The [migration table](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/migrations) lists `202608` as active through **2027-08-17**. Versions are released monthly and supported for at least one year, so the date is a maximum planning boundary rather than a reason to defer review.

The version is an environment setting because a newer supported header may need qualification without a code change. CI cannot prove a user's LinkedIn application permissions; a live dry-run-to-real activation remains an operator-controlled acceptance test.

### Member identity

The [Post schema](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/post-api-schema) requires `urn:li:person:<id>` for a member author and points to the current-member Profile API for the application-scoped ID. LinkedIn documents that member IDs are application-scoped.

The [OpenID Connect integration](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2) exposes `GET /v2/userinfo` with a `sub` subject when `openid profile` is granted. The helper prefers the explicit current-member `id` when Profile API access exists, then uses the OIDC subject. Developer Portal Token Inspector is the fallback authority for the authenticated member and token scopes.

### Member analytics

The [Member Post Statistics](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/members/post-statistics) endpoint is `GET /rest/memberCreatorPostAnalytics`. Its single-post finder uses Rest.li entity tuples such as:

```text
(share:urn:li:share:...)
(ugc:urn:li:ugcPost:...)
```

The implementation requests lifetime `TOTAL` values for `IMPRESSION`, `REACTION`, `COMMENT`, `RESHARE`, and `LINK_CLICKS`. The page states that this data is best-effort and not for billing. Access requires `r_member_postAnalytics`, which is separate from basic posting. The collector therefore:

- remains off by default;
- stores a UTC daily call counter before making calls;
- reserves all five calls for a post atomically;
- rotates among recent posts without a current-day snapshot;
- stores raw responses and normalized snapshot columns;
- never scrapes the LinkedIn website.

### OAuth and token operations

Member posting requires three-legged member authorization; two-legged application tokens do not represent a member. The [Developer Portal tools](https://learn.microsoft.com/en-us/linkedin/shared/authentication/developer-portal-tools) include a Token Generator for testing and Token Inspector for active/expired state, scopes, and TTL. The [token introspection API](https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection) returns `expires_at` when called with matching application credentials.

The repository accepts a member token and exact expiry through deployment secrets for a controlled single-operator installation. It warns seven days before expiry and assumes human reauthorization unless LinkedIn has separately granted refresh-token capability. It never automates a LinkedIn password. A multi-user service would need a full authorization-code callback, encrypted per-tenant token storage, consent/revocation lifecycle, and privacy controls that are outside this release.

## OpenAI conclusions

| Need | API | Default |
|---|---|---|
| Current research and source list | Responses API web search | `gpt-5.6-terra` |
| Strict content and intent schemas | Responses API Structured Outputs | `gpt-5.6-terra` |
| Independent editorial/fact review | Responses API | `gpt-5.6-terra` |
| Similarity representation | Embeddings API | `text-embedding-3-small` |
| Original PNG visual | Images API | `gpt-image-2` |

The defaults were checked against the official [model catalog](https://developers.openai.com/api/docs/models), [GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra), and [GPT-Image-2](https://developers.openai.com/api/docs/models/gpt-image-2). Model access remains account-dependent, so every name is configurable.

Responses used for research, drafting, revision, quality review, and natural-language intent parsing set `store=false`. JSON Schema is strict and application code recomputes weighted totals instead of trusting an AI-provided total. Images are base64-decoded with validation, written atomically, addressed by SHA-256, and verified again at approval and publication.

## Source-validation limits

OpenAI research is treated as untrusted input. A proposed citation is retained only when its normalized URL also appears in the Responses web-search tool source list. The local validator then:

1. permits only HTTP(S), no URL credentials, and only standard ports;
2. resolves DNS and rejects the entire hostname if any result is non-public;
3. connects to a validated address while preserving HTTPS SNI and Host;
4. revalidates every redirect, up to five hops;
5. requires a successful text response no larger than 1.5 MB;
6. checks conservative lexical support between the proposed fact and retrieved page.

This reduces fabricated URLs, SSRF, DNS rebinding, and clearly unsupported source notes. It cannot mathematically establish the truth or context of every claim. The independent quality pass and human approval are mandatory complementary controls.

## Explicit exclusions

The repository does not implement:

- LinkedIn browser automation, cookies, username/password login, scraping, or private endpoints;
- automatic comments, reactions, connection requests, direct messages, or engagement pods;
- organization posting or advertising permissions;
- unapproved analytics access;
- silent model fallback that bypasses schemas or quality thresholds;
- automatic retry after a possibly successful LinkedIn post request.

Those exclusions are intentional platform-policy and duplicate-publication safeguards.
