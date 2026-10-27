# Security policy

Do not put API keys, private booking/contact capabilities, customer data, recovery
codes, provider credentials or raw production traces in public issues or PRs.
SDK errors omit these values; application local-variable/query capture can still
expose them. Keep keys on trusted backends and use the least current authority.

For a suspected vulnerability use [private vulnerability reporting](https://github.com/availkit/sdk-python/security/advisories/new).
If that channel is unavailable, do not publish sensitive details in a public issue;
ask for a private contact using a secret-free issue.

The latest verified release line receives best-effort fixes. No response deadline,
24/7 service, bounty, contractual SLA or unlimited old-version support is promised.
Security fixes are forward releases; never silently replace a published version.
Keep runtimes, TLS trust stores and transitive dependencies updated. Registry
publisher credentials remain separate from application/API authority and builds.
