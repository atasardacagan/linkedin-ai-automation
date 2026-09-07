# Security policy

## Supported versions

Security fixes are applied to the latest release on `main`. Until a second release line exists,
only `0.1.x` is supported.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository when it is available. If the
private form is not available, open a minimal issue asking the maintainer for a private contact
channel. Do not include an exploit, access token, API key, database URL, personal content, or
other sensitive data in a public issue.

Include the affected commit or release, impact, reproduction conditions, and a proposed mitigation
when possible. Please allow reasonable time for validation and a coordinated fix before public
disclosure.

## Credential exposure

If a credential may have been exposed, do not wait for a code change:

1. revoke or rotate it at OpenAI, Telegram, LinkedIn, or the database provider;
2. pause automatic generation and keep `DRY_RUN=true`;
3. update the deployment secret store and restart the service;
4. inspect LinkedIn activity, immutable audit events, queue failures, and provider access logs;
5. remove leaked values from public systems without relying on Git history rewriting as revocation.

The application never needs a LinkedIn password. Any request for one is outside this project's
design and should be treated as suspicious.

## Container vulnerability policy

The runtime base uses an explicit Debian release tag and an immutable multi-platform digest. CI runs
two pinned Trivy image scans: one reports every HIGH or CRITICAL finding, including findings for
which the distribution has not published a fix; the blocking scan fails on HIGH or CRITICAL
findings that have a vendor-provided fixed version.

An unfixed finding is not considered harmless or resolved. It remains a tracked base-image risk.
When a vendor fix becomes available, the blocking scan requires the runtime digest to be updated
before CI can pass. Reassess the base-image choice and outstanding report whenever a release is
prepared.

## Security invariants

A change is security-sensitive when it touches Telegram administrator checks, webhook validation,
approval nonces, immutable approvals/revisions/audit events, content or image digests, LinkedIn
request classification, URL fetching, secret redaction, OAuth scopes, or database migrations.
Such a change requires focused tests and a review of `docs/ARCHITECTURE.md` and
`docs/OPERATIONS.md`.
