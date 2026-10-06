"""Alarm rule queries. Like alarms, every query starts from `_base`, which carries the site
filter. Writes need a writer role (site admin or system admin); a site admin writes only for the
devices of their own sites."""

import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.audit import audit
from hastori_api.errors import ApiError, conflict, not_found
from hastori_api.queries.devices import get_device
from hastori_api.schemas import RuleBody, RuleCreate, RuleOut
from hastori_api.scope import WRITERS, SiteScope
from hastori_common.models import AlarmRule, Device

UNIQUE_RULE = "uq_rules_device_metric_kind"


def _base(scope: SiteScope) -> Select[AlarmRule, Device]:
    return (
        select(AlarmRule, Device)
        .join(Device, Device.id == AlarmRule.device_id)
        .where(Device.site_id.in_(scope.site_ids))
    )


def rule_out(rule: AlarmRule, device: Device) -> RuleOut:
    return RuleOut(
        id=rule.id,
        device_id=device.id,
        device_name=device.name,
        site_id=device.site_id,
        name=rule.name,
        kind=rule.kind,  # type: ignore[arg-type]
        metric=rule.metric,  # type: ignore[arg-type]
        operator=rule.operator,  # type: ignore[arg-type]
        threshold=rule.threshold,
        clear_threshold=rule.clear_threshold,
        duration_s=rule.duration_s,
        window_s=rule.window_s,
        severity=rule.severity,  # type: ignore[arg-type]
        enabled=rule.enabled,
    )


def _body_values(body: RuleBody) -> dict[str, object]:
    return body.model_dump()


async def list_rules(
    session: AsyncSession,
    scope: SiteScope,
    *,
    site_id: uuid.UUID | None,
    device_id: uuid.UUID | None,
    enabled: bool | None,
    page: int,
    size: int,
) -> tuple[list[RuleOut], int]:
    scope.require_role(*WRITERS)
    stmt = _base(scope)
    if site_id is not None:
        scope.require_site(site_id)
        stmt = stmt.where(Device.site_id == site_id)
    if device_id is not None:
        stmt = stmt.where(AlarmRule.device_id == device_id)
    if enabled is not None:
        stmt = stmt.where(AlarmRule.enabled.is_(enabled))
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        await session.execute(
            stmt.order_by(Device.name, AlarmRule.name).limit(size).offset((page - 1) * size)
        )
    ).all()
    return [rule_out(r, d) for r, d in rows], total


async def _get(
    session: AsyncSession, scope: SiteScope, rule_id: uuid.UUID
) -> tuple[AlarmRule, Device]:
    row = (await session.execute(_base(scope).where(AlarmRule.id == rule_id))).first()
    if row is None:
        raise not_found("Alarm rule not found")
    rule, device = row
    return rule, device


async def get_rule(session: AsyncSession, scope: SiteScope, rule_id: uuid.UUID) -> RuleOut:
    scope.require_role(*WRITERS)
    return rule_out(*await _get(session, scope, rule_id))


def _check_device_fits(body: RuleBody, device: Device) -> None:
    if body.kind == "reactive_ratio" and device.type != "energy_analyzer":
        raise ApiError(
            422,
            "A reactive_ratio rule needs an energy analyzer: only it measures the whole site",
            code="validation_error",
        )


async def _flush(session: AsyncSession) -> None:
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if UNIQUE_RULE in str(exc.orig):
            raise conflict(
                "This device already has an enabled rule of this kind for this metric; "
                "change that rule instead"
            ) from None
        raise


async def create_rule(session: AsyncSession, scope: SiteScope, body: RuleCreate) -> RuleOut:
    scope.require_role(*WRITERS)
    device = await get_device(session, scope, body.device_id)  # 404 outside the scope
    _check_device_fits(body, device)
    rule = AlarmRule(**_body_values(body))
    session.add(rule)
    await _flush(session)
    audit(session, scope, "rule.create", "alarm_rule", rule.id, body.model_dump())
    await session.commit()
    return rule_out(rule, device)


async def update_rule(
    session: AsyncSession, scope: SiteScope, rule_id: uuid.UUID, body: RuleBody
) -> RuleOut:
    scope.require_role(*WRITERS)
    rule, device = await _get(session, scope, rule_id)
    _check_device_fits(body, device)
    for field, value in _body_values(body).items():
        setattr(rule, field, value)
    await _flush(session)
    audit(session, scope, "rule.update", "alarm_rule", rule.id, body.model_dump())
    await session.commit()
    return rule_out(rule, device)


async def disable_rule(session: AsyncSession, scope: SiteScope, rule_id: uuid.UUID) -> None:
    """Rules are never deleted: alarms refer to them. A disabled rule stops evaluating and the
    alarm service closes its open alarm."""
    scope.require_role(*WRITERS)
    rule, _ = await _get(session, scope, rule_id)
    if rule.enabled:
        rule.enabled = False
        await session.flush()
        audit(session, scope, "rule.disable", "alarm_rule", rule.id)
        await session.commit()
