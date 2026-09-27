"""演示数据：仅在数据库为空时通过正常服务流程写入，便于演示页开箱即用。"""

from datetime import datetime, timedelta, timezone


def seed_demo_data(service, actor):
    if service.list(kind="case"):
        return False

    today = datetime.now(timezone.utc).date()
    recent = (today - timedelta(days=1)).isoformat()
    stale = (today - timedelta(days=4)).isoformat()

    confirmed = service.create(
        actor,
        "case",
        {
            "person_id": "PAT-1001",
            "onset_date": (today - timedelta(days=6)).isoformat(),
            "location": "District-A",
            "symptoms": ["fever", "rash"],
        },
    )
    service.transition(
        actor, confirmed["id"], "triage", {"clinician": "C-1"}
    )
    confirmed = service.transition(
        actor,
        confirmed["id"],
        "lab_positive",
        {"lab_id": "L-1", "result": "positive"},
    )

    probable = service.create(
        actor,
        "case",
        {
            "person_id": "PAT-1002",
            "onset_date": (today - timedelta(days=5)).isoformat(),
            "location": "District-B",
            "symptoms": ["fever"],
        },
    )
    service.transition(actor, probable["id"], "triage", {"clinician": "C-1"})
    probable = service.transition(
        actor,
        probable["id"],
        "mark_probable",
        {"epi_link": "household contact of PAT-1001"},
    )

    reported = service.create(
        actor,
        "case",
        {
            "person_id": "PAT-1003",
            "onset_date": (today - timedelta(days=2)).isoformat(),
            "location": "District-A",
            "symptoms": ["cough"],
        },
    )

    service.create(
        actor,
        "contact",
        {
            "case_id": confirmed["id"],
            "person_id": "CT-2001",
            "exposure_start": recent + "T09:00:00",
        },
    )
    service.create(
        actor,
        "contact",
        {
            "case_id": probable["id"],
            "person_id": "CT-2002",
            "exposure_start": recent,
        },
    )
    service.create(
        actor,
        "contact",
        {
            "case_id": confirmed["id"],
            "person_id": "CT-2003",
            "exposure_start": stale,
        },
    )
    service.create(
        actor,
        "contact",
        {
            "case_id": reported["id"],
            "person_id": "CT-2004",
            "exposure_start": recent,
        },
    )

    done = service.create(
        actor,
        "contact",
        {
            "case_id": confirmed["id"],
            "person_id": "CT-2005",
            "exposure_start": recent,
        },
    )
    service.transition(
        actor,
        done["id"],
        "begin_followup",
        {"followup_start": today.isoformat(), "due_at": (today + timedelta(days=14)).isoformat()},
    )
    service.transition(
        actor, done["id"], "complete_followup", {"outcome": "no symptoms"}
    )

    service.create(
        actor,
        "drug_lot",
        {"drug_name": "PEP-A", "lot_number": "LOT-2026-01", "quantity_remaining": 30},
    )
    service.create(
        actor,
        "drug_lot",
        {"drug_name": "PEP-A", "lot_number": "LOT-2026-02", "quantity_remaining": 3},
    )
    return True
