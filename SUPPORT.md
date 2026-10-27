# Support and compatibility

General, secret-free bug reports and reproducible SDK issues belong in
https://github.com/availkit/sdk-python/issues. Consult the [versioned release notes](https://github.com/availkit/sdk-python/releases)
for dated verification results and available distribution channels.
Support is best effort during preview, without a response-time or availability
SLA. Maintainers may request a small synthetic reproduction, never credentials.
This preview has one maintainer; an independent backup maintainer is not established.

Version0.x breaking public API, runtime-minimum or security-boundary changes use a
minor release with migration notes; compatible fixes and metadata use patches.
A supported runtime declaration is distinct from actual tested versions. Consult
`runtime-policy.json`, this README and each release's dated finite evidence.
Native Android/iOS execution is not implied by JVM/Linux compilation. Keep old
versioned artifact bytes immutable; use a new release for corrections.

Only explicitly documented operations are supported. No automatic mutation
retry, lease recovery, cross-language durable-store parity or native mobile
account/passkey flow is implied. Reads cannot prove an unknown mutation failed.
Retain its original scope/body/key and use only deliberate supported replay while
current authority and server retention apply. Private storage/backups/exports
remain an application responsibility; same-server backup does not cover host loss.
