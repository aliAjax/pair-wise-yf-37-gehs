from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None, clock=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def dispense_pep(self, actor, data):
        payload = dict(data or {})
        validated = self.rules.validate_dispense(
            actor, payload, self._lookup, now=self._clock()
        )
        contact_id = validated["contact_id"]
        batch_no = validated["batch_no"]
        courses = validated["courses"]
        batches = self._lookup("drug_batch", "batch_no", batch_no)
        if not batches:
            raise ValidationError("unknown batch_no: " + str(batch_no))
        batch = batches[0]
        contact = self._lookup("contact", "id", contact_id)[0]
        order = {
            "contact_id": contact_id,
            "case_id": contact["data"].get("case_id"),
            "batch_no": batch_no,
            "batch_id": batch["id"],
            "drug_name": batch["data"].get("drug_name"),
            "courses": courses,
            "dispensed_by": actor.user_id,
            "dispensed_at": self._clock().isoformat(timespec="seconds"),
        }
        entity, created, updated_batch = self.repository.create_dispense(
            str(uuid4()), contact_id, batch["id"], courses, order, actor.user_id
        )
        if not created:
            return entity, False
        self.audit.record(
            entity["id"],
            actor,
            "dispense",
            None,
            "dispensed",
            {"contact_id": contact_id, "batch_no": batch_no, "courses": courses},
        )
        self.audit.record(
            batch["id"],
            actor,
            "deduct",
            batch["status"],
            updated_batch["status"],
            {"courses": courses, "remaining": updated_batch["data"].get("quantity")},
        )
        return entity, True

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
