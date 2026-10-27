"""Typed API shapes generated from the supported OpenAPI schemas.
Wire field names intentionally retain their canonical camelCase spelling.
Regenerate with tools/generate_contract.py from a repository checkout.
"""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

class ResourceInput(TypedDict):
    name: str
    timeZone: str
    capacity: int


class Resource(TypedDict):
    id: str
    name: str
    timeZone: str
    capacity: int
    archived: bool


class ResourcePage(TypedDict):
    items: list[Resource]
    nextAfter: str | None


class Hours(TypedDict):
    windows: list[HoursWindowsItem]


class OfferingInput(TypedDict):
    name: str
    resourceId: str
    durationMinutes: int


class Offering(TypedDict):
    id: str
    name: str
    resourceId: str
    durationMinutes: int
    archived: bool


class OfferingPage(TypedDict):
    items: list[Offering]
    nextAfter: str | None


class Availability(TypedDict):
    slots: list[AvailabilitySlotsItem]


class BookingInput(TypedDict):
    offeringId: str
    start: str
    customerId: NotRequired[str]


class Booking(TypedDict):
    id: str
    offeringId: str
    resourceId: str
    start: str
    end: str
    status: Literal['confirmed', 'cancelled']
    customerId: NotRequired[str]


class BookingPage(TypedDict):
    items: list[Booking]
    nextAfter: str | None


class Events(TypedDict):
    events: list[EventsEventsItem]
    nextAfter: int
    retentionFloor: int
    highWater: int


class WebhookCreate(TypedDict):
    url: str


class WebhookCreated(TypedDict):
    id: str
    url: str
    enabled: bool
    createdAt: str
    signingSecret: str


class WebhookPage(TypedDict):
    items: list[Webhook]
    nextAfter: str | None


class Webhook(TypedDict):
    id: str
    url: str
    enabled: bool
    createdAt: str


class WebhookDisable(TypedDict):
    pass


class WebhookDeliveryPage(TypedDict):
    items: list[WebhookDelivery]
    nextAfter: str | None


class WebhookDelivery(TypedDict):
    id: str
    subscriptionId: str
    eventId: str
    state: Literal['pending', 'suppressed', 'leased', 'sending', 'delivered', 'dead']
    createdAt: str
    attempts: int
    maxAttempts: Literal[5]
    nextAttemptAt: str | None
    leaseExpiresAt: str | None
    lastAttemptAt: str | None
    lastOutcome: Literal['admitted_unknown', 'claim_expired', 'lease_expired_unknown', 'disabled_unknown', 'not_attempted', 'invalid_target', 'invalid_delivery', 'address_denied', 'transport_unknown', 'deadline_unknown', 'http_drained', 'http_body_failed', 'http_body_deadline', 'http_body_too_large'] | None
    lastHttpStatus: int | None


class Usage(TypedDict):
    asOf: str
    period: UsagePeriod
    counters: UsageCounters


class UsagePeriod(TypedDict):
    start: str
    end: str


class UsageCounters(TypedDict):
    monthlyBookingCreates: UsageCounter
    catalogObjects: UsageCounter
    bookingRecords: UsageCounter
    committedEvents: UsageCounter
    webhookSubscriptions: UsageCounter
    webhookDeliveries: UsageCounter
    liveReplays: UsageCounter
    customerRecords: UsageCounter


class UsageCounter(TypedDict):
    used: int
    limit: int
    remaining: int


class RescheduleInput(TypedDict):
    expectedStart: str
    start: str


class HistoryPruneInput(TypedDict):
    kind: Literal['deliveries', 'events', 'bookings']
    limit: int


class HistoryPruneResult(TypedDict):
    asOf: str
    cutoff: str
    deleted: int
    hasMore: bool


class CustomerInput(TypedDict):
    displayName: str
    email: NotRequired[str | None]
    phone: NotRequired[str | None]


class Customer(TypedDict):
    id: str
    displayName: str
    email: str | None
    phone: str | None
    archived: bool
    erased: NotRequired[Literal[True]]


class CustomerPage(TypedDict):
    items: list[Customer]
    nextAfter: str | None


class Empty(TypedDict):
    pass


class CustomerErased(TypedDict):
    id: str
    erased: Literal[True]


class MaintenanceInput(TypedDict):
    limit: int


class MaintenanceResult(TypedDict):
    asOf: str
    deleted: int
    hasMore: bool


class PublicPage(TypedDict):
    id: str
    title: str
    environment: Literal['test', 'live']
    emailNotifications: NotRequired[bool]


class PublicOfferingPage(TypedDict):
    items: list[PublicOffering]
    nextAfter: str | None


class PublicOffering(TypedDict):
    id: str
    name: str
    durationMinutes: int
    timeZone: str


class PublicBookingInput(TypedDict):
    offeringId: str
    start: str
    contact: CustomerInput


class PublicBooking(TypedDict):
    id: str
    start: str
    end: str
    status: Literal['confirmed', 'cancelled']
    offering: PublicOffering
    page: PublicPage
    pageEnabled: bool


class BookingPageSettings(TypedDict):
    pageId: str | None
    title: str
    enabled: bool
    needsRepublish: bool


class BookingPageInput(TypedDict):
    title: str
    enabled: bool


class HoursWindowsItem(TypedDict):
    weekday: int
    start: str
    end: str


class AvailabilitySlotsItem(TypedDict):
    start: str
    end: str
    remainingCapacity: int


class EventsEventsItem(TypedDict):
    id: str
    sequence: int
    type: Literal['booking.created', 'booking.cancelled', 'booking.rescheduled']
    bookingId: str
    occurredAt: str
