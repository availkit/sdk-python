import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from availkit import AvailKitClient, AvailKitError, PublicBookingClient, RequestIntent, new_guest_token
from availkit.durable import FileOperationStore, SQLiteOperationStore, OperationStoreError

ID = "a" * 32
KEY = "synthetic-durable-original-key"
BODY = {"offeringId": "2" * 32, "start": "2030-01-07T09:00:00Z"}

def intent():
    return AvailKitClient(workspace_id="1" * 32, environment="test", credential="A" * 43).prepare_book(BODY, KEY)

def contend(kind, directory, original, barrier, queue):
    store = (FileOperationStore if kind == "file" else SQLiteOperationStore)(directory)
    barrier.wait(timeout=5)
    try:
        store.begin(ID, 0, RequestIntent.from_private(original))
        queue.put("won")
    except OperationStoreError as e:
        queue.put(e.code)
    finally:
        store.close()

class DurableTests(unittest.TestCase):
    def stores(self):
        return (FileOperationStore, SQLiteOperationStore)

    def test_original_cas_interruption_replay_and_terminal(self):
        for factory in self.stores():
            with self.subTest(factory=factory), tempfile.TemporaryDirectory() as directory:
                store = factory(directory)
                original = intent()
                record = store.create(ID, original)
                self.assertEqual(record.state, "prepared")
                self.assertEqual(store.create(ID, original).revision, 0)
                self.assertRaises(OperationStoreError, store.create, ID, replace(original, idempotency_key=KEY + "2"))
                admitted = store.begin(ID, 0, original)
                self.assertEqual((admitted.state, admitted.active, admitted.attempts), ("unknown", True, 1))
                store.close()
                store = factory(directory)
                row = store.read(ID)
                self.assertEqual(row.original().export_private(), original.export_private())
                with self.assertRaises(OperationStoreError):
                    store.begin(ID, row.revision, original, replay=True)
                released = store.release_interrupted(ID, row.revision, acknowledge_workers_joined=True)
                replay = store.begin(ID, released.revision, row.original(), replay=True)
                self.assertEqual(replay.attempts, 2)
                self.assertRaises(OperationStoreError, store.settle, ID, replay.revision, admitted.attempt_token(), "confirmed")
                done = store.settle(ID, replay.revision, replay.attempt_token(), "confirmed")
                self.assertFalse(done.active)
                self.assertEqual(done.state, "confirmed")
                self.assertRaises(OperationStoreError, store.begin, ID, done.revision, original, replay=True)
                self.assertNotIn(KEY, repr(done))
                self.assertNotIn(BODY["offeringId"], repr(done))
                store.close()

    def test_explicit_sdk_send_scope_and_exact_original_replay(self):
        for factory in self.stores():
            with self.subTest(factory=factory), tempfile.TemporaryDirectory() as directory:
                client = AvailKitClient(workspace_id="1" * 32, environment="test", credential="A" * 43)
                original = client.prepare_book(BODY, KEY)
                store = factory(directory)
                store.create(ID, original)
                calls = []
                def unknown(*args):
                    calls.append(args)
                    raise AvailKitError("transport", "request_failed")
                with patch.object(client, "_send", side_effect=unknown):
                    first = store.begin(ID, 0, original)
                    self.assertRaises(AvailKitError, client.execute, first.original())
                    self.assertEqual(len(calls), 1)
                    store.close()
                    store = factory(directory)
                    row = store.read(ID)
                    self.assertEqual(len(calls), 1)
                    released = store.release_interrupted(ID, row.revision, acknowledge_workers_joined=True)
                    replay = store.begin(ID, released.revision, row.original(), replay=True)
                    self.assertRaises(AvailKitError, client.execute, replay.original())
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[0], calls[1])
                    wrong = AvailKitClient(workspace_id="9" * 32, environment="test", credential="A" * 43)
                    with patch.object(wrong, "_send") as wrong_send:
                        self.assertRaises(AvailKitError, wrong.execute, replay.original())
                        wrong_send.assert_not_called()
                store.close()

    def test_public_originals_preserve_capabilities_privately_for_all_three_operations(self):
        token = new_guest_token()
        client = PublicBookingClient(page_id="1" * 32)
        originals = [client.prepare_book({**BODY, "contact": {"displayName": "Private fixture"}}, KEY, booking_token=token, contact_token=token),
            client.prepare_reschedule("3" * 32, {"expectedStart": BODY["start"], "start": "2030-01-07T10:00:00Z"}, KEY + "-move", booking_token=token),
            client.prepare_cancel("3" * 32, KEY + "-cancel", booking_token=token)]
        for factory in self.stores():
            with tempfile.TemporaryDirectory() as directory:
                store = factory(directory)
                for n, original in enumerate(originals):
                    row = store.create(f"{n:032x}", original)
                    self.assertEqual(row.original().export_private(), original.export_private())
                    self.assertNotIn(token, repr(row))
                    self.assertNotIn("Private fixture", repr(row))
                    self.assertTrue(all(token in value for _, value in row.original().headers))
                store.close()

    def test_sqlite_special_file_and_error_context_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            os.mkfifo(Path(directory) / "operations.sqlite", mode=0o600)
            with self.assertRaises(OperationStoreError) as caught:
                SQLiteOperationStore(directory)
            self.assertEqual(caught.exception.code, "invalid_storage")
            self.assertIsNone(caught.exception.__context__)
            self.assertIsNone(caught.exception.__cause__)
            self.assertNotIn(directory, str(caught.exception))

    def test_lost_original_then_replay_403_409_cannot_reject_logical_original(self):
        for factory in self.stores():
            for status in (403, 409):
                with self.subTest(factory=factory, status=status), tempfile.TemporaryDirectory() as directory:
                    client = AvailKitClient(workspace_id="1" * 32, environment="test", credential="A" * 43)
                    original = client.prepare_book(BODY, KEY)
                    serialized = original.export_private()
                    store = factory(directory)
                    store.create(ID, original)
                    calls = []
                    def response(*args):
                        calls.append(args)
                        if len(calls) == 1:
                            raise AvailKitError("transport", "request_failed")
                        return status, {"content-type": "application/json"}, {"error": {"code": "capability_denied" if status == 403 else "version_conflict", "message": "synthetic rejection"}}
                    with patch.object(client._transport, "request", side_effect=response):
                        first = store.begin(ID, 0, original)
                        with self.assertRaises(AvailKitError) as lost:
                            client.execute(original)
                        self.assertEqual(lost.exception.outcome, "unknown")
                        ready = store.settle(ID, first.revision, first.attempt_token(), "unknown")
                        replay = store.begin(ID, ready.revision, ready.original(), replay=True)
                        with self.assertRaises(AvailKitError) as rejected:
                            client.execute(replay.original())
                        self.assertEqual((rejected.exception.outcome, rejected.exception.status), ("rejected", status))
                        retained = store.settle(ID, replay.revision, replay.attempt_token(), "rejected")
                    self.assertEqual(retained.state, "unknown")
                    self.assertFalse(retained.active)
                    self.assertEqual(retained.original().export_private(), serialized)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[0], calls[1])
                    other = store.create("b" * 32, original)
                    initial = store.begin("b" * 32, other.revision, original)
                    self.assertEqual(store.settle("b" * 32, initial.revision, initial.attempt_token(), "rejected").state, "rejected")
                    store.close()

    def test_independent_workers_only_one_admission(self):
        for kind, factory in zip(("file", "sqlite"), self.stores()):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                original = intent()
                store = factory(directory)
                store.create(ID, original)
                store.close()
                ctx = multiprocessing.get_context("spawn")
                barrier, queue = ctx.Barrier(2), ctx.Queue()
                workers = [ctx.Process(target=contend, args=(kind, directory, original.export_private(), barrier, queue)) for _ in range(2)]
                try:
                    for worker in workers:
                        worker.start()
                    results = [queue.get(timeout=10) for _ in workers]
                    self.assertEqual(results.count("won"), 1)
                    self.assertTrue(set(results) <= {"won", "conflict", "busy"})
                finally:
                    for worker in workers:
                        worker.join(timeout=10)
                        if worker.is_alive():
                            worker.terminate()
                            worker.join()
                        self.assertEqual(worker.exitcode, 0)
                    queue.close()
                    queue.join_thread()
                store = factory(directory)
                self.assertEqual(store.read(ID).attempts, 1)
                store.close()

    def test_real_process_exit_after_durable_admission_never_sends_on_reopen(self):
        for factory in self.stores():
            with self.subTest(factory=factory), tempfile.TemporaryDirectory() as directory:
                store = factory(directory)
                original = intent()
                store.create(ID, original)
                original.save(Path(directory) / "input.json")
                store.close()
                code = "from availkit.durable import " + factory.__name__ + "; from availkit import RequestIntent; import os,sys; s=" + factory.__name__ + "(sys.argv[1]); s.begin('" + ID + "',0,RequestIntent.load(sys.argv[1]+'/input.json')); os._exit(17)"
                result = subprocess.run([sys.executable, "-c", code, directory], timeout=10, capture_output=True)
                self.assertEqual(result.returncode, 17)
                store = factory(directory)
                self.assertEqual((store.read(ID).state, store.read(ID).active), ("unknown", True))
                store.close()

    def test_scope_future_expiry_and_unknown_are_explicit(self):
        for factory in self.stores():
            for age in (-60, 86401):
                with self.subTest(factory=factory, age=age), tempfile.TemporaryDirectory() as directory:
                    original = replace(intent(), created_at=(datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
                    store = factory(directory)
                    store.create(ID, original)
                    with self.assertRaises(OperationStoreError) as caught:
                        store.begin(ID, 0, original)
                    self.assertEqual(caught.exception.code, "intent_window")
                    self.assertEqual(store.read(ID).state, "prepared")
                    store.close()
        with tempfile.TemporaryDirectory() as directory:
            store = FileOperationStore(directory)
            original = intent()
            store.create(ID, original)
            self.assertRaises(OperationStoreError, store.begin, ID, 0, replace(original, scope=original.scope.replace(":test:", ":live:")))
            self.assertEqual(store.read(ID).state, "prepared")
            store.close()

    def test_protected_storage_and_retained_pending(self):
        with tempfile.TemporaryDirectory() as outer:
            directory = Path(outer) / "private"
            directory.mkdir(mode=0o700)
            link = Path(outer) / "link"
            link.symlink_to(directory, target_is_directory=True)
            for factory in self.stores():
                self.assertRaises(OperationStoreError, factory, link)
            os.chmod(directory, 0o755)
            self.assertRaises(OperationStoreError, FileOperationStore, directory)
            os.chmod(directory, 0o700)
            store = FileOperationStore(directory)
            os.mkfifo(directory / (ID + ".json"), mode=0o600)
            self.assertRaises(OperationStoreError, store.read, ID)
            (directory / (ID + ".json")).unlink()
            (directory / ".lock").mkdir(mode=0o700)
            fd = os.open(directory / ".pending", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, b"private-interrupted-original")
            os.close(fd)
            with self.assertRaises(OperationStoreError) as caught:
                store.recover_lock(acknowledge_workers_joined=True)
            self.assertEqual(caught.exception.code, "repair_required")
            self.assertTrue((directory / ".pending").exists())
            self.assertTrue((directory / ".lock").exists())
            self.assertNotIn(str(directory), str(caught.exception))
            store.close()

    def test_capacity_and_malformed_record_fail_closed(self):
        for factory in self.stores():
            with self.subTest(factory=factory), tempfile.TemporaryDirectory() as directory:
                store = factory(directory)
                original = intent()
                for n in range(128):
                    store.create(f"{n:032x}", original)
                with self.assertRaises(OperationStoreError) as caught:
                    store.create(ID, original)
                self.assertEqual(caught.exception.code, "capacity")
                store.close()
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / (ID + ".json")
            fd = os.open(p, os.O_CREAT | os.O_WRONLY, 0o600)
            os.write(fd, b'{"secret":"do-not-reflect"}')
            os.close(fd)
            store = FileOperationStore(directory)
            with self.assertRaises(OperationStoreError) as caught:
                store.read(ID)
            self.assertEqual(caught.exception.code, "invalid_record")
            self.assertNotIn("do-not-reflect", str(caught.exception))
            store.close()
