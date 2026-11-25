from __future__ import annotations

from datetime import UTC
from typing import Annotated, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from fieldproof.design.contracts import Input, Key, ShortText, WholeSecond

SignalKind = Literal["start", "complete", "abort", "manual", "alarm", "meter"]
MeterValue = Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
Revision = Annotated[StrictInt, Field(ge=1, le=1_000_000)]
HandoverStatus = Literal["pending", "approved", "withdrawn"]
HandoverAction = Literal["register", "correct", "approve", "withdraw"]


class Event(Input):
    event_id: Key
    revision: Revision
    unit_id: Key
    cycle_id: Key
    occurred_at: WholeSecond
    kind: SignalKind
    void: bool = False
    meter_wh: MeterValue | None = None
    meter_id: Key | None = None
    reason: Key | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value):
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def signal_fields(self):
        if self.void:
            if self.revision == 1:
                raise ValueError("A retraction must revise an existing event")
            if any(v is not None for v in (self.meter_wh, self.meter_id, self.reason)):
                raise ValueError("A retraction cannot contain a measurement or reason")
            return self
        meter = self.kind == "meter"
        if meter != (self.meter_wh is not None) or meter != (self.meter_id is not None):
            raise ValueError("Only meter events carry both a meter ID and a Wh reading")
        if (self.kind in {"abort", "alarm"}) != (self.reason is not None):
            raise ValueError("Only abort and alarm events require reason codes")
        return self

    def identity(self) -> tuple[str, str, str]:
        return self.unit_id, self.cycle_id, self.kind


class Delivery(Input):
    delivery_id: Key
    events: list[Event] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_revisions(self):
        keys = {(event.event_id, event.revision) for event in self.events}
        if len(keys) != len(self.events):
            raise ValueError("A delivery cannot contain an event revision twice")
        return self


class HandoverSubmission(Input):
    """现场登记或更正一次换表交接。

    register/correct 必须给出交接时刻、两个计量段（旧表与新表）及断点读数；
    停表区间若可核查，需同时给出上下界，只给一边不予接受。
    approve/withdraw 只引用交接编号；撤销核准后材料保留但退出当前依据。
    """

    handover_id: Key
    submission_id: Key
    action: HandoverAction
    unit_id: Key | None = None
    occurred_at: WholeSecond | None = None
    old_meter_id: Key | None = None
    old_reading_wh: MeterValue | None = None
    new_meter_id: Key | None = None
    new_reading_wh: MeterValue | None = None
    gap_low_wh: MeterValue | None = None
    gap_high_wh: MeterValue | None = None
    stoppage_gap: bool = False
    note: ShortText | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value):
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def shape_for_action(self):
        full = (
            "unit_id",
            "occurred_at",
            "old_meter_id",
            "old_reading_wh",
            "new_meter_id",
            "new_reading_wh",
        )
        if self.action in {"register", "correct"}:
            absent = [name for name in full if getattr(self, name) is None]
            if absent:
                raise ValueError(f"{self.action} requires fields: {', '.join(absent)}")
            if self.old_meter_id == self.new_meter_id:
                raise ValueError("The two metering segments must use different meter IDs")
        else:
            extras = [
                name
                for name in (*full, "gap_low_wh", "gap_high_wh", "stoppage_gap")
                if getattr(self, name) not in (None, False)
            ]
            if extras:
                raise ValueError(f"{self.action} only references the handover identifier")
        bounds = self.gap_low_wh, self.gap_high_wh
        if any(value is not None for value in bounds):
            if any(value is None for value in bounds):
                raise ValueError("A stoppage bound needs both a low and a high value")
            if self.gap_low_wh > self.gap_high_wh:
                raise ValueError("The stoppage lower bound cannot exceed the upper bound")
        return self
