"""Trusted backend workspace client and separate public booking client."""

import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any, Generic, Literal, TypeVar, cast
from urllib.parse import urlencode

from . import models as m
from ._transport import Transport
from ._validation import OPERATIONS, credential, fail, identifier, input_body, instant, intent_key, text, validate
from .errors import AvailKitError
from .intents import RequestIntent, read_private

Environment = Literal["test", "live"]
T = TypeVar("T")
_SAFE_CODES = frozenset(
    "booking_page_paused booking_access_in_use contact_changed validation_failed synthetic_email_required "
    "unauthorized forbidden scope_denied not_found invalid_input invalid_query invalid_cursor invalid_limit "
    "quota_exceeded unavailable credential_invalid capability_denied customer_archived idempotency_conflict "
    "capacity_conflict capacity_exceeded version_conflict catalog_in_use catalog_archived event_cursor_expired "
    "invalid_id invalid_range identity_unavailable recovery_unavailable csrf_invalid recent_auth_required "
    "secret_already_revealed idempotency_mismatch membership_changed membership_denied last_credential "
    "identity_limit identity_rate_limited membership_conflict request_key_invalid".split()
)
_REPLAY = frozenset(("book", "cancel", "rescheduleBooking", "bookPublic", "cancelPublicBooking", "reschedulePublicBooking"))


def new_idempotency_key() -> str:
    """Generate once per distinct intent; retain before sending."""
    return secrets.token_urlsafe(24)


def new_guest_token() -> str:
    """Generate a32-byte private capability before a public booking send."""
    return secrets.token_urlsafe(32)


class _BaseClient(Generic[T]):
    _root: str
    _scope: str
    _headers: dict[str, str]
    _public: bool = False
    _transport: Transport
    page_id: str

    def __repr__(self) -> str:
        return f"<{type(self).__name__} (credentials redacted)>"

    def _path(self, operation: str, identifiers: tuple[str, ...] = ()) -> str:
        path = OPERATIONS[operation]["path"]
        root_template = "/v1/public/pages/{pageId}" if self._public else "/v1/workspaces/{workspaceId}"
        path = path.removeprefix(root_template)
        for value in identifiers:
            identifier(value)
            path = re.sub(r"\{[^}]+\}", value, path, count=1)
        if "{" in path:
            fail("invalid_id")
        return path

    def _call(self, operation: str, ids: tuple[str, ...] = (), body: Any = None,
              query: dict[str, Any] | None = None, key: str | None = None,
              extra_headers: dict[str, str] | None = None) -> Any:
        spec = OPERATIONS[operation]
        payload = input_body(body, spec["input"]) if spec["input"] is not None else None
        path = self._path(operation, ids)
        if query:
            path += "?" + urlencode(query)
        return self._send(operation, path, payload, key, extra_headers or {})

    def _send(self, operation: str, path: str, payload: bytes | None,
              key: str | None, extra_headers: dict[str, str]) -> Any:
        spec = OPERATIONS[operation]
        headers = {**self._headers, **extra_headers}
        if key is not None:
            headers["Idempotency-Key"] = intent_key(key)
        status, response_headers, data = self._transport.request(spec["method"], self._root + path, payload, headers)
        if not 200 <= status < 300:
            code: str | None = None
            if type(data) is dict:
                e = data.get("error")
                if type(e) is dict and type(e.get("code")) is str and type(e.get("message")) is str:
                    code = e["code"]
                elif data.get("type") == "urn:availkit:problem" and data.get("status") == status and type(data.get("code")) is str:
                    code = data["code"]
            if code is None:
                raise AvailKitError("protocol", "invalid_error", status)
            raise AvailKitError("api", code if code in _SAFE_CODES else "api_error", status)
        if (status != spec["status"] or not validate(data, spec["output"])
                or (self._public or path.startswith(("/usage", "/maintenance/", "/customers")))
                and response_headers.get("cache-control") != "no-store"):
            raise AvailKitError("protocol", "invalid_response", status)
        self._relations(operation, path, payload, data, status)
        return data

    def _relations(self, operation: str, path: str, payload: bytes | None, data: Any, status: int) -> None:
        """Reject malformed order, identifiers and success assertions."""
        valid = True
        body: Any = json.loads(payload) if payload is not None else None
        if "items" in data:
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(path).query)
            limit = int(query.get("limit", ["25"])[0])
            after = query.get("after", [""])[0]
            items = data["items"]
            valid = len(items) <= limit and all(v["id"] > (items[i - 1]["id"] if i else after) for i, v in enumerate(items))
            valid = valid and (data["nextAfter"] is None or len(items) == limit and data["nextAfter"] == items[-1]["id"])
            if operation == "listWebhookDeliveries":
                sub = path.split("/")[2]
                valid = valid and all(v["subscriptionId"] == sub for v in items)
        if operation in ("getResource", "updateResource", "archiveResource", "getOffering", "updateOffering", "archiveOffering", "getBooking", "cancel", "rescheduleBooking", "getCustomer", "replaceCustomer", "archiveCustomer", "eraseCustomer", "disableWebhook", "getPublicBooking", "cancelPublicBooking", "reschedulePublicBooking"):
            valid = valid and data["id"] == path.split("/")[2]
        if operation in ("book", "getBooking", "cancel", "rescheduleBooking", "getPublicBooking", "bookPublic", "cancelPublicBooking", "reschedulePublicBooking"):
            valid = valid and instant(data["end"]) > instant(data["start"])
        if operation in ("cancel", "cancelPublicBooking"):
            valid = valid and data["status"] == "cancelled"
        if operation in ("book", "bookPublic", "rescheduleBooking", "reschedulePublicBooking"):
            valid = valid and data["status"] == "confirmed" and instant(data["start"]) == instant(body["start"])
        if operation == "book":
            valid = valid and data["offeringId"] == body["offeringId"] and data.get("customerId") == body.get("customerId")
        if operation == "bookPublic":
            valid = valid and data["offering"]["id"] == body["offeringId"]
        if operation in ("archiveResource", "archiveOffering", "archiveCustomer"):
            valid = valid and data["archived"] is True
        if operation == "disableWebhook":
            valid = valid and data["enabled"] is False
        if self._public and operation == "getPublicPage":
            valid = valid and data["id"] == self.page_id
        if self._public and "page" in data:
            valid = valid and data["page"]["id"] == self.page_id
        if operation in ("availability", "publicAvailability"):
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(path).query)
            lo, hi = instant(query["from"][0]), instant(query["to"][0])
            slots = data["slots"]
            valid = valid and all(lo <= instant(v["start"]) < instant(v["end"]) <= hi and v["remainingCapacity"] > 0 and (i == 0 or instant(v["start"]) > instant(slots[i - 1]["start"])) for i, v in enumerate(slots))
        if operation == "getUsage":
            as_of = instant(data["asOf"])
            start = as_of.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            valid = valid and instant(data["period"]["start"]) == start and instant(data["period"]["end"]) == end
            valid = valid and all(c["remaining"] == max(0, c["limit"] - c["used"]) for c in data["counters"].values())
        if operation in ("pruneExpiredReplays", "pruneHistory"):
            valid = valid and data["deleted"] <= body["limit"] and (not data["hasMore"] or data["deleted"] == body["limit"])
            if operation == "pruneHistory":
                valid = valid and instant(data["asOf"]) - instant(data["cutoff"]) == timedelta(days=30)
        if operation == "events":
            from urllib.parse import parse_qs, urlsplit
            q = parse_qs(urlsplit(path).query)
            event_after, limit = int(q.get("after", ["0"])[0]), int(q.get("limit", ["100"])[0])
            floor, water, evs = data["retentionFloor"], data["highWater"], data["events"]
            valid = valid and floor <= water and len(evs) <= limit and data["nextAfter"] <= max(water, event_after)
            valid = valid and all(v["sequence"] > (evs[i - 1]["sequence"] if i else max(floor, event_after)) for i, v in enumerate(evs))
            valid = valid and data["nextAfter"] == (evs[-1]["sequence"] if evs else max(event_after, floor))
        if not valid:
            raise AvailKitError("protocol", "invalid_response", status)

    def _prepare(self, operation: str, ids: tuple[str, ...], body: Any, key: str,
                 headers: dict[str, str] | None = None) -> RequestIntent:
        return RequestIntent(operation, self._path(operation, ids),
            input_body(body, OPERATIONS[operation]["input"]), intent_key(key), self._scope,
            tuple((headers or {}).items()))

    def execute(self, intent: RequestIntent) -> T:
        """Send an explicitly supplied original booking intent exactly once.

        Loading or inspecting an intent never calls this method automatically.
        The API replay retention window and live credential authority still apply.
        """
        public_operations = {"bookPublic", "cancelPublicBooking", "reschedulePublicBooking"}
        if (not isinstance(intent, RequestIntent) or intent.scope != self._scope or intent.operation not in _REPLAY
                or (intent.operation in public_operations) != self._public):
            fail("intent_scope_mismatch")
        operation = intent.operation
        age = datetime.now(timezone.utc) - instant(intent.created_at)
        if age >= timedelta(hours=24):
            fail("intent_expired")
        if age < -timedelta(seconds=5):
            fail("invalid_intent_timestamp")
        # Reconstruct the only allowed relative path; no arbitrary proxy operation.
        ids = tuple(re.findall(r"[0-9a-f]{32}", intent.path))
        if self._path(operation, ids) != intent.path or type(intent.body) is not bytes:
            fail("invalid_intent")
        if len(intent.body) > 16384:
            fail("body_too_large")
        body: Any = None
        try:
            body = json.loads(intent.body.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            pass
        input_body(body, OPERATIONS[operation]["input"])
        extra = dict(intent.headers)
        expected = {"Authorization", "X-AvailKit-Contact-Token"} if operation == "bookPublic" else {"Authorization"} if self._public else set()
        if set(extra) != expected or len(intent.headers) != len(expected):
            fail("invalid_intent")
        if self._public:
            self._access(extra.get("Authorization", "").removeprefix("Booking "))
            if not extra["Authorization"].startswith("Booking "):
                fail("invalid_guest_access")
            if "X-AvailKit-Contact-Token" in extra:
                self._access(extra["X-AvailKit-Contact-Token"])
        # Validation must not rewrite the retained bytes: use the original body.
        return cast(T, self._send(operation, intent.path, intent.body, intent.idempotency_key, extra))

    @staticmethod
    def _access(token: str) -> str:
        valid = isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]", token)
        if not valid:
            fail("invalid_guest_access")
        return token

    @staticmethod
    def _page(after: str | None, limit: int, q: str | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            fail("invalid_limit")
        query: dict[str, Any] = {"limit": limit}
        if after is not None:
            query["after"] = identifier(after)
        if q is not None:
            if not text(q):
                fail("invalid_query")
            query["q"] = q
        return query

    def pages(self, read: Callable[..., Any], *, after: str | None = None,
              limit: int = 25, max_pages: int = 100, **kwargs: Any) -> Iterator[Any]:
        """Explicit bounded read pagination; exceeding the budget raises."""
        if type(max_pages) is not int or not 1 <= max_pages <= 1000:
            fail("invalid_page_budget")
        for _ in range(max_pages):
            page = read(after=after, limit=limit, **kwargs)
            yield page
            cursor = page["nextAfter"]
            if cursor is None:
                return
            if not page["items"] or cursor != page["items"][-1]["id"] or cursor <= (after or ""):
                raise AvailKitError("protocol", "invalid_cursor")
            after = cursor
        raise AvailKitError("input", "page_budget_exhausted")

    @staticmethod
    def _availability_query(offering_id: str, from_: str, to: str) -> dict[str, str]:
        identifier(offering_id)
        valid = False
        try:
            valid = timedelta(0) < instant(to) - instant(from_) <= timedelta(days=7)
        except (ValueError, OverflowError):
            pass
        if not valid:
            fail("invalid_range")
        return {"offeringId": offering_id, "from": from_, "to": to}


class AvailKitClient(_BaseClient[m.Booking]):
    """Trusted backend client requiring explicit workspace and environment intent.

    Credentials are issued for one environment. ``environment`` records caller
    intent; the server derives actual environment from the key. It is not an
    override or a server-verified claim: provision a matching key in the dashboard.
    No account cookies/passkey ceremonies are accepted by this client.
    """

    def __init__(self, *, workspace_id: str, environment: Environment,
                 credential: str | None = None, credential_file: str | os.PathLike[str] | None = None,
                 base_url: str = "https://api.availkit.com", timeout: float = 10,
                 max_response_bytes: int = 1048576):
        self.workspace_id = identifier(workspace_id)
        if environment not in ("test", "live"):
            fail("invalid_environment")
        self.environment = environment
        if (credential is None) == (credential_file is None):
            fail("ambiguous_credential")
        value: str | None = credential
        if credential_file is not None:
            raw = read_private(credential_file, 4096, "invalid_credential_file")
            try:
                value = raw.decode("utf-8").removesuffix("\n").removesuffix("\r")
            except UnicodeError:
                value = None
        # Keep the explicit public argument from shadowing the validator.
        from ._validation import credential as check_credential
        secret = check_credential(value)
        self._transport = Transport(base_url, timeout, max_response_bytes)
        self._root = "/v1/workspaces/" + self.workspace_id
        self._scope = ":".join((self._transport.base_url, self.workspace_id, environment, hashlib.sha256(secret.encode()).hexdigest()))
        self._headers = {"Authorization": "Bearer " + secret}

    @classmethod
    def from_environment(cls) -> "AvailKitClient":
        """Opt-in environment loading; credentials never appear in arguments."""
        return cls(workspace_id=os.environ.get("AVAILKIT_WORKSPACE_ID", ""),
                   environment=cast(Environment, os.environ.get("AVAILKIT_ENVIRONMENT", "")),
                   credential=os.environ.get("AVAILKIT_CREDENTIAL"),
                   credential_file=os.environ.get("AVAILKIT_CREDENTIAL_FILE"),
                   base_url=os.environ.get("AVAILKIT_BASE_URL", "https://api.availkit.com"))

    def availability(self, *, offering_id: str, from_: str, to: str) -> m.Availability:
        return self._call("availability", query=self._availability_query(offering_id, from_, to))

    def prepare_book(self, body: m.BookingInput, idempotency_key: str) -> RequestIntent:
        return self._prepare("book", (), body, idempotency_key)

    def prepare_cancel(self, booking_id: str, idempotency_key: str) -> RequestIntent:
        return self._prepare("cancel", (booking_id,), {}, idempotency_key)

    def prepare_reschedule(self, booking_id: str, body: m.RescheduleInput, idempotency_key: str) -> RequestIntent:
        return self._prepare("rescheduleBooking", (booking_id,), body, idempotency_key)

    def book(self, body: m.BookingInput, idempotency_key: str) -> m.Booking:
        return self.execute(self.prepare_book(body, idempotency_key))

    def cancel(self, booking_id: str, idempotency_key: str) -> m.Booking:
        return self.execute(self.prepare_cancel(booking_id, idempotency_key))

    def reschedule_booking(self, booking_id: str, body: m.RescheduleInput, idempotency_key: str) -> m.Booking:
        return self.execute(self.prepare_reschedule(booking_id, body, idempotency_key))

    def events(self, *, after: int = 0, limit: int = 100) -> m.Events:
        if type(after) is not int or not 0 <= after <= 9007199254740991:
            fail("invalid_cursor")
        if type(limit) is not int or not 1 <= limit <= 100:
            fail("invalid_limit")
        return self._call("events", query={"after": after, "limit": limit})

    def create_resource(self, body: m.ResourceInput) -> m.Resource:
        return self._call("createResource", body=body)

    def list_resources(self, *, after: str | None = None, limit: int = 25, q: str | None = None) -> m.ResourcePage:
        return self._call("listResources", query=self._page(after, limit, q))

    def get_resource(self, resource_id: str) -> m.Resource:
        return self._call("getResource", (resource_id,))

    def replace_resource(self, resource_id: str, body: m.ResourceInput) -> m.Resource:
        return self._call("updateResource", (resource_id,), body)

    def archive_resource(self, resource_id: str) -> m.Resource:
        return self._call("archiveResource", (resource_id,), {})

    def get_hours(self, resource_id: str) -> m.Hours:
        return self._call("getHours", (resource_id,))

    def replace_hours(self, resource_id: str, body: m.Hours) -> m.Hours:
        return self._call("setHours", (resource_id,), body)

    def create_offering(self, body: m.OfferingInput) -> m.Offering:
        return self._call("createOffering", body=body)

    def list_offerings(self, *, after: str | None = None, limit: int = 25, q: str | None = None) -> m.OfferingPage:
        return self._call("listOfferings", query=self._page(after, limit, q))

    def get_offering(self, offering_id: str) -> m.Offering:
        return self._call("getOffering", (offering_id,))

    def replace_offering(self, offering_id: str, body: m.OfferingInput) -> m.Offering:
        return self._call("updateOffering", (offering_id,), body)

    def archive_offering(self, offering_id: str) -> m.Offering:
        return self._call("archiveOffering", (offering_id,), {})

    def list_bookings(self, *, after: str | None = None, limit: int = 25) -> m.BookingPage:
        return self._call("listBookings", query=self._page(after, limit))

    def get_booking(self, booking_id: str) -> m.Booking:
        return self._call("getBooking", (booking_id,))

    def create_customer(self, body: m.CustomerInput) -> m.Customer:
        return self._call("createCustomer", body=body)

    def list_customers(self, *, after: str | None = None, limit: int = 25, q: str | None = None) -> m.CustomerPage:
        return self._call("listCustomers", query=self._page(after, limit, q))

    def get_customer(self, customer_id: str) -> m.Customer:
        return self._call("getCustomer", (customer_id,))

    def replace_customer(self, customer_id: str, body: m.CustomerInput) -> m.Customer:
        return self._call("replaceCustomer", (customer_id,), body)

    def archive_customer(self, customer_id: str) -> m.Customer:
        return self._call("archiveCustomer", (customer_id,), {})

    def erase_customer(self, customer_id: str) -> m.CustomerErased:
        """MANAGE+PRIVACY. Irreversible contact erasure, no replay guarantee."""
        return self._call("eraseCustomer", (customer_id,), {})

    def create_webhook(self, body: m.WebhookCreate) -> m.WebhookCreated:
        """MANAGE. Contains a once-only secret: persist privately, never log."""
        return self._call("createWebhook", body=body)

    def list_webhooks(self, *, after: str | None = None, limit: int = 25) -> m.WebhookPage:
        return self._call("listWebhooks", query=self._page(after, limit))

    def disable_webhook(self, subscription_id: str) -> m.Webhook:
        return self._call("disableWebhook", (subscription_id,), {})

    def list_webhook_deliveries(self, subscription_id: str, *, after: str | None = None, limit: int = 25) -> m.WebhookDeliveryPage:
        return self._call("listWebhookDeliveries", (subscription_id,), query=self._page(after, limit))

    def get_usage(self) -> m.Usage:
        return self._call("getUsage")

    def get_booking_page(self) -> m.BookingPageSettings:
        return self._call("getBookingPage")

    def replace_booking_page(self, body: m.BookingPageInput) -> m.BookingPageSettings:
        return self._call("replaceBookingPage", body=body)

    def prune_history(self, *, kind: Literal["deliveries", "events", "bookings"], limit: int) -> m.HistoryPruneResult:
        """PRIVACY. One bounded batch; no automatic repetition or replay key."""
        return self._call("pruneHistory", body={"kind": kind, "limit": limit})

    def prune_expired_replays(self, limit: int) -> m.MaintenanceResult:
        return self._call("pruneExpiredReplays", body={"limit": limit})


class PublicBookingClient(_BaseClient[m.PublicBooking]):
    """Public page access; private capabilities authorize individual bookings.

    This client accepts no workspace credential or account session. It does not
    change the API's browser origin/CORS policy or invent a native login flow.
    """

    _public = True

    def __init__(self, *, page_id: str, base_url: str = "https://api.availkit.com",
                 timeout: float = 2, max_response_bytes: int = 262144):
        self.page_id = identifier(page_id)
        self._transport = Transport(base_url, timeout, max_response_bytes)
        self._root = "/v1/public/pages/" + self.page_id
        self._scope = self._transport.base_url + ":public:" + self.page_id
        self._headers = {}

    @staticmethod
    def _access(token: str) -> str:
        valid = isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]", token)
        if not valid:
            fail("invalid_guest_access")
        return token

    def get_page(self) -> m.PublicPage:
        return self._call("getPublicPage")

    def list_offerings(self, *, after: str | None = None, limit: int = 25) -> m.PublicOfferingPage:
        return self._call("listPublicOfferings", query=self._page(after, limit))

    def availability(self, *, offering_id: str, from_: str, to: str) -> m.Availability:
        return self._call("publicAvailability", query=self._availability_query(offering_id, from_, to))

    def prepare_book(self, body: m.PublicBookingInput, idempotency_key: str, *, booking_token: str, contact_token: str) -> RequestIntent:
        return self._prepare("bookPublic", (), body, idempotency_key,
            {"Authorization": "Booking " + self._access(booking_token), "X-AvailKit-Contact-Token": self._access(contact_token)})

    def book(self, body: m.PublicBookingInput, idempotency_key: str, *, booking_token: str, contact_token: str) -> m.PublicBooking:
        return self.execute(self.prepare_book(body, idempotency_key, booking_token=booking_token, contact_token=contact_token))

    def get_booking(self, booking_id: str, *, booking_token: str) -> m.PublicBooking:
        return self._call("getPublicBooking", (booking_id,), extra_headers={"Authorization": "Booking " + self._access(booking_token)})

    def prepare_cancel(self, booking_id: str, idempotency_key: str, *, booking_token: str) -> RequestIntent:
        return self._prepare("cancelPublicBooking", (booking_id,), {}, idempotency_key,
            {"Authorization": "Booking " + self._access(booking_token)})

    def cancel(self, booking_id: str, idempotency_key: str, *, booking_token: str) -> m.PublicBooking:
        return self.execute(self.prepare_cancel(booking_id, idempotency_key, booking_token=booking_token))

    def prepare_reschedule(self, booking_id: str, body: m.RescheduleInput, idempotency_key: str, *, booking_token: str) -> RequestIntent:
        return self._prepare("reschedulePublicBooking", (booking_id,), body, idempotency_key,
            {"Authorization": "Booking " + self._access(booking_token)})

    def reschedule(self, booking_id: str, body: m.RescheduleInput, idempotency_key: str, *, booking_token: str) -> m.PublicBooking:
        return self.execute(self.prepare_reschedule(booking_id, body, idempotency_key, booking_token=booking_token))
