# Python backend examples 0.2.0

Install the hosted wheel in a clean virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install https://availkit.dev/downloads/availkit-0.2.0-py3-none-any.whl
```

These exact scripts also ship as modules in the wheel, so no private repository
clone is needed. The source archive and a separate examples ZIP are available
from the Python guide at https://availkit.dev/sdk/python.

Create an owned **test** resource with hours and an offering using the dashboard.
Select a range with at least 2 future available slots. Issue a test key with READ
and BOOK (CATALOG only if your own backend configures catalog data). Save the key
directly from its once-only reveal into an owned 0600 secret file. Never paste its
value into a command or commit it. Supply nonsecret configuration and file paths:

```sh
export AVAILKIT_WORKSPACE_ID=REPLACE_WITH_WORKSPACE_ID
export AVAILKIT_ENVIRONMENT=test
export AVAILKIT_CREDENTIAL_FILE=/private/path/availkit-test-key
export AVAILKIT_OFFERING_ID=REPLACE_WITH_OFFERING_ID
.venv/bin/python -m availkit.examples.first_booking inspect
.venv/bin/python -m availkit.examples.first_booking create
.venv/bin/python -m availkit.examples.first_booking read
.venv/bin/python -m availkit.examples.first_booking move
.venv/bin/python -m availkit.examples.first_booking cancel
```

Dates default to UTC now plus1 hour through the following7 days. The private
intent directory defaults to `./private/python-intents` (created0700). You can
override these with `AVAILKIT_FROM`/`AVAILKIT_TO` (UTC ISO8601 ending in`Z`, at
most7 days apart) and `AVAILKIT_INTENT_DIR`. Choose configured hours in that range.

The directory must be private 0700. It contains fsynced 0600 original intents and
results. Mutation steps refuse an existing intent instead of silently resending.
A failure after sending can be unknown; keep the directory. The separate
`reconcile` action only reads candidates. After inspection an explicit
`replay-create`, `replay-move` or `replay-cancel` sends that original supported
request once with the same key and scope. The API's 24-hour replay retention and
current credential authority still apply; do not replace an expired intent under
a fresh key. A replay's historical response may differ from a current read.
The SDK retains the original creation timestamp and refuses execution24 hours
after preparation, including delayed first sends. Keep expired intents for
read-only reconciliation; never edit metadata to bypass that boundary.

The public-page read example needs only `AVAILKIT_PAGE_ID` for a published page:

```sh
.venv/bin/python -m availkit.examples.public_booking
```

For the backend webhook example, privately save the once-only signing secret to
`AVAILKIT_WEBHOOK_SECRET_FILE` (0600), set `AVAILKIT_WEBHOOK_STATE_DIR` to an
owned0700 durable directory, then:

```sh
.venv/bin/python -m availkit.examples.webhook_receiver
```

Also set `AVAILKIT_WORKSPACE_ID`, `AVAILKIT_ENVIRONMENT` and
`AVAILKIT_WEBHOOK_SUBSCRIPTION_ID` to the subscription's intended scope. The
receiver checks the signed payload's scope and requires signed `deliveryId` to
match the delivery header. The event lives under `payload.event`, not at the root.

```sh
export AVAILKIT_WEBHOOK_SUBSCRIPTION_ID=REPLACE_WITH_SUBSCRIPTION_ID
export AVAILKIT_WEBHOOK_SECRET_FILE=/private/path/availkit-webhook-secret
export AVAILKIT_WEBHOOK_STATE_DIR=/private/path/owned-webhook-state
.venv/bin/python -m availkit.examples.webhook_receiver
```

It binds 127.0.0.1:8090 and handles POST `/webhooks/availkit`. Use an existing HTTPS
ingress for external sender delivery. Signature verification precedes parsing;
delivery deduplication and its local business effect share one SQLite transaction.
For external effects use a transactional outbox keyed by delivery ID. A database
failure returns 503 so the same delivery can retry. No raw request or secret logs.


The additive durable create example is `python -m availkit.examples.durable_booking`.
Follow the full configuration, action and interruption instructions in the SDK
README's **Durable operation stores** section. It supports local filesystem or
stdlib SQLite storage and never automatically sends/retries after reopening.
