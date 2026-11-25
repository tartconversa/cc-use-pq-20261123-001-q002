from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection

from fieldproof.common import conflict, encoded, fingerprint, missing, utc_text
from fieldproof.design.registry import Roster, Trial, require_phase
from fieldproof.signals.contracts import HandoverSubmission

ACTIVE_STATUS = ("pending", "approved")


@dataclass(frozen=True)
class Handover:
    handover_id: str
    revision: int
    unit_id: str
    occurred_at: datetime
    old_meter_id: str
    old_reading_wh: int
    new_meter_id: str
    new_reading_wh: int
    gap_low_wh: int | None
    gap_high_wh: int | None
    stoppage_gap: bool
    status: str
    note: str | None
    submission_id: str
    delivery_seq: int

    @classmethod
    def from_row(cls, row) -> "Handover":
        import json

        document = json.loads(row["document"])
        return cls(
            handover_id=row["handover_id"],
            revision=row["revision"],
            unit_id=document["unit_id"],
            occurred_at=datetime.fromisoformat(document["occurred_at"]),
            old_meter_id=document["old_meter_id"],
            old_reading_wh=document["old_reading_wh"],
            new_meter_id=document["new_meter_id"],
            new_reading_wh=document["new_reading_wh"],
            gap_low_wh=document.get("gap_low_wh"),
            gap_high_wh=document.get("gap_high_wh"),
            stoppage_gap=document.get("stoppage_gap", False),
            status=document["status"],
            note=document.get("note"),
            submission_id=document["submission_id"],
            delivery_seq=row["delivery_seq"],
        )

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUS

    @property
    def gap_bounded(self) -> bool:
        return self.gap_low_wh is not None

    def payload(self) -> dict:
        return {
            "handover_id": self.handover_id,
            "revision": self.revision,
            "unit_id": self.unit_id,
            "occurred_at": utc_text(self.occurred_at),
            "old_meter_id": self.old_meter_id,
            "old_reading_wh": self.old_reading_wh,
            "new_meter_id": self.new_meter_id,
            "new_reading_wh": self.new_reading_wh,
            "gap_low_wh": self.gap_low_wh,
            "gap_high_wh": self.gap_high_wh,
            "stoppage_gap": self.stoppage_gap,
            "status": self.status,
            "note": self.note,
            "submission_id": self.submission_id,
            "delivery_sequence": self.delivery_seq,
        }


def current_all(db: Connection, trial_id: str) -> list[Handover]:
    rows = db.execute(
        """WITH latest AS (
                SELECT handover_id, MAX(revision) AS revision
                FROM handover_revision WHERE trial_id=? GROUP BY handover_id
            )
            SELECT h.* FROM handover_revision h JOIN latest USING(handover_id,revision)
            WHERE h.trial_id=? ORDER BY handover_id""",
        (trial_id, trial_id),
    )
    return [Handover.from_row(row) for row in rows]


def as_of(db: Connection, trial_id: str, cut: int) -> list[Handover]:
    rows = db.execute(
        """WITH visible AS (
                SELECT * FROM handover_revision
                WHERE trial_id=? AND delivery_seq<=?
            ), latest AS (
                SELECT handover_id, MAX(revision) AS revision
                FROM visible GROUP BY handover_id
            )
            SELECT v.* FROM visible v JOIN latest USING(handover_id,revision)
            ORDER BY handover_id""",
        (trial_id, cut),
    )
    return [Handover.from_row(row) for row in rows]


def latest(db: Connection, trial_id: str, handover_id: str) -> Handover | None:
    row = db.execute(
        """SELECT * FROM handover_revision
           WHERE trial_id=? AND handover_id=?
           ORDER BY revision DESC LIMIT 1""",
        (trial_id, handover_id),
    ).fetchone()
    return Handover.from_row(row) if row is not None else None


def require_handover(db: Connection, trial_id: str, handover_id: str) -> Handover:
    handover = latest(db, trial_id, handover_id)
    if handover is None:
        raise missing("handover", handover_id)
    return handover


def validate_against_actives(candidate: Handover, actives: list[Handover]):
    """同一设备上的现行交接必须互相一致，重复或冲突的材料在提交时拒绝。

    合法连链中，前一交接的新表可以是后一交接的旧表（spare 在 h1 为新段、
    在 h2 为旧段）；只有同一角色（同为旧段或同为新段）的重复声明才冲突。
    """
    for other in actives:
        if other.handover_id == candidate.handover_id or other.unit_id != candidate.unit_id:
            continue
        pair = sorted((candidate.handover_id, other.handover_id))
        # 同一角色重复出现表号属于重叠段（新段接旧段的连链不在此列）。
        if candidate.old_meter_id == other.old_meter_id:
            if candidate.old_reading_wh == other.old_reading_wh:
                raise conflict(
                    "duplicate_handover_reading",
                    f"The same {candidate.old_meter_id} breakpoint reading is submitted twice",
                    details={"meter_id": candidate.old_meter_id,
                             "reading_wh": candidate.old_reading_wh, "handovers": pair},
                )
            raise conflict(
                "conflicting_handover_reading",
                f"Active handovers disagree on the {candidate.old_meter_id} breakpoint reading",
                details={"meter_id": candidate.old_meter_id, "handovers": pair},
            )
        if candidate.new_meter_id == other.new_meter_id:
            if candidate.new_reading_wh == other.new_reading_wh:
                raise conflict(
                    "duplicate_handover_reading",
                    f"The same {candidate.new_meter_id} breakpoint reading is submitted twice",
                    details={"meter_id": candidate.new_meter_id,
                             "reading_wh": candidate.new_reading_wh, "handovers": pair},
                )
            raise conflict(
                "conflicting_handover_reading",
                f"Active handovers disagree on the {candidate.new_meter_id} breakpoint reading",
                details={"meter_id": candidate.new_meter_id, "handovers": pair},
            )
        if candidate.occurred_at == other.occurred_at:
            raise conflict(
                "ambiguous_handover_order",
                "Two handovers for the device share a handover instant",
                details={"handovers": pair},
            )


def submit(
    db: Connection,
    trial: Trial,
    roster: Roster,
    request: HandoverSubmission,
) -> dict:
    trial_id = trial.trial_id
    require_phase(trial, "open")
    previous_delivery = db.execute(
        "SELECT seq,digest FROM delivery WHERE trial_id=? AND delivery_id=?",
        (trial_id, request.submission_id),
    ).fetchone()
    previous = latest(db, trial_id, request.handover_id)
    if previous_delivery is not None:
        prior = db.execute(
            """SELECT document FROM handover_revision
               WHERE trial_id=? AND delivery_seq=?""",
            (trial_id, previous_delivery["seq"]),
        ).fetchone()
        if prior is None or previous is None or prior["document"] != encoded(
            _document(db, trial_id, request, previous)
        ):
            raise conflict(
                "submission_changed",
                "Submission identifier was reused with other content",
            )
        return previous.payload()
    if request.action in {"approve", "withdraw"}:
        if previous is None:
            raise missing("handover", request.handover_id)
        target = "approved" if request.action == "approve" else "withdrawn"
        if previous.status == target:
            raise conflict(
                "handover_status_unchanged",
                f"The handover is already {target}",
                details={"handover_id": request.handover_id, "status": target},
            )
    elif previous is None and request.action == "correct":
        raise missing("handover", request.handover_id)
    if request.action == "register" and previous is not None:
        raise conflict(
            "handover_exists",
            "Use action 'correct' to change a registered handover",
            details={"handover_id": request.handover_id, "current_revision": previous.revision},
        )
    if request.action in {"register", "correct"} and request.unit_id not in roster.units:
        raise missing("unit", request.unit_id)
    document = _document(db, trial_id, request, previous)
    candidate = Handover(
        handover_id=request.handover_id,
        revision=(previous.revision + 1) if previous is not None else 1,
        unit_id=document["unit_id"],
        occurred_at=datetime.fromisoformat(document["occurred_at"]),
        old_meter_id=document["old_meter_id"],
        old_reading_wh=document["old_reading_wh"],
        new_meter_id=document["new_meter_id"],
        new_reading_wh=document["new_reading_wh"],
        gap_low_wh=document.get("gap_low_wh"),
        gap_high_wh=document.get("gap_high_wh"),
        stoppage_gap=document.get("stoppage_gap", False),
        status=document["status"],
        note=document.get("note"),
        submission_id=document["submission_id"],
        delivery_seq=-1,
    )
    if candidate.active:
        validate_against_actives(
            candidate,
            [item for item in current_all(db, trial_id) if item.active],
        )
    digest = fingerprint(document)
    result = db.execute(
        "INSERT INTO delivery(trial_id, delivery_id, digest, accepted) VALUES (?,?,?,1)",
        (trial_id, request.submission_id, digest),
    )
    seq = result.lastrowid
    db.execute(
        """INSERT INTO handover_revision(trial_id,handover_id,revision,unit_id,
                                         delivery_seq,document,digest)
           VALUES (?,?,?,?,?,?,?)""",
        (
            trial_id,
            request.handover_id,
            candidate.revision,
            document["unit_id"],
            seq,
            encoded(document),
            digest,
        ),
    )
    return {**candidate.payload(), "delivery_seq": seq, "delivery_sequence": seq}


def _document(
    db: Connection,
    trial_id: str,
    request: HandoverSubmission,
    previous: Handover | None,
) -> dict:
    if request.action in {"register", "correct"}:
        return {
            "unit_id": request.unit_id,
            "occurred_at": request.occurred_at.isoformat(),
            "old_meter_id": request.old_meter_id,
            "old_reading_wh": request.old_reading_wh,
            "new_meter_id": request.new_meter_id,
            "new_reading_wh": request.new_reading_wh,
            "gap_low_wh": request.gap_low_wh,
            "gap_high_wh": request.gap_high_wh,
            "stoppage_gap": request.stoppage_gap,
            "status": "pending",
            "note": request.note,
            "submission_id": request.submission_id,
        }
    assert previous is not None
    return {
        "unit_id": previous.unit_id,
        "occurred_at": utc_text(previous.occurred_at),
        "old_meter_id": previous.old_meter_id,
        "old_reading_wh": previous.old_reading_wh,
        "new_meter_id": previous.new_meter_id,
        "new_reading_wh": previous.new_reading_wh,
        "gap_low_wh": previous.gap_low_wh,
        "gap_high_wh": previous.gap_high_wh,
        "stoppage_gap": previous.stoppage_gap,
        "status": "approved" if request.action == "approve" else "withdrawn",
        "note": request.note,
        "submission_id": request.submission_id,
    }


def history(db: Connection, trial_id: str, handover_id: str) -> list[dict]:
    rows = db.execute(
        """SELECT * FROM handover_revision WHERE trial_id=? AND handover_id=?
           ORDER BY revision""",
        (trial_id, handover_id),
    )
    items = [
        {**Handover.from_row(row).payload(), "revision": row["revision"]}
        for row in rows
    ]
    if not items:
        raise missing("handover", handover_id)
    return items
