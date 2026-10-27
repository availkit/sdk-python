"""Local backend receiver behind an existing trusted TLS ingress.

python webhook_receiver.py binds 127.0.0.1:8090. Configure a public HTTPS webhook
destination at your ingress; never put the signing secret in a browser/mobile app.
AVAILKIT_WEBHOOK_SECRET_FILE points to an owned0600 file containing the once-only
signing secret. AVAILKIT_WEBHOOK_STATE_DIR is an owned0700 durable directory.
"""

import json
from contextlib import closing
import os
import re
import sqlite3
import stat
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from availkit import verify_webhook
from availkit.intents import read_private


class Receiver(BaseHTTPRequestHandler):
    # Set during startup; values never enter request logs or diagnostics.
    signing_secret: str
    database: Path
    workspace_id: str
    environment: str
    subscription_id: str

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.connection.settimeout(3)
        if self.path != "/webhooks/availkit":
            self.reply(404)
            return
        if (len(self.headers.get_all("AvailKit-Signature", [])) != 1
                or len(self.headers.get_all("AvailKit-Delivery-Id", [])) != 1):
            self.reply(401)
            return
        if len(self.headers.get_all("Content-Length", [])) != 1:
            self.reply(413)
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or not 1 <= int(length) <= 16384 or self.headers.get("Transfer-Encoding") is not None:
            self.reply(413)
            return
        try:
            raw = self.rfile.read(int(length))
            signature = self.headers.get("AvailKit-Signature", "")
            delivery_id = self.headers.get("AvailKit-Delivery-Id", "")
            if (len(raw) != int(length) or not re.fullmatch(r"[0-9a-f]{32}", delivery_id)
                    or not verify_webhook(raw, signature, self.signing_secret)):
                self.reply(401)
                return
            payload = json.loads(raw.decode("utf-8"))
            event = payload.get("event") if type(payload) is dict else None
            if (type(payload) is not dict or type(payload.get("schemaVersion")) is not int
                    or payload["schemaVersion"] != 1 or payload.get("deliveryId") != delivery_id
                    or payload.get("workspaceId") != self.workspace_id
                    or payload.get("environment") != self.environment
                    or payload.get("subscriptionId") != self.subscription_id
                    or type(event) is not dict or event.get("type") not in ("booking.created", "booking.rescheduled", "booking.cancelled")
                    or type(event.get("sequence")) is not int or event["sequence"] < 1):
                self.reply(400)
                return
            with closing(sqlite3.connect(self.database, timeout=3)) as db, db:
                # Local effect and dedupe share one transaction. A crash rolls both
                # back. For external effects use a transactional outbox keyed by
                # delivery_id; an INSERT followed by an HTTP call is not atomic.
                cursor = db.execute("INSERT OR IGNORE INTO deliveries(id) VALUES(?)", (delivery_id,))
                if cursor.rowcount:
                    db.execute("INSERT INTO handled(delivery_id,event_type) VALUES(?,?)", (delivery_id, event["type"]))
            self.reply(204)
        except Exception:
            #5xx allows the sender to retry the same stable delivery identity.
            self.reply(503)

    def reply(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def main() -> None:
    try:
        Receiver.signing_secret = read_private(os.environ["AVAILKIT_WEBHOOK_SECRET_FILE"], 4096, "invalid_credential_file").decode().strip()
        Receiver.workspace_id = os.environ["AVAILKIT_WORKSPACE_ID"]
        Receiver.environment = os.environ["AVAILKIT_ENVIRONMENT"]
        Receiver.subscription_id = os.environ["AVAILKIT_WEBHOOK_SUBSCRIPTION_ID"]
        if (not re.fullmatch(r"[0-9a-f]{32}", Receiver.workspace_id)
                or not re.fullmatch(r"[0-9a-f]{32}", Receiver.subscription_id)
                or Receiver.environment not in ("test", "live")):
            raise ValueError
        directory = Path(os.environ["AVAILKIT_WEBHOOK_STATE_DIR"])
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise ValueError
        Receiver.database = directory / "webhooks.sqlite"
        # Reject symlinks, shared files and foreign ownership before SQLite opens.
        try:
            info = Receiver.database.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_nlink != 1
                    or hasattr(os, "getuid") and info.st_uid != os.getuid()):
                raise ValueError
        except FileNotFoundError:
            os.close(os.open(Receiver.database, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                             | getattr(os, "O_NOFOLLOW", 0), 0o600))
        with closing(sqlite3.connect(Receiver.database)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY)")
            db.execute("CREATE TABLE IF NOT EXISTS handled(delivery_id TEXT PRIMARY KEY,event_type TEXT NOT NULL)")
        print("Webhook receiver listening on 127.0.0.1:8090", flush=True)
        HTTPServer(("127.0.0.1", 8090), Receiver).serve_forever()
    except KeyboardInterrupt:
        return
    except Exception:
        print("Webhook receiver failed; inspect private configuration.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
