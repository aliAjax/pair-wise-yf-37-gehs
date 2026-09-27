import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class PepHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = SQLiteRepository(Path(cls.tmp.name) / "http.db")
        cls.service = DomainService(cls.repo, RuleEngine())
        cls.server = create_server("127.0.0.1", 0, cls.service, RuleEngine(), ".")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        admin = Actor("admin-1", "admin")
        case = cls.service.create(
            admin,
            "case",
            {
                "person_id": "P-HTTP-1",
                "onset_date": "2026-09-01",
                "location": "A",
                "symptoms": ["fever"],
            },
        )
        cls.service.transition(admin, case["id"], "triage", {"clinician": "C-1"})
        cls.case = cls.service.transition(
            admin, case["id"], "lab_positive", {"lab_id": "L-1", "result": "positive"}
        )
        exposure = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(timespec="seconds")
        cls.contact = cls.service.create(
            admin,
            "contact",
            {"case_id": cls.case["id"], "person_id": "P-HTTP-2", "exposure_start": exposure},
        )
        cls.lot = cls.service.create(
            admin,
            "drug_lot",
            {"drug_name": "PEP-A", "lot_number": "LOT-HTTP", "quantity_remaining": 30},
        )
        cls.small_lot = cls.service.create(
            admin,
            "drug_lot",
            {"drug_name": "PEP-A", "lot_number": "LOT-HTTP-S", "quantity_remaining": 2},
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def _post(self, path, payload, role="clinician", user="clin-1"):
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-User-Id": user, "X-Role": role},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_dispense_flow_over_http(self):
        payload = {
            "contact_id": self.contact["id"],
            "lot_id": self.lot["id"],
            "course_days": 28,
        }
        status, body = self._post("/api/pep_dispense", payload)
        self.assertEqual(status, 201)
        self.assertEqual(body["kind"], "pep_dispense")
        self.assertEqual(body["data"]["quantity"], 28)
        original_id = body["id"]

        # 重复提交：返回原编号，不再扣库存
        status, body = self._post("/api/pep_dispense", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], original_id)

        status, body = self._get("/api/drug_lots")
        self.assertEqual(status, 200)
        lots = {item["id"]: item for item in body["items"]}
        self.assertEqual(lots[self.lot["id"]]["data"]["quantity_remaining"], 2)

        status, body = self._get("/api/pep_dispenses?status=active")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 1)

    def test_dispense_rejections_over_http(self):
        # 非临床角色
        status, body = self._post(
            "/api/pep_dispense",
            {"contact_id": self.contact["id"], "lot_id": self.lot["id"], "course_days": 1},
            role="viewer",
        )
        self.assertEqual(status, 403)

        # 库存不足：409，且不产生发放单
        other_contact = self.service.create(
            Actor("admin-1", "admin"),
            "contact",
            {
                "case_id": self.case["id"],
                "person_id": "P-HTTP-3",
                "exposure_start": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds"),
            },
        )
        status, body = self._post(
            "/api/pep_dispense",
            {"contact_id": other_contact["id"], "lot_id": self.small_lot["id"], "course_days": 28},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["type"], "StockShortage")
        lot = self.service.get(self.small_lot["id"])
        self.assertEqual(lot["data"]["quantity_remaining"], 2)
        dispenses = [
            item
            for item in self.service.list(kind="pep_dispense")
            if item["data"]["contact_id"] == other_contact["id"]
        ]
        self.assertEqual(dispenses, [])


if __name__ == "__main__":
    unittest.main()
