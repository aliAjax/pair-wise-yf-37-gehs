from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    StockShortage,
    ValidationError,
)

PEP_WINDOW = timedelta(hours=72)
CONTACT_DISPENSE_STATUSES = ("identified", "following")
CASE_DIAGNOSED_STATUSES = ("confirmed", "probable")


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    return {"confirmed_by": actor.user_id}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def cluster_cases(cases, max_days=14):
    groups = []
    for case in sorted(cases, key=lambda item: str(item.get("onset_date", ""))):
        placed = False
        for group in groups:
            same_location = group["location"] == case.get("location")
            delta = abs(_date_ordinal(group["onset_date"]) - _date_ordinal(case.get("onset_date")))
            if same_location and delta <= max_days:
                group["members"].append(case.get("id"))
                placed = True
                break
        if not placed:
            groups.append({"location": case.get("location"), "onset_date": case.get("onset_date"), "members": [case.get("id")]})
    return [group for group in groups if len(group["members"]) > 1]


def _parse_exposure_start(value):
    text = str(value or "").strip()
    if not text:
        raise ValidationError("contact is missing exposure_start")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError("invalid exposure_start: " + text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_utc_naive(moment):
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _validate_drug_lot(actor, data, lookup):
    quantity = data.get("quantity_remaining")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
        raise ValidationError("quantity_remaining must be a non-negative integer")
    lot_number = data.get("lot_number")
    if lookup and lot_number:
        rows = lookup("drug_lot", "lot_number", lot_number) or []
        if any(row["data"].get("lot_number") == lot_number for row in rows):
            raise ConflictError("duplicate drug lot number: " + str(lot_number))


CUSTOM_CREATE = {'case': _validate_case, 'drug_lot': _validate_drug_lot}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact', 'drug_lots': 'drug_lot', 'pep_dispenses': 'pep_dispense'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified', 'drug_lot': 'active', 'pep_dispense': 'active'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start'), 'drug_lot': ('drug_name', 'lot_number', 'quantity_remaining')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator'), 'drug_lot': ('admin',)}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator')}
    PEP_REQUIRED = ('contact_id', 'lot_id', 'course_days')
    PEP_ROLES = ('admin', 'clinician')

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def validate_pep_actor(self, actor, data):
        """发放前的轻量校验：角色与必填字段（用于重复提交的快速识别）。"""
        self._ensure_role(actor, self.PEP_ROLES)
        self._require(data, self.PEP_REQUIRED)

    def validate_pep_dispense(self, actor, data, lookup, now=None):
        """发放单准入规则：

        - 仅临床医生（及管理员）可发放；
        - 关联病例已确诊（confirmed）或临床诊断（probable）；
        - 接触者仍在观察中（identified/following，未完成随访）；
        - 从暴露开始未满 72 小时；
        - 批号有效、库存足够、疗程为正整数。
        返回快照后的发放单数据（含数量、批号、药品名、病例）。
        """
        self.validate_pep_actor(actor, data)
        course_days = data.get("course_days")
        if not isinstance(course_days, int) or isinstance(course_days, bool) or course_days <= 0:
            raise ValidationError("course_days must be a positive integer")

        contact = _find_one(lookup, "contact", "id", data["contact_id"])
        if not contact:
            raise NotFoundError("contact not found: " + str(data["contact_id"]))
        if contact["status"] not in CONTACT_DISPENSE_STATUSES:
            raise InvalidTransition(
                "contact is no longer under observation (status %s)" % contact["status"]
            )

        case_id = contact["data"].get("case_id")
        case = _find_one(lookup, "case", "id", case_id) if case_id else None
        if not case:
            raise NotFoundError("linked case not found: " + str(case_id))
        if case["status"] not in CASE_DIAGNOSED_STATUSES:
            raise ValidationError(
                "case must be confirmed or clinically diagnosed (probable), found %s"
                % case["status"]
            )

        exposure_start = _parse_exposure_start(contact["data"].get("exposure_start"))
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if _as_utc_naive(current) - _as_utc_naive(exposure_start) >= PEP_WINDOW:
            raise ValidationError("more than 72 hours have passed since exposure started")

        lot = _find_one(lookup, "drug_lot", "id", data["lot_id"])
        if not lot:
            raise NotFoundError("drug lot not found: " + str(data["lot_id"]))
        if lot["status"] != "active":
            raise ValidationError("drug lot %s is not active" % lot["id"])
        quantity = int(course_days)
        if int(lot["data"].get("quantity_remaining", 0)) < quantity:
            raise StockShortage(
                "insufficient stock in lot %s for a %s-day course"
                % (lot["data"].get("lot_number", lot["id"]), quantity)
            )

        return {
            "contact_id": contact["id"],
            "case_id": case["id"],
            "person_id": contact["data"].get("person_id"),
            "lot_id": lot["id"],
            "lot_number": lot["data"].get("lot_number"),
            "drug_name": lot["data"].get("drug_name"),
            "course_days": quantity,
            "quantity": quantity,
            "exposure_start": contact["data"].get("exposure_start"),
        }


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
