import sqlite3
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, StockShortage
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

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

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def dispense_pep(self, actor, data, now=None):
        """发放暴露后预防药品。

        成功扣减批号库存并生成一份 active 发放单；库存不足时整体拒绝，
        不扣药也不产生单据。同一接触者已有有效发放单时，重复提交直接
        返回原单据（不重复发药）。返回 (entity, created)。
        """
        incoming = dict(data or {})
        # 先校验角色与必填，再查重：同一接触者已有有效发放单时直接返回原编号，
        # 不再触发库存/窗口等业务校验（幂等重放）。
        self.rules.validate_pep_actor(actor, incoming)
        contact_id = incoming["contact_id"]
        existing = self.repository.find_active_dispense(contact_id)
        if existing:
            return existing, False
        payload = self.rules.validate_pep_dispense(actor, incoming, self._lookup, now=now)

        dispense_id = str(uuid4())
        payload["dispensed_by"] = actor.user_id
        payload["dispensed_at"] = utcnow()
        try:
            dispense, lot = self.repository.create_pep_dispense(
                dispense_id, payload, payload["lot_id"], actor.user_id
            )
        except sqlite3.IntegrityError:
            # 并发下另一请求已为该接触者建单：回退为返回原编号
            existing = self.repository.find_active_dispense(contact_id)
            if existing:
                return existing, False
            raise ConflictError("could not create pep dispense")
        except StockShortage:
            # 规则层预检后仍被并发抢空：无单据、无扣减
            raise
        self.audit.record(
            dispense["id"],
            actor,
            "pep_dispense",
            None,
            "active",
            {
                "contact_id": dispense["data"]["contact_id"],
                "lot_id": dispense["data"]["lot_id"],
                "lot_number": dispense["data"]["lot_number"],
                "course_days": dispense["data"]["course_days"],
                "quantity": dispense["data"]["quantity"],
            },
        )
        self.audit.record(
            lot["id"],
            actor,
            "stock_deduct",
            "active",
            "active",
            {"lot_id": lot["id"], "quantity": payload["quantity"], "reason": "pep_dispense", "dispense_id": dispense["id"]},
        )
        return dispense, True

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
