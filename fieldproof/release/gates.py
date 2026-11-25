from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from fieldproof.analysis.contrast import MarketEstimate, display
from fieldproof.design.contracts import Policy


@dataclass(frozen=True)
class Gate:
    name: str
    state: str
    observed: str | None = None
    boundary: int | None = None
    direction: str | None = None
    reason: str | None = None

    def payload(self) -> dict:
        payload = {
            "gate": self.name,
            "state": self.state,
            "observed": self.observed,
            "boundary": self.boundary,
            "direction": self.direction,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def threshold(name: str, observed: Fraction | None, boundary: int, direction: str) -> Gate:
    if observed is None:
        return Gate(name, "unavailable", boundary=boundary, direction=direction)
    passed = observed <= boundary if direction == "at_most" else observed >= boundary
    return Gate(name, "pass" if passed else "fail", display(observed), boundary, direction)


def in_basis_points(number: Fraction | None) -> Fraction | None:
    return number * 10_000 if number is not None else None


def energy_gate(estimate: MarketEstimate, policy: Policy) -> Gate:
    standard = estimate.standard["energy_wh"]
    assisted = estimate.assisted["energy_wh"]
    allowed = policy.max_energy_increase_bp
    if standard is None or assisted is None:
        return Gate("energy_change_bp", "unavailable", boundary=allowed, direction="at_most")
    if standard == 0:
        return Gate(
            "zero_baseline_energy",
            "pass" if assisted == 0 else "fail",
            display(assisted),
            0,
            "at_most",
        )
    return threshold(
        "energy_change_bp", (assisted - standard) / standard * 10_000, allowed, "at_most"
    )


def open_energy_units(estimate: MarketEstimate) -> list[str]:
    return sorted({
        unit.unit_id
        for cell in estimate.cells
        for arm in cell.arms.values()
        for unit in arm.recruited
        if unit.open_energy_cycles
    })


def interval_energy_gate(estimate: MarketEstimate, policy: Policy) -> Gate | None:
    """区间护栏：只有存在有界或未闭合耗电证据时才出现。

    区间上界仍可能越过护栏则不可放行；区间整体落在护栏内且原定覆盖条件
    满足时，才允许采用保守结果。跨过零耗电基线时不给有限增长比例。
    """
    standard = estimate.interval_energy("standard")
    assisted = estimate.interval_energy("assisted")
    bounded = any(
        cell.arms[arm].bounded_units
        for cell in estimate.cells
        for arm in cell.arms
    )
    open_units = open_energy_units(estimate)
    if not bounded and not open_units:
        return None
    if standard is None or assisted is None:
        return Gate(
            "energy_change_range_bp",
            "unavailable",
            boundary=policy.max_energy_increase_bp,
            direction="at_most",
            reason="interval_energy_unavailable",
        )
    if not bounded and not open_units:
        return None
    if open_units:
        return Gate(
            "energy_change_range_bp",
            "unavailable",
            boundary=policy.max_energy_increase_bp,
            direction="at_most",
            reason="unbounded_gap",
        )
    if standard["_low"] <= 0:
        # 区间跨过零耗电基线：不能解释为有限增长比例，按最坏情况直接判定。
        if assisted["_high"] <= 0:
            return Gate(
                "energy_change_range_bp",
                "pass",
                None,
                policy.max_energy_increase_bp,
                "at_most",
                reason="zero_baseline_no_finite_ratio",
            )
        return Gate(
            "energy_change_range_bp",
            "fail",
            display(assisted["_high"]),
            policy.max_energy_increase_bp,
            "at_most",
            reason="zero_baseline_crossing",
        )
    worst = (assisted["_high"] - standard["_low"]) / standard["_low"] * 10_000
    boundary = Fraction(policy.max_energy_increase_bp)
    if worst <= boundary:
        return Gate(
            "energy_change_range_bp",
            "pass",
            display(worst),
            policy.max_energy_increase_bp,
            "at_most",
        )
    return Gate(
        "energy_change_range_bp",
        "fail",
        display(worst),
        policy.max_energy_increase_bp,
        "at_most",
    )


def assess(estimate: MarketEstimate, policy: Policy) -> dict:
    integrity = estimate.integrity()
    manual = estimate.delta("manual")
    unresolved = integrity["unresolved_units"]
    coverage = not any(cell.gaps for cell in estimate.cells)
    gates = [
        Gate("planned_cell_coverage", "pass" if coverage else "unavailable"),
        threshold("units_with_unresolved_cycles", unresolved, 0, "at_most"),
        threshold("alarm_increase_bp", in_basis_points(estimate.delta("alarm")),
                  policy.max_alarm_increase_bp, "at_most"),
        threshold("failure_increase_bp", in_basis_points(estimate.delta("failure")),
                  policy.max_failure_increase_bp, "at_most"),
        threshold("manual_saving_bp", in_basis_points(-manual if manual is not None else None),
                  policy.min_manual_saving_bp, "at_least"),
        energy_gate(estimate, policy),
    ]
    range_gate = interval_energy_gate(estimate, policy)
    if range_gate is not None:
        gates.append(range_gate)
        # 存在有界证据时，能耗结论以区间护栏为准；定点护栏仅保留作披露。
        energy_names = {"energy_change_bp", "energy_change_range_bp", "zero_baseline_energy"}
        decisive = [gate for gate in gates if gate.name not in energy_names]
        decisive.append(range_gate)
    else:
        decisive = gates
    if any(gate.state == "fail" and gate.name != "units_with_unresolved_cycles"
           for gate in decisive):
        recommendation = "hold"
    elif any(gate.state != "pass" for gate in decisive):
        recommendation = "insufficient"
    else:
        recommendation = "advance"
    return {
        "recommendation": recommendation,
        "gates": [gate.payload() for gate in gates],
        "blocked_by": [gate.name for gate in gates if gate.state != "pass"],
        "decisive_gates": [gate.name for gate in decisive],
        "open_energy_units": open_energy_units(estimate),
    }
