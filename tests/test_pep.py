import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

NOW = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)


class PepDispenseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine(), clock=lambda: NOW)
        self.admin = Actor("admin", "admin")
        self.clinician = Actor("doc-1", "clinician")
        self.investigator = Actor("inv-1", "investigator")

    def tearDown(self):
        self.tmp.cleanup()

    def _case(self, confirm="lab"):
        case = self.service.create(
            self.admin, "case",
            {"person_id": "P-1", "onset_date": "2026-03-01", "location": "A", "symptoms": ["fever"]},
        )
        self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
        if confirm == "lab":
            self.service.transition(self.admin, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"})
        elif confirm == "probable":
            self.service.transition(self.admin, case["id"], "mark_probable", {"epi_link": "cluster-1"})
        return case

    def _contact(self, case_id, person="P-2", exposure="2026-03-09T12:00:00", follow=True):
        contact = self.service.create(
            self.investigator, "contact",
            {"case_id": case_id, "person_id": person, "exposure_start": exposure},
        )
        if follow:
            self.service.transition(
                self.investigator, contact["id"], "begin_followup",
                {"followup_start": "2026-03-09", "due_at": "2026-03-23"},
            )
        return contact

    def _batch(self, batch_no="B-1", quantity=5):
        return self.service.create(
            self.admin, "drug_batch",
            {"drug_name": "PEP-A", "batch_no": batch_no, "quantity": quantity},
        )

    def _dispense(self, contact, batch_no="B-1", courses=1, actor=None):
        return self.service.dispense_pep(
            actor or self.clinician,
            {"contact_id": contact["id"], "batch_no": batch_no, "courses": courses},
        )

    def test_dispense_success_deducts_stock(self):
        case = self._case()
        contact = self._contact(case["id"])
        batch = self._batch(quantity=5)
        order, created = self._dispense(contact, courses=2)
        self.assertTrue(created)
        self.assertEqual(order["kind"], "pep_dispense")
        self.assertEqual(order["status"], "dispensed")
        self.assertEqual(order["data"]["courses"], 2)
        self.assertEqual(order["data"]["case_id"], case["id"])
        self.assertEqual(order["data"]["dispensed_by"], "doc-1")
        self.assertEqual(self.service.get(batch["id"])["data"]["quantity"], 3)

    def test_dispense_allowed_for_probable_case(self):
        case = self._case(confirm="probable")
        contact = self._contact(case["id"])
        self._batch()
        order, created = self._dispense(contact)
        self.assertTrue(created)

    def test_dispense_requires_clinician_role(self):
        case = self._case()
        contact = self._contact(case["id"])
        self._batch()
        with self.assertRaises(PermissionDenied):
            self._dispense(contact, actor=self.investigator)
        with self.assertRaises(PermissionDenied):
            self._dispense(contact, actor=self.admin)

    def test_dispense_rejected_when_case_not_confirmed(self):
        case = self._case(confirm=None)
        contact = self._contact(case["id"])
        self._batch()
        with self.assertRaises(InvalidTransition):
            self._dispense(contact)

    def test_dispense_rejected_when_contact_not_following(self):
        case = self._case()
        contact = self._contact(case["id"], follow=False)
        self._batch()
        with self.assertRaises(InvalidTransition):
            self._dispense(contact)
        following = self._contact(case["id"], person="P-3")
        self.service.transition(
            self.investigator, following["id"], "complete_followup", {"outcome": "no symptoms"}
        )
        with self.assertRaises(InvalidTransition):
            self._dispense(following)

    def test_dispense_rejected_after_72_hours(self):
        case = self._case()
        expired = self._contact(case["id"], exposure="2026-03-06T11:59:00")
        self._batch()
        with self.assertRaises(ValidationError):
            self._dispense(expired)
        exactly_72h = self._contact(case["id"], person="P-3", exposure="2026-03-07T12:00:00")
        with self.assertRaises(ValidationError):
            self._dispense(exactly_72h)

    def test_dispense_allowed_just_inside_window(self):
        case = self._case()
        contact = self._contact(case["id"], exposure="2026-03-07T12:00:01")
        self._batch()
        order, created = self._dispense(contact)
        self.assertTrue(created)

    def test_insufficient_stock_refuses_without_side_effects(self):
        case = self._case()
        contact = self._contact(case["id"])
        batch = self._batch(quantity=1)
        with self.assertRaises(ValidationError):
            self._dispense(contact, courses=2)
        self.assertEqual(self.service.get(batch["id"])["data"]["quantity"], 1)
        self.assertEqual(self.service.list("pep_dispense"), [])

    def test_duplicate_dispense_returns_original_order(self):
        case = self._case()
        contact = self._contact(case["id"])
        batch = self._batch(quantity=5)
        first, created = self._dispense(contact, courses=1)
        self.assertTrue(created)
        second, created = self._dispense(contact, courses=3)
        self.assertFalse(created)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(self.service.get(batch["id"])["data"]["quantity"], 4)
        self.assertEqual(len(self.service.list("pep_dispense")), 1)

    def test_dispense_rejects_unknown_batch_and_contact(self):
        case = self._case()
        contact = self._contact(case["id"])
        self._batch()
        with self.assertRaises(ValidationError):
            self._dispense(contact, batch_no="NOPE")
        with self.assertRaises(ValidationError):
            self.service.dispense_pep(
                self.clinician, {"contact_id": "missing", "batch_no": "B-1", "courses": 1}
            )

    def test_dispense_rejects_invalid_courses(self):
        case = self._case()
        contact = self._contact(case["id"])
        self._batch()
        for bad in (0, -1, "2", True):
            with self.assertRaises(ValidationError):
                self._dispense(contact, courses=bad)

    def test_pep_dispense_cannot_be_created_generically(self):
        with self.assertRaises(ValidationError):
            self.service.create(self.admin, "pep_dispense", {"contact_id": "x"})

    def test_drug_batch_validation(self):
        self._batch(batch_no="B-9", quantity=3)
        with self.assertRaises(ConflictError):
            self._batch(batch_no="B-9", quantity=1)
        with self.assertRaises(ValidationError):
            self._batch(batch_no="B-10", quantity=-1)
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.clinician, "drug_batch",
                {"drug_name": "PEP-A", "batch_no": "B-11", "quantity": 1},
            )


class PepHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(repo, RuleEngine(), clock=lambda: NOW)
        static_dir = Path(__file__).resolve().parent.parent / "static"
        self.server = create_server("127.0.0.1", 0, self.service, RuleEngine(), str(static_dir))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%s" % self.server.server_address[1]
        admin = Actor("admin", "admin")
        investigator = Actor("inv-1", "investigator")
        case = self.service.create(
            admin, "case",
            {"person_id": "P-1", "onset_date": "2026-03-01", "location": "A", "symptoms": ["fever"]},
        )
        self.service.transition(admin, case["id"], "triage", {"clinician": "C-1"})
        self.service.transition(admin, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"})
        self.contact = self.service.create(
            investigator, "contact",
            {"case_id": case["id"], "person_id": "P-2", "exposure_start": "2026-03-09T12:00:00"},
        )
        self.service.transition(
            investigator, self.contact["id"], "begin_followup",
            {"followup_start": "2026-03-09", "due_at": "2026-03-23"},
        )
        self.service.create(admin, "drug_batch", {"drug_name": "PEP-A", "batch_no": "B-1", "quantity": 2})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def _post_dispense(self, payload, role="clinician"):
        request = urllib.request.Request(
            self.base + "/api/pep/dispense",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-User-Id": "doc-1", "X-Role": role},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_dispense_endpoint_created_then_duplicate(self):
        payload = {"contact_id": self.contact["id"], "batch_no": "B-1", "courses": 1}
        status, body = self._post_dispense(payload)
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "dispensed")
        status, duplicate = self._post_dispense(payload)
        self.assertEqual(status, 200)
        self.assertEqual(duplicate["id"], body["id"])

    def test_dispense_endpoint_rejects_bad_requests(self):
        status, body = self._post_dispense(
            {"contact_id": self.contact["id"], "batch_no": "B-1", "courses": 99}
        )
        self.assertEqual(status, 400)
        self.assertIn("insufficient stock", body["error"])
        status, body = self._post_dispense(
            {"contact_id": self.contact["id"], "batch_no": "B-1", "courses": 1}, role="viewer"
        )
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
