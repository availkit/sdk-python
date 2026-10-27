"""Run from a trusted backend. Use only an owned test offering with2+ slots.

python first_booking.py inspect
python first_booking.py create
python first_booking.py read
python first_booking.py move
python first_booking.py cancel

Unknown responses retain their intent. ``reconcile`` only reads. An intentional
``replay-create``, ``replay-move`` or ``replay-cancel`` sends the saved original
request once under the original key/scope; it is never triggered automatically.
"""

import argparse
import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from availkit import AvailKitClient, AvailKitError, RequestIntent, new_idempotency_key
from availkit.intents import read_private


def private_write(path: Path, value: Mapping[str, Any]) -> None:
    # Exclusive creation: a prior uncertain or confirmed result is never replaced.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())


def run(action: str) -> None:
    client = AvailKitClient.from_environment()
    if client.environment != "test":
        raise AvailKitError("input", "example_requires_test_environment")
    offering_id = os.environ["AVAILKIT_OFFERING_ID"]
    directory = Path(os.environ.get("AVAILKIT_INTENT_DIR", "./private/python-intents"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise AvailKitError("input", "private_directory_required")
    default_start = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(second=0, microsecond=0)
    from_ = os.environ.get("AVAILKIT_FROM", default_start.isoformat().replace("+00:00", "Z"))
    default_end = datetime.fromisoformat(from_.replace("Z", "+00:00")) + timedelta(days=7)
    to = os.environ.get("AVAILKIT_TO", default_end.isoformat().replace("+00:00", "Z"))
    if action == "inspect":
        resources = client.list_resources()
        slots = client.availability(offering_id=offering_id, from_=from_, to=to)["slots"]
        print(json.dumps({"resourceCount": len(resources["items"]), "availableSlots": len(slots)}))
        return
    if action == "reconcile":
        original = RequestIntent.load(directory / "create-intent.json")
        body = json.loads(original.body)
        # A matching row is a candidate, not proof of the original response.
        candidates = [b["id"] for page in client.pages(client.list_bookings, max_pages=100)
                      for b in page["items"] if b["offeringId"] == body["offeringId"] and datetime.fromisoformat(b["start"].replace("Z", "+00:00")) == datetime.fromisoformat(body["start"].replace("Z", "+00:00"))]
        print(json.dumps({"candidateBookingIds": candidates, "outcome": "inspect_before_explicit_replay"}))
        return
    if action.startswith("replay-"):
        operation = action.removeprefix("replay-")
        intent = RequestIntent.load(directory / f"{operation}-intent.json")
        booking = client.execute(intent)  # Explicit operator action, same bytes/key.
        result = directory / f"{operation}-result.json"
        if not result.exists():
            private_write(result, booking)
        print(json.dumps({"bookingId": booking["id"], "status": booking["status"]}))
        return
    if action == "create":
        # Refuse to create another intent when a prior send may have committed.
        path = directory / "create-intent.json"
        if path.exists():
            raise AvailKitError("input", "retained_intent_requires_reconciliation")
        slots = client.availability(offering_id=offering_id, from_=from_, to=to)["slots"]
        if len(slots) < 2:
            raise AvailKitError("input", "example_requires_two_available_slots")
        intent = client.prepare_book({"offeringId": offering_id, "start": slots[0]["start"]}, new_idempotency_key())
    else:
        created = json.loads(read_private(directory / "create-result.json", 65536, "create_result_required"))
        current = client.get_booking(created["id"])
        if action == "read":
            print(json.dumps({"bookingId": current["id"], "status": current["status"]}))
            return
        path = directory / f"{action}-intent.json"
        if path.exists():
            raise AvailKitError("input", "retained_intent_requires_reconciliation")
        if action == "move":
            slots = client.availability(offering_id=offering_id, from_=from_, to=to)["slots"]
            alternatives = [s for s in slots if s["start"] != current["start"]]
            if not alternatives:
                raise AvailKitError("input", "alternative_slot_required")
            intent = client.prepare_reschedule(current["id"], {
                "expectedStart": current["start"], "start": alternatives[0]["start"]
            }, new_idempotency_key())
        else:
            intent = client.prepare_cancel(current["id"], new_idempotency_key())
    intent.save(path)  # Fsync before the first send; keep after every outcome.
    booking = client.execute(intent)
    private_write(directory / f"{action}-result.json", booking)
    print(json.dumps({"bookingId": booking["id"], "status": booking["status"]}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Owned test booking lifecycle; secrets come from private files.")
    parser.add_argument("action", choices=["inspect", "create", "read", "move", "cancel", "reconcile", "replay-create", "replay-move", "replay-cancel"])
    action = parser.parse_args().action
    try:
        run(action)
    except AvailKitError as error:
        print(json.dumps(error.as_dict()), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Raw filesystem/environment/library errors can contain private paths/data.
        print(json.dumps({"code": "example_failed", "outcome": "inspect_retained_intent"}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
