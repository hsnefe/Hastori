"""Alarm queries. `_base` is the only way to start a query on alarms and it already contains the
site filter, so no function in here (or one added later) can return an alarm of another site."""

import uuid
from datetime import datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hastori_api.audit import audit
from hastori_api.errors import conflict, not_found
from hastori_api.queries.rules import rule_out
from hastori_api.schemas import AlarmDetailOut, AlarmOut, AlarmState, TimelineEntry
from hastori_api.scope import WRITERS, SiteScope
from hastori_common.models import Alarm, AlarmRule, Device, Site


def _base(scope: SiteScope) -> Select[Alarm, AlarmRule, Device, Site]:
    return (
        select(Alarm, AlarmRule, Device, Site)
        .join(AlarmRule, AlarmRule.id == Alarm.rule_id)
        .join(Device, Device.id == Alarm.device_id)
        .join(Site, Site.id == Device.site_id)
        .where(Device.site_id.in_(scope.site_ids))
    )


def display_name(email: str) -> str:
    """The name shown for a user: their address without the domain (users have no other name)."""
    return email.split("@", 1)[0]


def _out(alarm: Alarm, rule: AlarmRule, device: Device, site: Site) -> AlarmOut:
    return AlarmOut(
        id=alarm.id,
        state=alarm.state,  # type: ignore[arg-type]
        severity=rule.severity,  # type: ignore[arg-type]
        rule_id=rule.id,
        rule_name=rule.name,
        metric=rule.metric,
        device_id=device.id,
        device_name=device.name,
        site_id=site.id,
        site_name=site.name,
        opened_at=alarm.opened_at,
        acked_at=alarm.acked_at,
        acked_by=alarm.acked_by,
        acked_by_label=alarm.acked_by_label,
        cleared_at=alarm.cleared_at,
        peak_value=alarm.peak_value,
    )


async def list_alarms(
    session: AsyncSession,
    scope: SiteScope,
    *,
    state: AlarmState | None,
    site_id: uuid.UUID | None,
    start: datetime | None,
    end: datetime | None,
    page: int,
    size: int,
) -> tuple[list[AlarmOut], int]:
    stmt = _base(scope)
    if site_id is not None:
        scope.require_site(site_id)
        stmt = stmt.where(Device.site_id == site_id)
    if state is not None:
        stmt = stmt.where(Alarm.state == state)
    if start is not None:
        stmt = stmt.where(Alarm.opened_at >= start)
    if end is not None:
        stmt = stmt.where(Alarm.opened_at < end)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        await session.execute(
            stmt.order_by(Alarm.opened_at.desc(), Alarm.id).limit(size).offset((page - 1) * size)
        )
    ).all()
    return [_out(*row) for row in rows], total


async def get_alarm(
    session: AsyncSession, scope: SiteScope, alarm_id: uuid.UUID
) -> tuple[Alarm, AlarmRule, Device, Site]:
    row = (await session.execute(_base(scope).where(Alarm.id == alarm_id))).first()
    if row is None:
        raise not_found("Alarm not found")
    alarm, rule, device, site = row
    return alarm, rule, device, site


async def alarm_detail(
    session: AsyncSession, scope: SiteScope, alarm_id: uuid.UUID
) -> AlarmDetailOut:
    alarm, rule, device, site = await get_alarm(session, scope, alarm_id)
    timeline = [TimelineEntry(event="opened", at=alarm.opened_at)]
    if alarm.acked_at is not None:
        timeline.append(
            TimelineEntry(event="acknowledged", at=alarm.acked_at, by=alarm.acked_by_label)
        )
    if alarm.cleared_at is not None:
        timeline.append(TimelineEntry(event="cleared", at=alarm.cleared_at))
    return AlarmDetailOut(
        **_out(alarm, rule, device, site).model_dump(),
        threshold=alarm.threshold,
        clear_threshold=alarm.clear_threshold,
        timeline=timeline,
        rule=rule_out(rule, device),
    )


async def acknowledge(session: AsyncSession, scope: SiteScope, alarm_id: uuid.UUID) -> AlarmOut:
    scope.require_role(*WRITERS)  # the role first: a viewer is refused whatever the alarm is
    await get_alarm(session, scope, alarm_id)  # 404 outside the scope
    result = await session.execute(
        update(Alarm)
        .where(Alarm.id == alarm_id, Alarm.state == "active")
        .values(
            state="acknowledged",
            acked_at=func.now(),
            acked_by=scope.user_id,
            acked_by_label=display_name(scope.email),
        )
        .returning(Alarm.id)
    )
    if result.scalar_one_or_none() is None:
        # Not active any more: someone else acknowledged it, or the alarm service closed it in
        # the meantime. The conditional update is what makes the two writers safe.
        await session.rollback()
        raise conflict("Only an active alarm can be acknowledged")
    audit(session, scope, "alarm.acknowledge", "alarm", alarm_id)
    await session.commit()
    session.expire_all()
    alarm, rule, device, site = await get_alarm(session, scope, alarm_id)
    return _out(alarm, rule, device, site)
