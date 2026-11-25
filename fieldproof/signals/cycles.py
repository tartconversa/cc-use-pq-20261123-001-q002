from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

from fieldproof.common import utc_text
from fieldproof.design.contracts import Policy, local_day
from fieldproof.design.registry import Roster
from fieldproof.signals.contracts import Event
from fieldproof.signals.handovers import Handover
from fieldproof.signals.ledger import cycle_inputs

# 已确认：同一表计连续读数，或经核准交接断点拼接而成的完整计量。
ENERGY_METERED = "metered"
# 已确认拼接：断点读数均来自经核准的交接，且不存在未覆盖缺口。
ENERGY_SPLICED = "spliced"
# 有界：停表区间只有可核查的用电上下界，给出区间而非定值。
ENERGY_BOUNDED = "bounded"
# 待补证：交接尚未核准，拼接结论暂不成立。
PENDING_SPLICE = "pending_splice"
# 显式缺口：停表区间没有任何可核查的上下界。
UNBOUNDED_GAP = "unbounded_gap"
# 非交接类耗电问题（缺端点、读数冲突、表码倒退等）的占位状态。
UNRESOLVED = "unresolved"
# 同一断点存在多个无法区分的候选交接。
AMBIGUOUS_HANDOVER = "ambiguous_handover"
# 交接断点读数与表计事件对不上，或表计链在物理上不连续。
BROKEN_METER_CHAIN = "broken_meter_chain"
# 交接声称的断点与实际读数不一致。
HANDOVER_MISMATCH = "handover_reading_mismatch"


@dataclass(frozen=True)
class Energy:
    wh: int | None
    problem: str | None = None
    meter_id: str | None = None
    event_ids: tuple[str, ...] = ()
    status: str = UNRESOLVED
    low_wh: int | None = None
    high_wh: int | None = None
    handover_ids: tuple[str, ...] = ()
    gaps: tuple[dict, ...] = ()

    @property
    def bounded(self) -> bool:
        return self.status == ENERGY_BOUNDED

    @property
    def confirmed(self) -> bool:
        return self.wh is not None and self.status in {ENERGY_METERED, ENERGY_SPLICED}

    def gap_payload(self) -> list[dict]:
        return [dict(item) for item in self.gaps]


@dataclass
class Cycle:
    unit_id: str
    cycle_id: str
    state: str
    issues: list[str]
    day: date | None = None
    start: Event | None = None
    close: Event | None = None
    manual: bool = False
    alarm: bool = False
    elapsed_seconds: int | None = None
    energy: Energy = field(default_factory=lambda: Energy(None, "cycle_unresolved"))
    sources: dict[str, int] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.state in {"complete", "aborted"}

    @property
    def failed(self) -> bool:
        return self.state == "aborted"

    def payload(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "cycle_id": self.cycle_id,
            "state": self.state,
            "issues": self.issues,
            "local_date": self.day.isoformat() if self.day else None,
            "started_at": utc_text(self.start.occurred_at) if self.start else None,
            "closed_at": utc_text(self.close.occurred_at) if self.close else None,
            "manual": self.manual,
            "alarm": self.alarm,
            "elapsed_seconds": self.elapsed_seconds,
            "energy_wh": self.energy.wh,
            "energy_issue": self.energy.problem,
            "energy_status": self.energy.status,
            "energy_low_wh": self.energy.low_wh,
            "energy_high_wh": self.energy.high_wh,
            "meter_id": self.energy.meter_id,
            "meter_events": list(self.energy.event_ids),
            "handover_ids": list(self.energy.handover_ids),
            "energy_gaps": self.energy.gap_payload(),
            "source_revisions": self.sources,
        }


def ordered_meter_events(meters: list[Event], start: datetime, close: datetime):
    by_time: dict[datetime, list[Event]] = defaultdict(list)
    for meter in meters:
        if start <= meter.occurred_at <= close:
            by_time[meter.occurred_at].append(meter)
    if start not in by_time or close not in by_time:
        return None, Energy(None, "missing_endpoint")
    ordered = []
    for moment in sorted(by_time):
        candidates = by_time[moment]
        by_meter: dict[str, set[int]] = defaultdict(set)
        for item in candidates:
            by_meter[item.meter_id].add(item.meter_wh)
        if any(len(readings) > 1 for readings in by_meter.values()):
            # 同一表计在同一时刻给出不同读数，互相冲突。
            return None, Energy(None, "conflicting_readings")
        # 同表同时刻同读数视为重复上报，按事件编号取一条；不同表计各自保留。
        unique = {}
        for item in sorted(candidates, key=lambda item: item.event_id):
            unique.setdefault(item.meter_id, item)
        ordered.extend(unique.values())
    readings = [item.meter_wh for item in ordered]
    return ordered, readings


def split_segments(ordered: list[Event]):
    """按相邻读数的表号变化切分计量段，返回 (段列表, 变化时刻列表)。"""
    segments: list[list[Event]] = [[ordered[0]]]
    changes: list[datetime] = []
    for previous, current in zip(ordered, ordered[1:]):
        if current.meter_id == previous.meter_id:
            segments[-1].append(current)
        else:
            changes.append(current.occurred_at)
            segments.append([current])
    return segments, changes


def find_handover(
    handovers: list[Handover],
    moment: datetime,
    outgoing: str,
    incoming: str,
) -> tuple[Handover | None, str | None]:
    matches = [
        item
        for item in handovers
        if item.occurred_at == moment
        and item.old_meter_id == outgoing
        and item.new_meter_id == incoming
    ]
    if not matches:
        return None, BROKEN_METER_CHAIN
    if len(matches) > 1:
        return None, AMBIGUOUS_HANDOVER
    return matches[0], None


def energy_used(
    meters: list[Event],
    start: datetime,
    close: datetime,
    cap: int,
    handovers: list[Handover],
) -> Energy:
    ordered, readings_or_problem = ordered_meter_events(meters, start, close)
    if ordered is None:
        return readings_or_problem
    readings = readings_or_problem
    if len({item.meter_id for item in ordered}) == 1:
        if any(after < before for before, after in zip(readings, readings[1:])):
            return Energy(None, "counter_decreased")
        energy = readings[-1] - readings[0]
        if energy > cap:
            return Energy(None, "implausible_consumption")
        return Energy(
            energy,
            status=ENERGY_METERED,
            meter_id=ordered[0].meter_id,
            event_ids=tuple(item.event_id for item in ordered),
        )

    segments, changes = split_segments(ordered)
    used_handovers: list[Handover] = []
    event_ids: list[str] = []
    gaps: list[dict] = []
    confirmed_wh = 0
    gap_low = 0
    gap_high = 0
    for segment in segments:
        segment_readings = [item.meter_wh for item in segment]
        if any(after < before for before, after in zip(segment_readings, segment_readings[1:])):
            return Energy(None, "counter_decreased")
        confirmed_wh += segment_readings[-1] - segment_readings[0]
        event_ids.extend(item.event_id for item in segment)
    for index, moment in enumerate(changes):
        outgoing = segments[index][-1]
        incoming = segments[index + 1][0]
        handover, problem = find_handover(
            handovers, moment, outgoing.meter_id, incoming.meter_id
        )
        if problem is not None:
            return Energy(
                None,
                problem,
                status=problem,
                event_ids=tuple(item.event_id for item in ordered),
                handover_ids=tuple(item.handover_id for item in used_handovers),
            )
        used_handovers.append(handover)
        if (
            handover.old_reading_wh != outgoing.meter_wh
            or handover.new_reading_wh != incoming.meter_wh
        ):
            return Energy(
                None,
                HANDOVER_MISMATCH,
                status=HANDOVER_MISMATCH,
                event_ids=tuple(item.event_id for item in ordered),
                handover_ids=tuple(item.handover_id for item in used_handovers),
            )
        gap = {
            "handover_id": handover.handover_id,
            "at": utc_text(moment),
            "stoppage_gap": handover.stoppage_gap,
            "low_wh": handover.gap_low_wh,
            "high_wh": handover.gap_high_wh,
        }
        gaps.append(gap)
        if handover.gap_bounded:
            gap_low += handover.gap_low_wh
            gap_high += handover.gap_high_wh

    if any(item.status != "approved" for item in used_handovers):
        return Energy(
            None,
            PENDING_SPLICE,
            status=PENDING_SPLICE,
            event_ids=tuple(event_ids),
            handover_ids=tuple(item.handover_id for item in used_handovers),
            gaps=tuple(gaps),
        )
    handover_ids = tuple(item.handover_id for item in used_handovers)
    if any(item.stoppage_gap and not item.gap_bounded for item in used_handovers):
        return Energy(
            None,
            UNBOUNDED_GAP,
            status=UNBOUNDED_GAP,
            event_ids=tuple(event_ids),
            handover_ids=handover_ids,
            gaps=tuple(gaps),
        )
    if any(item.gap_bounded for item in used_handovers):
        if confirmed_wh + gap_low > cap:
            return Energy(
                None,
                "implausible_consumption",
                event_ids=tuple(event_ids),
                handover_ids=handover_ids,
                gaps=tuple(gaps),
            )
        return Energy(
            None,
            status=ENERGY_BOUNDED,
            low_wh=confirmed_wh + gap_low,
            high_wh=confirmed_wh + gap_high,
            meter_id=None,
            event_ids=tuple(event_ids),
            handover_ids=handover_ids,
            gaps=tuple(gaps),
        )
    if confirmed_wh > cap:
        return Energy(
            None,
            "implausible_consumption",
            event_ids=tuple(event_ids),
            handover_ids=handover_ids,
            gaps=tuple(gaps),
        )
    return Energy(
        confirmed_wh,
        status=ENERGY_SPLICED,
        meter_id=None,
        event_ids=tuple(event_ids),
        handover_ids=handover_ids,
        gaps=tuple(gaps),
    )


def resolve(
    unit_id: str,
    cycle_id: str,
    events: list[Event],
    roster: Roster,
    policy: Policy,
    handovers: list[Handover] | None = None,
) -> Cycle:
    groups = defaultdict(list)
    for event in events:
        groups[event.kind].append(event)
    starts = groups["start"]
    ends = groups["complete"] + groups["abort"]
    cycle = Cycle(
        unit_id,
        cycle_id,
        "invalid",
        [],
        alarm=bool(groups["alarm"]),
        sources={event.event_id: event.revision for event in events},
    )
    if len(starts) != 1:
        if not starts:
            cycle.state = "orphan"
            cycle.issues.append("missing_start")
        else:
            cycle.issues.append("duplicate_start")
        return cycle
    cycle.start = start = starts[0]
    unit = roster.units[unit_id]
    cycle.day = local_day(start.occurred_at, roster.sites[unit.site_id])
    if not ends:
        cycle.state = "pending"
        cycle.issues.append("missing_terminal")
        return cycle
    if len(ends) != 1:
        cycle.issues.append("multiple_terminals")
        return cycle
    cycle.close = close = ends[0]
    elapsed = close.occurred_at - start.occurred_at
    if elapsed <= timedelta() or elapsed > timedelta(seconds=policy.max_cycle_seconds):
        cycle.issues.append("invalid_duration")
        return cycle
    actions = groups["manual"] + groups["alarm"]
    if any(not start.occurred_at <= event.occurred_at <= close.occurred_at for event in actions):
        cycle.issues.append("action_outside_cycle")
        return cycle
    cycle.elapsed_seconds = int(elapsed.total_seconds())
    cycle.manual = bool(groups["manual"])
    cycle.state = "aborted" if close.kind == "abort" else "complete"
    if cycle.state == "aborted":
        cycle.energy = Energy(None, "cycle_aborted")
    else:
        cycle.energy = energy_used(
            groups["meter"],
            start.occurred_at,
            close.occurred_at,
            policy.max_cycle_wh,
            handovers or [],
        )
    return cycle


def resolve_cycles(
    events: Iterable[Event],
    roster: Roster,
    policy: Policy,
    handovers: list[Handover] | None = None,
) -> list[Cycle]:
    handovers = handovers or []
    by_unit: dict[str, list[Handover]] = defaultdict(list)
    for handover in handovers:
        by_unit[handover.unit_id].append(handover)
    cycles = [
        resolve(unit, cycle, group, roster, policy, by_unit.get(unit, []))
        for (unit, cycle), group in cycle_inputs(events)
    ]
    grouped: dict[str, list[Cycle]] = defaultdict(list)
    for cycle in cycles:
        if cycle.usable:
            grouped[cycle.unit_id].append(cycle)
    for observed in grouped.values():
        ordered = sorted(observed, key=lambda cycle: (cycle.start.occurred_at, cycle.cycle_id))
        if not ordered:
            continue
        last_ending = ordered[0]
        for candidate in ordered[1:]:
            if candidate.start.occurred_at < last_ending.close.occurred_at:
                for item in (last_ending, candidate):
                    if item.state != "invalid":
                        item.state = "invalid"
                        item.issues.append("overlapping_cycle")
                        item.energy = Energy(None, "cycle_unresolved")
            if candidate.close.occurred_at > last_ending.close.occurred_at:
                last_ending = candidate
    return cycles
