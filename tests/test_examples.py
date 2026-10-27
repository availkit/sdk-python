"""Execute the shipped tutorial modules against explicit loopback API fixtures."""

import contextlib
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from availkit import AvailKitError
from availkit.examples import first_booking, webhook_receiver
from test_sdk import BOOKING, BODY, KEY, LATER, OFF, RID, STAMP, WS, reply, server


class ExampleTests(unittest.TestCase):
    def test_tutorial_inspect_create_read_move_cancel_reconcile(self):
        current = dict(BOOKING)
        calls = []
        def handle(req):
            body = req.rfile.read(int(req.headers.get("Content-Length", "0")))
            calls.append((req.command, req.path, body))
            if "/resources?" in req.path:
                reply(req, {"items": [{"id": RID, "name": "Owned fixture", "timeZone": "UTC", "capacity": 1, "archived": False}], "nextAfter": None})
            elif "/availability?" in req.path:
                reply(req, {"slots": [
                    {"start": STAMP, "end": "2030-01-07T09:30:00Z", "remainingCapacity": 1},
                    {"start": LATER, "end": "2030-01-07T10:30:00Z", "remainingCapacity": 1}]})
            elif req.command == "POST" and req.path.endswith("/bookings"):
                reply(req, current, 201)
            elif req.path.endswith("/reschedule"):
                current.update(start=LATER, end="2030-01-07T10:30:00Z")
                reply(req, current)
            elif req.path.endswith("/cancel"):
                current["status"] = "cancelled"
                reply(req, current)
            elif "/bookings?" in req.path:
                reply(req, {"items": [current], "nextAfter": None})
            else:
                reply(req, current)
        with tempfile.TemporaryDirectory() as tmp, server(handle) as url:
            key_path = Path(tmp)/"key"
            key_path.write_text(KEY)
            key_path.chmod(0o600)
            env = {"AVAILKIT_BASE_URL": url, "AVAILKIT_WORKSPACE_ID": WS,
                   "AVAILKIT_ENVIRONMENT": "test", "AVAILKIT_CREDENTIAL_FILE": str(key_path),
                   "AVAILKIT_OFFERING_ID": OFF, "AVAILKIT_FROM": STAMP,
                   "AVAILKIT_TO": "2030-01-07T11:00:00Z", "AVAILKIT_INTENT_DIR": str(Path(tmp)/"intents")}
            with patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
                for action in ("inspect", "create", "read", "move", "cancel", "reconcile"):
                    first_booking.run(action)
                before = len(calls)
                with self.assertRaises(AvailKitError):
                    first_booking.run("create")
                self.assertEqual(len(calls), before)
                self.assertNotIn(KEY, out.getvalue())
            intents = list((Path(tmp)/"intents").glob("*-intent.json"))
            self.assertEqual(len(intents), 3)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in intents))
        self.assertEqual(sum(method == "POST" for method, _, _ in calls), 3)
        self.assertEqual(current["status"], "cancelled")

    def test_webhook_receiver_verifies_and_deduplicates_atomically(self):
        import base64
        raw_key = bytes(range(32))
        body = json.dumps({"schemaVersion":1,"deliveryId":"a"*32,"workspaceId":WS,"environment":"test","subscriptionId":"b"*32,"event":{"id":"c"*32,"sequence":1,"type":"booking.created","bookingId":"d"*32,"occurredAt":STAMP}},separators=(",",":")).encode()
        now = int(time.time())
        signature = f"t={now},v1=" + hmac.new(raw_key,str(now).encode()+b"."+body,hashlib.sha256).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            receiver = webhook_receiver.Receiver
            receiver.signing_secret = base64.urlsafe_b64encode(raw_key).decode().rstrip("=")
            receiver.workspace_id = WS
            receiver.environment = "test"
            receiver.subscription_id = "b"*32
            receiver.database = Path(tmp)/"state.sqlite"
            with contextlib.closing(sqlite3.connect(receiver.database)) as db, db:
                db.execute("CREATE TABLE deliveries(id TEXT PRIMARY KEY)")
                db.execute("CREATE TABLE handled(delivery_id TEXT PRIMARY KEY,event_type TEXT NOT NULL)")
            http = ThreadingHTTPServer(("127.0.0.1", 0), receiver)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                for value, status in ((signature,204),(signature,204),(signature+"x",401)):
                    connection = HTTPConnection("127.0.0.1",http.server_port,timeout=2)
                    connection.request("POST","/webhooks/availkit",body=body,headers={"AvailKit-Signature":value,"AvailKit-Delivery-Id":"a"*32})
                    response = connection.getresponse()
                    self.assertEqual(response.status,status)
                    response.read()
                    connection.close()
                # Duplicate signature/delivery headers reject before dedup/effect,
                # even if the first value is a valid real HMAC.
                for duplicate in ("AvailKit-Signature", "AvailKit-Delivery-Id"):
                    connection = HTTPConnection("127.0.0.1", http.server_port, timeout=2)
                    connection.putrequest("POST", "/webhooks/availkit")
                    connection.putheader("Content-Length", str(len(body)))
                    connection.putheader("AvailKit-Signature", signature)
                    connection.putheader("AvailKit-Delivery-Id", "a" * 32)
                    connection.putheader(duplicate, signature if duplicate.endswith("Signature") else "a" * 32)
                    connection.endheaders(body)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 401)
                    response.read()
                    connection.close()
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=2)
            with contextlib.closing(sqlite3.connect(receiver.database)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM handled").fetchone()[0],1)
                self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0],1)
