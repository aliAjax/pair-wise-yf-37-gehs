import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    StockShortage,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _hours_ago(hours, base=None):
    base = base or datetime.now(timezone.utc)
    return (base - timedelta(hours=hours)).isoformat(timespec="seconds")


class PepBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin-1", "admin")
        self.clinician = Actor("clin-1", "clinician")
        self._seq = 0

    def tearDown(self):
        self.tmp.cleanup()

    def _person(self):
        self._seq += 1
        return "P-%d" % self._seq

    def make_case(self, status="confirmed"):
        case = self.service.create(
            self.admin,
            "case",
            {
                "person_id": self._person(),
                "onset_date": "2026-09-01",
                "location": "A",
                "symptoms": ["fever"],
            },
        )
        if status == "reported":
            return case
        case = self.service.transition(self.admin, case["id"], "triage", {"clinician": "C-1"})
        if status == "investigating":
            return case
        if status == "confirmed":
            return self.service.transition(
                self.admin, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"}
            )
        if status == "probable":
            return self.service.transition(
                self.admin, case["id"], "mark_probable", {"epi_link": "household"}
            )
        raise AssertionError("unsupported status " + status)

    def make_contact(self, case_id, exposure=None, status="identified"):
        contact = self.service.create(
            self.admin,
            "contact",
            {
                "case_id": case_id,
                "person_id": self._person(),
                "exposure_start": exposure or _hours_ago(24),
            },
        )
        if status == "identified":
            return contact
        contact = self.service.transition(
            self.admin,
            contact["id"],
            "begin_followup",
            {"followup_start": "2026-09-20", "due_at": "2026-10-04"},
        )
        if status == "following":
            return contact
        if status == "completed":
            return self.service.transition(
                self.admin, contact["id"], "complete_followup", {"outcome": "no symptoms"}
            )
        raise AssertionError("unsupported status " + status)

    def make_lot(self, remaining=30, lot_number=None):
        self._seq += 1
        return self.service.create(
            self.admin,
            "drug_lot",
            {
                "drug_name": "PEP-A",
                "lot_number": lot_number or "LOT-%d" % self._seq,
                "quantity_remaining": remaining,
            },
        )

    def dispense(self, contact_id, lot_id, days=28, actor=None, now=None):
        return self.service.dispense_pep(
            actor or self.clinician,
            {"contact_id": contact_id, "lot_id": lot_id, "course_days": days},
            now=now,
        )


class PepDispenseTest(PepBase):
    def test_dispense_success_deducts_stock_and_records(self):
        case = self.make_case("confirmed")
        contact = self.make_contact(case["id"])
        lot = self.make_lot(remaining=30)

        dispense, created = self.dispense(contact["id"], lot["id"], days=28)

        self.assertTrue(created)
        self.assertEqual(dispense["kind"], "pep_dispense")
        self.assertEqual(dispense["status"], "active")
        self.assertEqual(dispense["data"]["contact_id"], contact["id"])
        self.assertEqual(dispense["data"]["case_id"], case["id"])
        self.assertEqual(dispense["data"]["lot_number"], lot["data"]["lot_number"])
        self.assertEqual(dispense["data"]["course_days"], 28)
        self.assertEqual(dispense["data"]["quantity"], 28)
        self.assertEqual(dispense["data"]["dispensed_by"], "clin-1")

        lot_after = self.service.get(lot["id"])
        self.assertEqual(lot_after["data"]["quantity_remaining"], 2)

        actions = [row["action"] for row in self.service.audit_log(dispense["id"])]
        self.assertIn("pep_dispense", actions)

    def test_probable_case_is_accepted(self):
        case = self.make_case("probable")
        contact = self.make_contact(case["id"])
        lot = self.make_lot()
        dispense, created = self.dispense(contact["id"], lot["id"])
        self.assertTrue(created)

    def test_duplicate_submission_returns_original_id(self):
        case = self.make_case("confirmed")
        contact = self.make_contact(case["id"])
        lot = self.make_lot(remaining=60)

        first, created_first = self.dispense(contact["id"], lot["id"], days=28)
        second, created_second = self.dispense(contact["id"], lot["id"], days=28)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["id"], second["id"])
        # 第二次没有重复扣库存
        self.assertEqual(self.service.get(lot["id"])["data"]["quantity_remaining"], 60 - 28)
        dispenses = self.service.list(kind="pep_dispense")
        self.assertEqual(len(dispenses), 1)

    def test_insufficient_stock_rejected_without_side_effects(self):
        case = self.make_case("confirmed")
        contact = self.make_contact(case["id"])
        lot = self.make_lot(remaining=2)

        with self.assertRaises(StockShortage):
            self.dispense(contact["id"], lot["id"], days=28)

        # 不扣药、不产生发放单
        self.assertEqual(self.service.get(lot["id"])["data"]["quantity_remaining"], 2)
        self.assertEqual(self.service.list(kind="pep_dispense"), [])

    def test_exposure_window_boundary(self):
        now = datetime.now(timezone.utc)
        case = self.make_case("confirmed")
        lot = self.make_lot(remaining=200)

        within = self.make_contact(case["id"], exposure=_hours_ago(71, base=now))
        dispense, created = self.dispense(within["id"], lot["id"], days=1, now=now)
        self.assertTrue(created)

        edge = self.make_contact(case["id"], exposure=_hours_ago(72, base=now))
        with self.assertRaises(ValidationError):
            self.dispense(edge["id"], lot["id"], days=1, now=now)

        late = self.make_contact(case["id"], exposure=_hours_ago(96, base=now))
        with self.assertRaises(ValidationError):
            self.dispense(late["id"], lot["id"], days=1, now=now)

    def test_contact_no_longer_observed_rejected(self):
        case = self.make_case("confirmed")
        contact = self.make_contact(case["id"], status="completed")
        lot = self.make_lot()
        with self.assertRaises(InvalidTransition):
            self.dispense(contact["id"], lot["id"])

    def test_case_not_diagnosed_rejected(self):
        for status in ("reported", "investigating"):
            case = self.make_case(status)
            contact = self.make_contact(case["id"])
            lot = self.make_lot()
            with self.assertRaises(ValidationError):
                self.dispense(contact["id"], lot["id"])

    def test_only_clinician_or_admin_can_dispense(self):
        case = self.make_case("confirmed")
        contact = self.make_contact(case["id"])
        lot = self.make_lot()
        for role in ("viewer", "investigator", "lab"):
            with self.assertRaises(PermissionDenied):
                self.dispense(contact["id"], lot["id"], actor=Actor("u-" + role, role))

    def test_missing_fields_rejected(self):
        case = self.make_case("confirmed")
        contact = self.make_contact(case["id"])
        lot = self.make_lot()
        with self.assertRaises(ValidationError):
            self.service.dispense_pep(self.clinician, {"contact_id": contact["id"]})
        with self.assertRaises(ValidationError):
            self.service.dispense_pep(
                self.clinician,
                {"contact_id": contact["id"], "lot_id": lot["id"], "course_days": 0},
            )

    def test_unknown_contact_or_lot(self):
        case = self.make_case("confirmed")
        contact = self.make_contact(case["id"])
        lot = self.make_lot()
        with self.assertRaises(NotFoundError):
            self.dispense("no-such-contact", lot["id"])
        with self.assertRaises(NotFoundError):
            self.dispense(contact["id"], "no-such-lot")

    def test_drug_lot_creation_rules(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.admin,
                "drug_lot",
                {"drug_name": "PEP-A", "lot_number": "LOT-NEG", "quantity_remaining": -1},
            )
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("v", "viewer"),
                "drug_lot",
                {"drug_name": "PEP-A", "lot_number": "LOT-X", "quantity_remaining": 1},
            )
        self.make_lot(remaining=5, lot_number="LOT-DUP")
        with self.assertRaises(ConflictError):
            self.make_lot(remaining=5, lot_number="LOT-DUP")


if __name__ == "__main__":
    unittest.main()
