"""Explicit durable owned-test create; prepare/send/inspect/reconcile/replay are separate."""
import argparse
import json
import os
import sys

from availkit import AvailKitClient, AvailKitError, new_idempotency_key
from availkit.durable import FileOperationStore, SQLiteOperationStore, OperationStoreError


def summary(row):
    return {"state": row.state, "revision": row.revision, "attempts": row.attempts, "active": row.active} if row else {"state": "absent"}


def run(action):
    identifier = os.environ.get("AVAILKIT_OPERATION_ID", "00000000000000000000000000000001")
    backend = os.environ.get("AVAILKIT_OPERATION_BACKEND", "file")
    if backend not in ("file", "sqlite") or os.environ.get("AVAILKIT_ENVIRONMENT") != "test":
        raise OperationStoreError("invalid_input")
    store = (FileOperationStore if backend == "file" else SQLiteOperationStore)(os.environ["AVAILKIT_OPERATION_DIR"])
    try:
        row = store.read(identifier)
        if action == "inspect":
            print(json.dumps(summary(row)))
            return
        if action == "acknowledge-interruption":
            # Explicit owner assertion: ALL previous workers must have stopped/joined.
            if os.environ.get("AVAILKIT_ACK_WORKERS_JOINED") != "yes" or row is None:
                raise OperationStoreError("invalid_input")
            row = store.release_interrupted(identifier, row.revision, acknowledge_workers_joined=True)
            print(json.dumps(summary(row)))
            return
        client = AvailKitClient.from_environment()
        if action == "prepare":
            if row is not None:
                raise OperationStoreError("conflict")
            original = client.prepare_book({"offeringId": os.environ["AVAILKIT_OFFERING_ID"], "start": os.environ["AVAILKIT_START"]}, new_idempotency_key())
            row = store.create(identifier, original)
            print(json.dumps(summary(row)))
            return
        if row is None:
            raise OperationStoreError("not_found")
        if action == "reconcile":
            body = json.loads(row.original().body)
            page = client.list_bookings(limit=100)
            candidates = sum(b["offeringId"] == body["offeringId"] and b["start"] == body["start"] for b in page["items"])
            # One bounded page; a current match never proves the original response.
            print(json.dumps({"candidateCount": candidates, "partial": page["nextAfter"] is not None, "outcome": "inspect_before_explicit_replay"}))
            return
        if action not in ("send", "replay"):
            raise OperationStoreError("invalid_input")
        original = row.original()
        attempt = store.begin(identifier, row.revision, original, replay=action == "replay")
        try:
            client.execute(original)  # Exactly one explicit send, after durable UNKNOWN.
        except AvailKitError as error:
            retained = store.settle(identifier, attempt.revision, attempt.attempt_token(), "rejected" if error.outcome == "rejected" else "unknown")
            # A rejected replay says nothing conclusive about the earlier unknown.
            print(json.dumps({"code": error.code, "attemptOutcome": error.outcome, "operationState": retained.state}), file=sys.stderr)
            raise SystemExit(1)
        row = store.settle(identifier, attempt.revision, attempt.attempt_token(), "confirmed")
        print(json.dumps(summary(row)))
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description="Durable owned test booking; never automatically retries.")
    parser.add_argument("action", choices=["prepare", "send", "inspect", "reconcile", "acknowledge-interruption", "replay"])
    action = parser.parse_args().action
    try:
        run(action)
    except (OperationStoreError, AvailKitError) as error:
        print(json.dumps({"code": error.code, "outcome": error.outcome if isinstance(error, AvailKitError) else "inspect_retained_operation"}), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print(json.dumps({"code": "example_failed", "outcome": "inspect_retained_operation"}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
