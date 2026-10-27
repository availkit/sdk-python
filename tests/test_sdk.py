import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from availkit import AvailKitClient, AvailKitError, PublicBookingClient, RequestIntent, new_guest_token, verify_webhook
from availkit._validation import OPERATIONS, SCHEMAS, validate

WS = "1" * 32
OFF = "2" * 32
BID = "3" * 32
RID = "4" * 32
KEY = "A" * 43
STAMP = "2030-01-07T09:00:00Z"
END = "2030-01-07T09:30:00Z"
LATER = "2030-01-07T10:00:00Z"
BODY = {"offeringId": OFF, "start": STAMP}
BOOKING = {**BODY, "id": BID, "resourceId": RID, "end": END, "status": "confirmed"}
INTENT = "synthetic-stable-intent"


def client(**options):
    return AvailKitClient(workspace_id=WS, environment="test", credential=KEY, **options)


@contextmanager
def server(handler):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            handler(self)

        def do_POST(self):
            handler(self)

        def do_PUT(self):
            handler(self)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http.daemon_threads = True
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}"
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def reply(req, data, status=200, cache="no-store", media="application/json"):
    encoded = json.dumps(data).encode()
    req.send_response(status)
    req.send_header("Content-Type", media)
    req.send_header("Cache-Control", cache)
    req.send_header("Content-Length", str(len(encoded)))
    req.end_headers()
    req.wfile.write(encoded)


class SDKTests(unittest.TestCase):
    def test_stale_or_future_intent_is_retained_and_never_dispatched(self):
        c = client()
        c._transport.request = lambda *args: self.fail("expired intent must not send")
        original = c.prepare_book(dict(BODY),INTENT)
        for delta, expected in ((timedelta(hours=-24,seconds=-1),"intent_expired"),(timedelta(hours=1),"invalid_intent_timestamp")):
            stamp = (datetime.now(timezone.utc)+delta).isoformat(timespec="milliseconds").replace("+00:00","Z")
            retained = replace(original,created_at=stamp)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/"intent.json"
                retained.save(path)
                loaded = RequestIntent.load(path)
                self.assertEqual(loaded.created_at,stamp)
                with self.assertRaises(AvailKitError) as found:
                    c.execute(loaded)
                self.assertEqual(found.exception.code,expected)
                self.assertEqual(found.exception.outcome,"not_sent")
                self.assertEqual(loaded.body,original.body)
                self.assertTrue(path.exists())
    def test_every_workspace_operation_matches_the_frozen_route_and_schema(self):
        c = client()
        resource = {"id": RID, "name": "Room", "timeZone": "UTC", "capacity": 1, "archived": False}
        resource_input = {k:resource[k] for k in ("name","timeZone","capacity")}
        offering = {"id":OFF,"name":"Visit","resourceId":RID,"durationMinutes":30,"archived":False}
        offering_input = {k:offering[k] for k in ("name","resourceId","durationMinutes")}
        customer = {"id":BID,"displayName":"Synthetic contact","email":None,"phone":None,"archived":False}
        hook = {"id":BID,"url":"https://hooks.example.com/availkit","enabled":True,"createdAt":STAMP}
        settings = {"pageId":WS,"title":"Bookings","enabled":True,"needsRepublish":False}
        hours = {"windows":[{"weekday":1,"start":"09:00","end":"17:00"}]}
        page = lambda value:{"items":[value],"nextAfter":None}
        usage = {"asOf":STAMP,"period":{"start":"2030-01-01T00:00:00Z","end":"2030-02-01T00:00:00Z"},"counters":{
            k:{"used":0,"limit":10,"remaining":10} for k in
            ("monthlyBookingCreates","catalogObjects","bookingRecords","committedEvents","webhookSubscriptions","webhookDeliveries","liveReplays","customerRecords")}}
        cases = [
            ("createResource",lambda:c.create_resource(resource_input),resource),
            ("listResources",lambda:c.list_resources(),page(resource)),
            ("setHours",lambda:c.replace_hours(RID,hours),hours),
            ("getHours",lambda:c.get_hours(RID),hours),
            ("createOffering",lambda:c.create_offering(offering_input),offering),
            ("listOfferings",lambda:c.list_offerings(),page(offering)),
            ("availability",lambda:c.availability(offering_id=OFF,from_=STAMP,to=LATER),{"slots":[]}),
            ("book",lambda:c.book(BODY,INTENT),BOOKING),
            ("listBookings",lambda:c.list_bookings(),page(BOOKING)),
            ("cancel",lambda:c.cancel(BID,INTENT),{**BOOKING,"status":"cancelled"}),
            ("events",lambda:c.events(),{"events":[],"nextAfter":0,"retentionFloor":0,"highWater":0}),
            ("getResource",lambda:c.get_resource(RID),resource),
            ("updateResource",lambda:c.replace_resource(RID,resource_input),resource),
            ("getOffering",lambda:c.get_offering(OFF),offering),
            ("updateOffering",lambda:c.replace_offering(OFF,offering_input),offering),
            ("getBooking",lambda:c.get_booking(BID),BOOKING),
            ("createWebhook",lambda:c.create_webhook({"url":hook["url"]}),{**hook,"signingSecret":KEY}),
            ("listWebhooks",lambda:c.list_webhooks(),page(hook)),
            ("disableWebhook",lambda:c.disable_webhook(BID),{**hook,"enabled":False}),
            ("listWebhookDeliveries",lambda:c.list_webhook_deliveries(BID),{"items":[],"nextAfter":None}),
            ("getUsage",lambda:c.get_usage(),usage),
            ("rescheduleBooking",lambda:c.reschedule_booking(BID,{"expectedStart":STAMP,"start":STAMP},INTENT),BOOKING),
            ("archiveResource",lambda:c.archive_resource(RID),{**resource,"archived":True}),
            ("archiveOffering",lambda:c.archive_offering(OFF),{**offering,"archived":True}),
            ("pruneHistory",lambda:c.prune_history(kind="events",limit=1),{"asOf":"2030-01-31T00:00:00Z","cutoff":"2030-01-01T00:00:00Z","deleted":0,"hasMore":False}),
            ("createCustomer",lambda:c.create_customer({"displayName":customer["displayName"]}),customer),
            ("listCustomers",lambda:c.list_customers(),page(customer)),
            ("getCustomer",lambda:c.get_customer(BID),customer),
            ("replaceCustomer",lambda:c.replace_customer(BID,{"displayName":customer["displayName"]}),customer),
            ("archiveCustomer",lambda:c.archive_customer(BID),{**customer,"archived":True}),
            ("eraseCustomer",lambda:c.erase_customer(BID),{"id":BID,"erased":True}),
            ("pruneExpiredReplays",lambda:c.prune_expired_replays(1),{"asOf":STAMP,"deleted":0,"hasMore":False}),
            ("getBookingPage",lambda:c.get_booking_page(),settings),
            ("replaceBookingPage",lambda:c.replace_booking_page({"title":"Bookings","enabled":True}),settings),
        ]
        self.assertEqual(len(cases),34)
        for name, action, output in cases:
            with self.subTest(operation=name):
                seen=[]
                spec=OPERATIONS[name]
                def send(method,path,body,headers):
                    seen.append((method,path,body,headers))
                    return spec["status"],{"cache-control":"no-store"},output
                c._transport.request=send
                self.assertEqual(action(),output)
                self.assertEqual(len(seen),1)
                method,path,body,headers=seen[0]
                self.assertEqual(method,spec["method"])
                expected=spec["path"].replace("{workspaceId}",WS).replace("{resourceId}",RID).replace("{offeringId}",OFF).replace("{bookingId}",BID).replace("{customerId}",BID).replace("{subscriptionId}",BID)
                self.assertEqual(path.split("?",1)[0],expected)
                if spec["input"] is None:
                    self.assertIsNone(body)
                else:
                    self.assertTrue(validate(json.loads(body),spec["input"]))
                self.assertEqual(headers.get("Idempotency-Key"),INTENT if name in ("book","cancel","rescheduleBooking") else None)

    def test_real_http_lifecycle_and_explicit_identical_replay(self):
        requests = []
        current = dict(BOOKING)
        def handle(req):
            payload = req.rfile.read(int(req.headers.get("Content-Length", "0")))
            requests.append((req.command, req.path, payload, req.headers.get("Idempotency-Key")))
            self.assertEqual(req.headers["Authorization"], "Bearer " + KEY)
            self.assertIsNone(req.headers.get("Cookie"))
            if req.path.endswith("/availability?offeringId=" + OFF + "&from=2030-01-07T09%3A00%3A00Z&to=2030-01-07T11%3A00%3A00Z"):
                reply(req, {"slots": [{"start": STAMP, "end": END, "remainingCapacity": 1}]})
            elif req.command == "POST" and req.path.endswith("/bookings"):
                reply(req, current, 201)
            elif req.path.endswith("/reschedule"):
                current.update(start=LATER, end="2030-01-07T10:30:00Z")
                reply(req, current)
            elif req.path.endswith("/cancel"):
                current["status"] = "cancelled"
                reply(req, current)
            else:
                reply(req, current)
        with server(handle) as url:
            c = client(base_url=url)
            self.assertEqual(len(c.availability(offering_id=OFF, from_=STAMP, to="2030-01-07T11:00:00Z")["slots"]), 1)
            body = dict(BODY)
            intent = c.prepare_book(body, INTENT)
            body["start"] = LATER
            self.assertEqual(c.execute(intent)["id"], BID)
            self.assertEqual(c.execute(intent)["id"], BID)
            self.assertEqual(c.get_booking(BID)["start"], STAMP)
            self.assertEqual(c.reschedule_booking(BID, {"expectedStart": STAMP, "start": LATER}, "another-stable-intent")["start"], LATER)
            self.assertEqual(c.cancel(BID, "cancel-stable-intent")["status"], "cancelled")
        self.assertEqual(len(requests), 6)
        self.assertEqual(requests[1], requests[2])
        self.assertEqual(json.loads(requests[1][2]), BODY)

    def test_rejected_unknown_and_secret_redaction(self):
        for status, code, outcome in [(403,"scope_denied","rejected"), (404,"not_found","rejected"), (503,"unavailable","unknown"), (400,KEY,"unknown")]:
            c = client()
            calls = []
            def transport(*args):
                calls.append(args)
                return status, {}, {"error": {"code": code, "message": KEY + " private-url"}}
            c._transport.request = transport
            with self.assertRaises(AvailKitError) as found:
                c.book(dict(BODY), INTENT)
            e = found.exception
            self.assertEqual(e.outcome, outcome)
            self.assertEqual(len(calls), 1)
            self.assertNotIn(KEY, str(e) + repr(e) + json.dumps(e.as_dict()))
            self.assertIsNone(e.__context__)
            self.assertIsNone(e.__cause__)

    def test_lost_response_is_one_send_with_original_retained(self):
        count = []
        def handle(req):
            count.append(req.rfile.read(int(req.headers["Content-Length"])))
            req.connection.shutdown(socket.SHUT_RDWR)
            req.connection.close()
        with server(handle) as url:
            c = client(base_url=url)
            intent = c.prepare_book(dict(BODY), INTENT)
            with self.assertRaises(AvailKitError) as found:
                c.execute(intent)
            self.assertEqual(found.exception.outcome, "unknown")
            self.assertEqual(found.exception.kind, "transport")
            self.assertIsNone(found.exception.__context__)
            self.assertEqual(json.loads(intent.body), BODY)
            self.assertEqual(len(count), 1)

    def test_total_deadline_including_drip_headers_and_body(self):
        for mode in ("headers", "body"):
            def handle(req):
                try:
                    if mode == "headers":
                        req.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Slow: ")
                    else:
                        req.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n")
                    for _ in range(20):
                        req.connection.sendall(b" ")
                        time.sleep(.03)
                except OSError:
                    pass
            with server(handle) as url:
                started = time.monotonic()
                with self.assertRaises(AvailKitError) as found:
                    client(base_url=url, timeout=.12).get_booking(BID)
                self.assertEqual(found.exception.code, "timeout")
                self.assertLess(time.monotonic() - started, .4)

    def test_dns_deadline_never_sends_after_timeout(self):
        resolver = socket.getaddrinfo
        sent = []
        def slow(*args, **kwargs):
            time.sleep(.15)
            return resolver(*args, **kwargs)
        with patch("socket.getaddrinfo", slow), patch("http.client.HTTPConnection.request", lambda *a, **kw: sent.append(1)):
            with self.assertRaises(AvailKitError) as found:
                client(base_url="http://127.0.0.1:1", timeout=.03).book(dict(BODY), INTENT)
            self.assertEqual(found.exception.code, "timeout")
            time.sleep(.2)
        self.assertEqual(sent, [])

    def test_redirect_oversize_json_and_status_are_unknown(self):
        for kind in ("redirect", "oversize", "wrong_status", "extra_field", "bad_json"):
            calls = []
            def handle(req):
                calls.append(req.path)
                req.rfile.read(int(req.headers.get("Content-Length", "0")))
                if kind == "redirect":
                    req.send_response(302)
                    req.send_header("Location", "https://secret.invalid/" + KEY)
                    req.end_headers()
                elif kind == "bad_json":
                    req.send_response(201)
                    req.send_header("Content-Type", "application/json")
                    req.end_headers()
                    req.wfile.write(b'{"id":1,"id":2}')
                else:
                    reply(req, {**BOOKING, **({"signingSecret": KEY} if kind == "extra_field" else {})}, 200 if kind == "wrong_status" else 201)
            with server(handle) as url:
                with self.assertRaises(AvailKitError) as found:
                    client(base_url=url, max_response_bytes=10 if kind == "oversize" else 1048576).book(dict(BODY), INTENT)
                self.assertEqual(found.exception.outcome, "unknown")
                self.assertNotIn(KEY, str(found.exception))
                self.assertEqual(len(calls), 1)

    def test_configuration_and_input_rejection_before_send(self):
        for url in ("http://example.com", "https://user:password@api.availkit.com", "https://api.availkit.com/path", "https://api.availkit.com/?key=" + KEY, "https://api.availkit.com:" + KEY):
            with self.assertRaises(AvailKitError) as found:
                client(base_url=url)
            self.assertEqual(found.exception.outcome, "not_sent")
            self.assertNotIn("password", str(found.exception))
            self.assertIsNone(found.exception.__context__)
        c = client()
        c._transport.request = lambda *args: self.fail("must not send")
        for action in (lambda: c.book({**BODY, "secret": KEY}, INTENT),
                       lambda: c.book(dict(BODY), "short"),
                       lambda: c.list_resources(limit=0),
                       lambda: c.list_resources(after="../"),
                       lambda: c.create_customer({"displayName": " ", "email": KEY}),
                       lambda: c.availability(offering_id=OFF, from_=STAMP, to="2030-02-30T00:00:00Z"),
                       lambda: c.replace_hours(RID,{"windows":[{"weekday":1,"start":"25:00","end":"26:00"}]})):
            with self.assertRaises(AvailKitError) as found:
                action()
            self.assertEqual(found.exception.outcome, "not_sent")

    def test_private_files_immutable_scope_and_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            secret = directory / "credential"
            secret.write_text(KEY + "\n")
            secret.chmod(0o600)
            c = AvailKitClient(workspace_id=WS, environment="test", credential_file=secret)
            intent = c.prepare_book(dict(BODY), INTENT)
            path = directory / "intent.json"
            intent.save(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(KEY, path.read_text())
            loaded = RequestIntent.load(path)
            self.assertEqual(loaded, intent)
            for action in (lambda: intent.save(path), lambda: client().execute(RequestIntent(intent.operation,intent.path,intent.body,intent.idempotency_key,"foreign")),
                           lambda: AvailKitClient(workspace_id=WS, environment="live", credential=KEY).execute(intent)):
                with self.assertRaises(AvailKitError):
                    action()
            secret.chmod(0o644)
            with self.assertRaises(AvailKitError):
                AvailKitClient(workspace_id=WS, environment="test", credential_file=secret)
            secret.chmod(0o600)
            (directory / "link").symlink_to(secret)
            with self.assertRaises(AvailKitError):
                AvailKitClient(workspace_id=WS, environment="test", credential_file=directory / "link")
            self.assertNotIn(KEY, repr(c) + repr(intent))

    def test_pagination_cursor_and_budget(self):
        c = client()
        row = {"id": RID, "name": "Room", "timeZone": "UTC", "capacity": 1, "archived": False}
        for data in ({"items": [row,row], "nextAfter": None}, {"items":[row],"nextAfter":RID}):
            c._transport.request = lambda *args: (200, {}, data)
            with self.assertRaises(AvailKitError):
                c.list_resources()
        def read(**q):
            return {"items":[row],"nextAfter":RID}
        iterator = c.pages(read, limit=1,max_pages=1)
        self.assertEqual(next(iterator)["nextAfter"], RID)
        with self.assertRaises(AvailKitError) as found:
            next(iterator)
        self.assertEqual(found.exception.code,"page_budget_exhausted")

    def test_public_guest_authority_and_persisted_intent(self):
        token = new_guest_token()
        guest = {"id":BID,"start":STAMP,"end":END,"status":"confirmed",
            "page":{"id":WS,"title":"Test bookings","environment":"test"},
            "offering":{"id":OFF,"name":"Visit","durationMinutes":30,"timeZone":"UTC"},"pageEnabled":True}
        seen = []
        def handle(req):
            payload = req.rfile.read(int(req.headers.get("Content-Length","0")))
            seen.append((req.command,req.path,payload))
            self.assertEqual(req.headers.get("Authorization"),"Booking " + token)
            self.assertIsNone(req.headers.get("Cookie"))
            if req.command == "POST" and req.path.endswith("/bookings"):
                self.assertEqual(req.headers.get("X-AvailKit-Contact-Token"),token)
                reply(req,guest,201)
            elif req.path.endswith("/cancel"):
                reply(req,{**guest,"status":"cancelled"})
            elif req.path.endswith("/reschedule"):
                reply(req,{**guest,"start":LATER,"end":"2030-01-07T10:30:00Z"})
            else:
                reply(req,guest)
        with server(handle) as url:
            c = PublicBookingClient(page_id=WS,base_url=url)
            intent = c.prepare_book({**BODY,"contact":{"displayName":"Synthetic guest"}},INTENT,booking_token=token,contact_token=token)
            self.assertEqual(c.execute(intent)["id"],BID)
            self.assertEqual(c.get_booking(BID,booking_token=token)["id"],BID)
            self.assertEqual(c.reschedule(BID,{"expectedStart":STAMP,"start":LATER},INTENT,booking_token=token)["start"],LATER)
            self.assertEqual(c.cancel(BID,INTENT,booking_token=token)["status"],"cancelled")
        self.assertEqual(len(seen),4)

    def test_webhook_signature_exact_bytes_age_and_constant_vector(self):
        secret = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
        payload = b'{"type":"booking.created"}'
        stamp = 1893456000
        digest = hmac.new(bytes(range(32)),str(stamp).encode()+b"."+payload,hashlib.sha256).hexdigest()
        signature = f"t={stamp},v1={digest}"
        self.assertTrue(verify_webhook(payload,signature,secret,now=stamp))
        self.assertFalse(verify_webhook(payload+b" ",signature,secret,now=stamp))
        self.assertFalse(verify_webhook(payload,signature,secret,now=stamp+301))
        self.assertFalse(verify_webhook(payload,signature,secret,now=stamp-301))
        self.assertFalse(verify_webhook(payload,signature+",v1="+digest,secret,now=stamp))
        large = b" " * 16385
        large_signature = f"t={stamp},v1=" + hmac.new(bytes(range(32)),str(stamp).encode()+b"."+large,hashlib.sha256).hexdigest()
        self.assertFalse(verify_webhook(large,large_signature,secret,now=stamp))

    def test_contract_coverage_matches_all_supported_operations(self):
        path = Path(__file__).resolve().parents[1]/"coverage.json"
        if path.exists():
            coverage = json.loads(path.read_text())
            self.assertEqual(coverage["workspace_operations"],34)
            self.assertEqual(coverage["public_operations"],7)
            for op, entry in coverage["operations"].items():
                if entry["status"] == "supported":
                    klass = PublicBookingClient if entry["client"] == "PublicBookingClient" else AvailKitClient
                    self.assertTrue(callable(getattr(klass,entry["sdk"])),op)
                    self.assertIn(op, OPERATIONS)
        self.assertEqual(len(OPERATIONS),41)


if __name__ == "__main__":
    unittest.main()
