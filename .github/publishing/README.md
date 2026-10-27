# Prebuilt SDK registry publisher

This upload-only automation publishes reviewed prebuilt SDK archives. Its source
does not create a repository, environment, trusted publisher or registry account.
Provider activation and actual OIDC/first-release acceptance remain separately
gated on account setup and explicit release verification.

In a standalone SDK repository, these helpers, their tests, schema and this guide
live under `.github/publishing/`. The repository contains only its selected
`.github/workflows/publish.yml`. Run the suite from that SDK repository root:

```sh
python3 -m unittest discover -s .github/publishing -p test_publishing.py
```

Maintainer source note: the canonical application tree keeps this source under
`ops/sdk-publishing`, outside `clients/`, and holds all five
`workflows/<ecosystem>/publish.yml` templates. After source review and finite
mocked checks, the allowlisted SDK exporter copies only the selected workflow
and the helper/test/schema/guide files to the standalone paths above. Never copy
app source/history/configuration, local evidence, credentials or private
application automation into an SDK repository.

| Ecosystem | Only repository | Package | Prebuilt registry files |
| --- | --- | --- | --- |
| TypeScript | `availkit/sdk-typescript` | `@availkit/booking` | `availkit-booking-VERSION.tgz` |
| Python | `availkit/sdk-python` | `availkit` | `availkit-VERSION-py3-none-any.whl`, then `availkit-VERSION.tar.gz` |
| .NET | `availkit/sdk-dotnet` | `AvailKit` | `AvailKit.VERSION.nupkg` |
| Ruby | `availkit/sdk-ruby` | `availkit` | `availkit-VERSION.gem` |
| Rust | `availkit/sdk-rust` | `availkit-sdk` | `availkit-sdk-VERSION.crate`; a separate approved `availkit-sdk-VERSION.publish.json` supplies Cargo upload metadata |

Only stable `vMAJOR.MINOR.PATCH` tags are supported. Prerelease/tag conventions
need a separately reviewed policy change. No Maven/Packagist/Go/Swift publisher
is included here. No account setup, paid infrastructure or registry ownership is
inferred from repository/domain ownership.

## Release inputs and source binding

Build/test/package and inspect the SDK on the owned server. This uploader never
checks out the application, builds an SDK, runs package lifecycle hooks, invokes
`cargo package`/`cargo publish`, `dotnet pack`, or a gem release/build task. It
checks out only publisher code at the workflow's own commit; every third-party
Action is pinned to a complete inspected commit SHA. Runtime setup is separate
from SDK compilation. Jobs use GitHub-hosted Ubuntu24.04, a fifteen-minute job
bound, individually bounded steps and the `registry-release` environment.

First create the exact SDK-only source tag. After that commit exists, place a
reviewed `.github/publishing/approved/VERSION.json` on **main**:

```json
{
  "schema": 1,
  "ecosystem": "python",
  "package": "availkit",
  "version": "0.2.0",
  "tag": "v0.2.0",
  "source_commit": "EXACT_40_HEX_SDK_TAG_COMMIT",
  "manifest_sha256": "EXACT_64_HEX_RELEASE_MANIFEST_DIGEST"
}
```

The approval lives after the source tag to avoid a self-referential commit hash.
It is a reviewed release mapping from source to the already tested package
bytes; it is not an uploader-generated build-provenance claim. Do not put
private application commit/source paths or credentials into it.

Attach `release-manifest.json` plus the exact fixed artifact filenames to that
SDK repository's GitHub Release. Enable immutable releases before publishing;
the helper requires the API's published immutable-release flag, a stable tag,
exact peeled tag commit and the approval's manifest digest. The manifest has
only these fields (see `manifest.schema.json`):

```json
{
  "schema": 1,
  "ecosystem": "python",
  "repository": "availkit/sdk-python",
  "package": "availkit",
  "version": "0.2.0",
  "tag": "v0.2.0",
  "source_commit": "EXACT_40_HEX_SDK_TAG_COMMIT",
  "artifacts": [
    {"role":"wheel","filename":"availkit-0.2.0-py3-none-any.whl","sha256":"EXACT_64_HEX_DIGEST","size":12345},
    {"role":"sdist","filename":"availkit-0.2.0.tar.gz","sha256":"EXACT_64_HEX_DIGEST","size":23456}
  ]
}
```

All fields/roles/filenames must match the fixed ecosystem policy. Other SDKs use
`package`; Rust additionally uses `cargo-metadata`. Neither descriptor accepts
an upload/download URL, shell command, alternative package/repository or token.
The uploader selects the exact assets by ID from that same release, verifies
size/hash and package identity/source metadata, and never executes extracted
source. Cargo metadata is separately digest-bound and identity-checked; its
`.crate` bytes are appended unchanged using the documented raw protocol.

## Attempt markers and unknown outcomes

Before each registry upload, create a public-safe file on main at:

`.github/publishing/attempts/ECOSYSTEM/VERSION/APPROVED_FILENAME.json`

It records only fixed package/repository/tag/source/release/file identities,
approved digests, run numbers/time, `attempted` or `confirmed` and a narrow
verification result. It never contains a credential, raw request/reply, token,
private path or application data. Contents API writes are restricted by code to
these allowlisted marker paths; tags/source trees are never rewritten.

Creation omits an existing file SHA (create-only). Confirmation supplies that
file's current blob SHA (CAS). Create admission carries the **exact main commit
at which the current approval was read** into the marker writer. It requires
both the pre-write main head and resulting commit parent to equal that checked
commit. The Contents API has no branch-head precondition: a main change before
the write stops without upload; a change during the write retains the marker
and stops before upload. A fresh unchecked main head cannot substitute for the
approval's commit. Main's
rules must permit these SDK-only marker writes. A denied write stops before the
registry; it is not a reason to use a private-app credential or weaken app rules.

Once a marker exists, reruns only read and reconcile its exact intended registry
file or stop. Missing, delayed, mismatched or unreadable state never permits a
blind second upload, automatic marker removal, fresh version, yank or unpublish.
A lost marker acknowledgment likewise sends no registry upload and leaves an
explicit operator investigation. Review the run and registry observations before
any separate owner-directed continuation; this helper provides no retry switch.

Python has exactly two per-file markers. It stops further writes immediately on
an unknown first upload or confirmation. A later deliberate rerun may reconcile
the first file as exact and then upload the still-unattempted second original.
It cannot reupload the attempted first file or hide a partial version. Registry
indexing can lag: an accepted upload whose public file is not yet visible stays
unknown until a later read-only reconciliation.

## Authentication and exact bytes

Configure trusted publishers for the exact repository, filename `publish.yml`
and environment `registry-release`; initial namespace/bootstrap rules remain
provider-specific. `release.published` and manual dispatch on main are the only
triggers. There is no PR/fork trigger or cross-repository upload. GitHub's job
token has contents-write only in the SDK repository for markers; OIDC has its
own short-lived provider scope. No static production application secret is used.

- npm uses its CLI's supported GitHub-hosted OIDC path, npm11.5.1+ and Node24.
  The command publishes the verified **tgz file**, with `--ignore-scripts`,
  `fetch-retries=0` and `--provenance=false`. It isolates npm configuration and
  removes token fallbacks. Uploading a server-built archive from Actions does
  not make Actions its build system. Name bootstrap, publisher direct-publish
  permissions and current activation delays must be completed before activation.
- PyPI uses the documented GitHub OIDC audience `pypi` and token-mint endpoint,
  then one stdlib multipart POST for each exact wheel/sdist file. No twine build,
  install, skip-existing behavior or upload retry is used. Pending publishers
  support first-project setup; accounts/namespace rights are still prerequisites.
- NuGet uses the pinned official `NuGet/login` action to obtain a temporary key.
  Upload is one raw HTTP PUT with the key in `X-NuGet-ApiKey`, never a CLI argument.
  The original unsigned nupkg bytes are the multipart file payload. NuGet.org
  adds its repository signature: reconciliation requires every other ZIP entry
  name/byte to match, then bounded `dotnet nuget verify --all` with fingerprints
  from NuGet.org's announced repository-signature index. It also requires an
  actual repository signature naming the NuGet.org service index, not merely a
  valid other author signature. Both archive hashes are recorded. This sole
  provider transformation is disclosed; ZIP header equality is not promised.
- Ruby uses the pinned official auth-only credentials action and one raw gem
  upload POST. No Ruby setup, gem build, release task or dependency install runs.
- Rust uses the pinned official auth-only crates.io action. It submits one raw
  Cargo registry PUT containing the approved JSON metadata and exact `.crate`
  bytes; Cargo is never invoked. The action revokes its temporary key in its
  normal post step. First-crate ownership/bootstrap remains a separate setup.

Except for NuGet's verified signature-only transformation, post-upload reads
require the exact approved archive size/SHA256. Provider metadata alone never
settles an operation. Requests have bounded bodies/time, no automatic retry and
no mutation redirects. Read redirects strip authentication and accept only the
fixed trusted provider/CDN hosts. Tokens stay in headers/environment or memory.
Captured npm/verifier diagnostics remain0600 under the ephemeral runner's0700
working directory; only fixed status codes/counts go to the public job log.
The only child commands are bounded npm publishing/version and read-only
signature verification; failures kill/join their owned process group.

## Current source status and verification

The earlier finite offline mock suite passed 13/13 before the approval-head,
Cargo preflight and standalone-layout amendments. The amended cases have not
yet executed; maintainers must run the final suite before activating a release.
Mocks and syntax checks do not establish live OIDC identity, provider
permissions, signed NuGet output-format compatibility or successful registry
publication. In the canonical source tree, run:

```sh
python3 -m unittest discover -s ops/sdk-publishing -p test_publishing.py
```

They cover create-before-upload, unknown marker/upload acknowledgment, retained
markers, Python partial reconciliation, source/tag and digest refusal, fixed
manifest/filename boundaries, raw Cargo framing, Python byte retention, npm
no-hooks/no-retry/no-token-argv policy and the NuGet repository-signature boundary.
Their providers and verifier are mocked; real first-publication/readback and
fresh registry-installed consumers remain separate acceptance.

Official protocol references reviewed27October2026:

- [npm trusted publishers](https://docs.npmjs.com/trusted-publishers/) and [npm publish](https://docs.npmjs.com/cli/commands/npm-publish/).
- [PyPI direct OIDC token use](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
- [NuGet OIDC](https://learn.microsoft.com/en-us/nuget/nuget-org/trusted-publishing), [raw package push](https://learn.microsoft.com/en-us/nuget/api/package-publish-resource), [automatic repository signing](https://learn.microsoft.com/en-us/nuget/reference/signed-packages-reference), and [announced repository certificates](https://learn.microsoft.com/en-us/nuget/api/repository-signatures-resource).
- [RubyGems OIDC and raw gem API](https://guides.rubygems.org/rubygems-org-api/) and [official auth-only action](https://github.com/rubygems/configure-rubygems-credentials).
- [Cargo raw registry publish](https://doc.rust-lang.org/cargo/reference/registry-web-api.html#publish) and [official crates.io auth action](https://github.com/rust-lang/crates-io-auth-action).
