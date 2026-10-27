# AvailKit Python SDK 0.2.0

SDK **0.2.0**. Standalone build and synthetic test instructions are in
[DEVELOPMENT.md](DEVELOPMENT.md). [Versioned release notes](https://github.com/availkit/sdk-python/releases)
record dated verification and available distribution channels. Source availability
does not imply registry publication or mobile/service acceptance.
Install examples name this SDK version; check the release notes for artifact availability.

Typed Python client for the stable AvailKit integration API and published public
booking flow. Workspace credentials run on a trusted backend. Python 3.11–3.15
(`>=3.11,<3.16`), standard library only; no runtime dependencies. The distribution
is a conventional wheel and source archive, hosted by AvailKit. No PyPI registry
publication is claimed. The TypeScript SDK and CLI remain separate products.

## Install in a clean project

```sh
python3 -m venv .venv
.venv/bin/python -m pip install https://availkit.dev/downloads/availkit-0.2.0-py3-none-any.whl
.venv/bin/python -c 'import availkit; print(availkit.__version__)'
```

The printed version is 0.2.0. On Windows use `.venv\Scripts\python.exe`.
The source distribution is
`https://availkit.dev/downloads/availkit-0.2.0.tar.gz`; installing it uses the
PEP517 build backend declared in `pyproject.toml`, requiring access to its
build dependencies. The wheel requires no dependency resolution or repository
clone. Downloadable examples are at
`https://availkit.dev/downloads/availkit-python-examples-0.2.0.zip`; identical
runnable modules also ship in the wheel:

```sh
.venv/bin/python -m availkit.examples.first_booking --help
.venv/bin/python -m availkit.examples.public_booking
.venv/bin/python -m availkit.examples.webhook_receiver
```

## Explicit backend configuration

Create an integration key from the signed-in dashboard for one workspace and
choose **test**. Give it only the permissions needed below. Save its once-only
value directly to an owned 0600 file; keep its parent directory 0700. Do not put
the key in source, a URL, command arguments, a browser bundle, mobile app or logs.
The file loader opens once, rejects final symlinks and nonregular/insecure files,
checks ownership on POSIX and reads a bounded 4096 bytes from the same descriptor.
Permission checks on non-POSIX systems depend on OS protection; use a protected
deployment secret mount. This SDK never reads environment variables implicitly.

```python
import os
from availkit import AvailKitClient

client = AvailKitClient(
    workspace_id=os.environ["AVAILKIT_WORKSPACE_ID"],
    environment="test",
    credential_file=os.environ["AVAILKIT_CREDENTIAL_FILE"],
    # default base_url="https://api.availkit.com", timeout=10 seconds
)
resources = client.list_resources(limit=25)
for resource in resources["items"]:
    # Use the data in your backend; do not log arbitrary customer/secret fields.
    resource_id = resource["id"]
```

`environment` is a mandatory caller intent label. The opaque credential's server
scope selects the actual environment; an SDK argument cannot switch or prove its
scope. Provision a matching workspace/environment key in the dashboard. A test
key cannot access live objects. Never reuse one client as a different authority.
`AvailKitClient.from_environment()` explicitly opts into
`AVAILKIT_WORKSPACE_ID`, `AVAILKIT_ENVIRONMENT`, `AVAILKIT_CREDENTIAL_FILE` (or
`AVAILKIT_CREDENTIAL`, exactly one) and optional `AVAILKIT_BASE_URL`.

Methods use snake_case. Request and response `TypedDict` types in
`availkit.models` preserve API field names such as `offeringId`, `expectedStart`
and `remainingCapacity`. Unknown fields and malformed responses reject with a
safe error. Booking timestamps must be valid UTC ISO8601 instants ending in `Z`.

## Availability and a retained booking intent

```python
from availkit import AvailKitError, new_idempotency_key

slots = client.availability(
    offering_id=offering_id,
    from_="2030-01-07T09:00:00Z",  # placeholders: select a future configured range
    to="2030-01-07T11:00:00Z",
)["slots"]
intent = client.prepare_book(
    {"offeringId": offering_id, "start": slots[0]["start"]},
    new_idempotency_key(),
)
intent.save(private_intent_directory / "create-intent.json")
try:
    booking = client.execute(intent)  # one send; never an automatic retry
except AvailKitError as error:
    safe_diagnostic = error.as_dict()
    # Keep the intent. With outcome="unknown", inspect bookings before deciding
    # whether to explicitly execute that same original intent within retention.
    raise
```

`private_intent_directory` must be an existing owned0700 directory. Save creates
a new0600 file exclusively and fsyncs it and its directory. Existing intents are
never overwritten. A persistence failure stops before send and retains any file
for inspection. `RequestIntent.load(path)` only reads; it never sends. Original
body bytes, idempotency key, canonical origin, workspace/environment intent and a
credential fingerprint are preserved. A different credential/client scope rejects
the loaded intent. Public intents also retain private capability tokens.
Immutable `created_at` metadata is stored as `createdAt`. Execution refuses an
intent24 hours after preparation (conservatively including time before its first
send), and rejects a timestamp more than5 seconds in the future. Keep expired
intents for read-only reconciliation; never edit their timestamp or mint a fresh
key to bypass this guard. Server receipt expiry and current authority still apply.

Read with `client.get_booking(booking_id)`. Move using
`client.prepare_reschedule(booking_id, {"expectedStart": current["start"],
"start": next_slot["start"]}, new_idempotency_key())`; save then execute. Cancel
using `client.prepare_cancel(booking_id, new_idempotency_key())`; save then execute.
`expectedStart` compares the current start; it is not a monotonic booking version.
Convenience `book`, `reschedule_booking` and `cancel` methods also send once, but
durable workflows should prepare/save first. The bundled lifecycle example has
separate inspect/create/read/move/cancel/reconcile/explicit-replay commands.

## Failures, deadlines and replay

`AvailKitError` has `kind` (`input`, `api`, `transport`, `protocol`), safe finite
`code`, optional HTTP `status`, and `outcome`. Input errors are `not_sent`; known
4xx API rejections are `rejected`; unusable responses, transport failure, timeouts,
5xx and unknown error codes are `unknown`. Unknown mutations may have committed.
The client never automatically retries any operation, changes a key, disables a
subscription, creates a replacement booking or sends a compensating mutation.

The default workspace request deadline is 10 seconds, public deadline 2 seconds;
set `timeout` from 0 exclusive to 120 inclusive. It covers DNS, TCP/TLS, request
transmission, headers and bounded body reading. DNS resolution is isolated with
a bounded 8-worker allowance; a timed-out resolver can finish but cannot send.
Deadline shutdown interrupts slow headers and body streams. Decode/schema checks
run on a bounded body (default 1MiB workspace, 256KiB public, configurable up to 8MiB);
local CPU scheduling may finish those checks after the network deadline. Requests
are at most 16KiB. HTTPS verifies platform CA trust and hostname; HTTP is allowed
only on localhost/loopback for local testing. No proxy environment, redirects,
cookies, compression, insecure TLS option or raw transport logging is used.

Only booking create, cancel and reschedule have the documented explicit
idempotency replay contract in these clients. Current replay retention is 24 hours;
retain the original key/body/authority/target and inspect current state. An exact
replay can return its historical response while a fresh read shows later changes.
Expired keys and changed requests are not blanket permission to create a new
intent. Customer/catalog/webhook/publication/maintenance writes have no general
replay guarantee. A lost webhook create response can lose its once-only signing
secret: list subscriptions, inspect, then deliberately choose an operator action.
Never log the raw creation result. Errors discard server messages, URLs, bodies,
headers and underlying exception causes. Disable traceback local-variable capture
in production logging because caller locals can still hold credentials.

## Coverage and permissions

The machine-readable [coverage.json](coverage.json) enumerates every operation
against the checked OpenAPI digest: **34 workspace + 7 public**. Account enrollment,
passkeys, sessions, membership and key issuance remain same-origin dashboard
ceremonies; current recovery is a separate versioned browser protocol. Health probes remain operator HTTP.

| Surface | Python methods | Minimum authority |
| --- | --- | --- |
| Resources | `create_resource`, `list_resources`, `get_resource`, `replace_resource`, `archive_resource` | READ reads; CATALOG writes |
| Hours | `get_hours`, `replace_hours` | READ/CATALOG |
| Offerings | `create_offering`, `list_offerings`, `get_offering`, `replace_offering`, `archive_offering` | READ/CATALOG |
| Availability | `availability` | READ |
| Bookings | `book`, `list_bookings`, `get_booking`, `reschedule_booking`, `cancel` | READ reads; BOOK writes; linking a customer also MANAGE |
| Customers | `create_customer`, `list_customers`, `get_customer`, `replace_customer`, `archive_customer`, `erase_customer` | MANAGE; erasure also PRIVACY |
| Booking page | `get_booking_page`, `replace_booking_page` | READ/CATALOG |
| Webhooks | `create_webhook`, `list_webhooks`, `disable_webhook`, `list_webhook_deliveries` | MANAGE control; READ delivery status |
| Activity/usage | `events`, `get_usage` | READ |
| Maintenance | `prune_history`, `prune_expired_replays` | MANAGE+PRIVACY history; PRIVACY expired replay batch |
| Public page | `PublicBookingClient.get_page`, `list_offerings`, `availability` | Published page identifier |
| Public booking | `book`, `get_booking`, `reschedule`, `cancel` | Private booking capability; create additionally contact-reuse token |

Collection pages default 25, maximum 100; `pages(client.list_resources,
max_pages=100)` provides explicit bounded iteration and raises on exhausted budget
or nonadvancing cursors. `events(after=0, limit=100)` uses numeric event cursors and
exposes retention floor/high water. It is separate from collection pagination.

## Public booking and webhook verification

`PublicBookingClient(page_id=page_id)` accepts no workspace key or account cookie.
`new_guest_token()` creates a 32-byte capability; create/save the booking and
contact tokens before `prepare_book(body, key, booking_token=...,
contact_token=...)`. The body is `{offeringId, start, contact: {displayName,
email?, phone?}}`. The private booking token manages that booking; the contact
token proves contact reuse and is not workspace/account authority. Never put
workspace credentials or webhook secrets in browsers or mobile applications.
This backend SDK does not grant cross-origin account/passkey or new native login
capabilities. See the developer hub for the supported browser architecture.

`verify_webhook(raw_body_bytes, signature_header, signing_secret, tolerance=300)`
verifies exact HMAC-SHA256 bytes, strict header shape and timestamp age. Read
`AvailKit-Signature` and `AvailKit-Delivery-Id` case-insensitively. Verify before
JSON parsing and transactionally deduplicate the stable delivery ID with your
business effect/outbox. Sender retries reuse identity and refresh signatures.
The helper and bundled receiver reject bodies larger than16KiB before processing.
`availkit.examples.webhook_receiver` is a runnable loopback receiver with durable
SQLite deduplication and a local transactional effect; put it behind an existing
trusted HTTPS ingress for actual delivery. It does not expose itself publicly.

## Development, builds and versioning

Use a supported Python runtime from this flat SDK repository root. The same
standalone commands are documented in [DEVELOPMENT.md](DEVELOPMENT.md):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'build==1.3.0' 'setuptools==82.0.1' 'packaging==26.3' 'pyproject_hooks==1.3.3'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python build.py --output artifacts
```

Run the exact wheel from an empty consumer venv, and the source archive from a
separate one, before release. The runtime uses no dependencies. Version 0.2.0
starts a maintained semantic-versioned SDK; breaking public changes increment
the minor version while pre 1.0, then the major version after 1.0. Package versions,
download filenames and examples advance together. Browser account ceremonies are
deliberately outside its contract. Evidence and tested runtime versions belong
to the release acceptance record, not claims implied by this README.


## Durable operation stores (0.2.0)

`availkit.durable.FileOperationStore` and `SQLiteOperationStore` wrap the existing
immutable `RequestIntent` for its six workspace/public create/move/cancel
operations. Neither store sends, retries, reconciles or compensates. Original
scope/body bytes/idempotency key/preparation time are immutable; a stable local
ID conflicts if given another original. No workspace API credential is embedded;
public originals contain private booking/contact capabilities and must stay secret.

```python
from availkit import AvailKitError
from availkit.durable import FileOperationStore
store = FileOperationStore('/private/owned0700/operations')
try:
    local_id = '00000000000000000000000000000001'  # stable across restarts
    # Prepare only once; persist before any possible send.
    original = client.prepare_book(body, new_idempotency_key())
    row = store.create(local_id, original)
    attempt = store.begin(local_id, row.revision, original)
    # UNKNOWN is durable before begin returns. This line sends exactly once.
    try:
        booking = client.execute(original)
    except AvailKitError as error:
        store.settle(local_id, attempt.revision, attempt.attempt_token(),
                     'rejected' if error.outcome == 'rejected' else 'unknown')
        raise
    store.settle(local_id, attempt.revision, attempt.attempt_token(), 'confirmed')
finally:
    store.close()
```

The runnable `availkit.examples.durable_booking` keeps `prepare`, `send`,
`inspect`, `reconcile`, `acknowledge-interruption` and `replay` as separate actions.
It refuses a second prepare for a retained operation. Use the same least-privilege
READ+BOOK owned test setup as the first-booking example. Save the key directly in
its0600 credential file and choose an actual future available UTC slot:

```sh
umask 077
mkdir -p private
mkdir -m 700 private/operations
export AVAILKIT_WORKSPACE_ID=REPLACE_WITH_WORKSPACE_ID
export AVAILKIT_ENVIRONMENT=test
export AVAILKIT_CREDENTIAL_FILE=/private/path/owned0600-test-key
export AVAILKIT_OFFERING_ID=REPLACE_WITH_OWNED_OFFERING_ID
export AVAILKIT_START=REPLACE_WITH_FUTURE_AVAILABLE_UTC_TIMESTAMP
export AVAILKIT_OPERATION_DIR="$PWD/private/operations"
export AVAILKIT_OPERATION_BACKEND=file # or sqlite in a separate directory
.venv/bin/python -m availkit.examples.durable_booking prepare
.venv/bin/python -m availkit.examples.durable_booking inspect
.venv/bin/python -m availkit.examples.durable_booking send
```

Both adapters currently require Linux and a trusted local owned0700 directory.
Regular files are owned0600 and descriptor-checked; symlink path components and
special files are refused. SQLite uses optional stdlib `sqlite3`, a1-second busy
timeout, DELETE journaling and FULL synchronization. Its engine opens its own DB
path: do not modify/share the directory with other software while open. The
filesystem adapter uses an exclusive crash-retained directory lock and an
fsynced atomic snapshot. It never removes a lock based on elapsed time.

A process interruption leaves `unknown` with an active claim. After joining all
workers, explicitly call `release_interrupted(...,
acknowledge_workers_joined=True)` or use `acknowledge-interruption` with
`AVAILKIT_ACK_WORKERS_JOINED=yes`. This is an owner assertion, not proof of worker
death or server outcome. A separate explicit `begin(..., replay=True)`/`replay`
admits the exact original only at a nonnegative age under24 hours. Existing
`client.execute` scope/authority/server-retention checks still apply. A current
read is a reconciliation aid, never proof that the original failed or succeeded.
Terminal confirmed/rejected records cannot be reopened through this helper.

For a filesystem crash lock, `recover_lock(acknowledge_workers_joined=True)`
refuses if a private unfinished `.pending` snapshot remains. Keep it for private
inspection; do not guess its publication or delete an unknown original. SQLite
releases the database transaction lock on process exit, while preserving the
application's unknown claim.

Limits:128 records/store,65536 bytes/record,256 revisions,64 admitted attempts and
16MiB SQLite DB. No auto eviction/deletion/claim expiry. Capacity exhaustion fails
closed. `repr(record)` is redacted; `record.original()`,
`RequestIntent.export_private()`/`from_private()` and `attempt_token()` deliberately
expose private material. The store saves no raw API result/error. It is plaintext
PII/capability-bearing local storage; protect exports/backups and disable local
variable/query capture. It does not cover same-UID/root software, hostile mounts,
network filesystems, rollback or host loss. Retention and deliberate archival
after resolution belong to the application, as does any external DB adapter.
Node and Python storage formats are not an interchange contract.


A stored state describes the original logical operation, not just the latest
attempt. If an earlier send was unknown, a later explicit replay's403/409 or other
recognized rejection cannot prove that earlier send failed. Both stores centrally
normalize `settle(..., rejected)` after more than one admitted attempt to
`unknown`, clear only the completed attempt claim and retain the exact original.
A first attempt's definite rejection can be terminal. The runnable examples label
the latest `attemptOutcome` separately from the retained `operationState`.


Manual filesystem lock recovery requires every writer, new worker starter and
other recovery operator to remain quiescent for the entire removal operation.
Joining only a previously crashed worker is insufficient if another process can
start concurrently. There is no concurrent manual-repair protocol; stop the
application's workers before private repair and resume them only after it returns.


See [CHANGELOG.md](CHANGELOG.md), [SUPPORT.md](SUPPORT.md) and
[SECURITY.md](SECURITY.md) for release policy and private reporting prerequisites.
`contract-projection.json` pins only this SDK's supported wire operations;
additive unrelated API routes do not imply this SDK implements them.

The separate optional business export endpoint and current versioned browser
recovery protocol are not methods of this SDK. Use their documented browser/
TypeScript integration where appropriate; this package preserves its declared
workspace/public booking surface rather than inventing account/native APIs.
