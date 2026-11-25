from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from fieldproof.analysis.cohorts import ARMS, ArmSummary, UnitSummary, by_placement
from fieldproof.design.contracts import Market, Policy
from fieldproof.design.registry import Trial

RATE_METRICS = ("manual", "alarm", "failure")
METRICS = (*RATE_METRICS, "energy_wh", "duration_seconds")


def display(value: Fraction | None, multiplier: int = 1) -> str | None:
    if value is None:
        return None
    scaled = round(value * multiplier * 1_000)
    sign = "-" if scaled < 0 else ""
    whole, remainder = divmod(abs(scaled), 1_000)
    return f"{sign}{whole}.{remainder:03d}"


def pair(metric: str, standard: Fraction | None, assisted: Fraction | None) -> dict:
    scale = 10_000 if metric in RATE_METRICS else 1
    delta = assisted - standard if assisted is not None and standard is not None else None
    payload = {
        "standard": display(standard, scale),
        "assisted": display(assisted, scale),
        "assisted_minus_standard": display(delta, scale),
        "unit": "basis_points" if scale == 10_000 else ("Wh" if metric == "energy_wh" else "seconds"),
    }
    if metric == "energy_wh":
        payload["relative_change_bp"] = display(delta / standard, 10_000) if standard and delta is not None else None
    return payload


@dataclass
class CellEstimate:
    key: str
    weight_bp: int
    arms: dict[str, ArmSummary]
    gaps: list[dict]

    def supports(self, name: str) -> bool:
        reasons = {"units", "enrollment_coverage"}
        if name == "energy_wh":
            reasons |= {"energy_units", "energy_coverage"}
        return not any(gap["reason"] in reasons for gap in self.gaps)

    def payload(self) -> dict:
        accounts = {}
        for arm, summary in self.arms.items():
            interval = summary.interval_mean()
            accounts[arm] = {
                **summary.payload(),
                "unit_coverage_bp": display(summary.enrollment_coverage, 10_000),
                "energy_coverage_bp": display(summary.energy_coverage, 10_000),
                "bounded_energy_coverage_bp": display(summary.bounded_coverage, 10_000),
                "energy_interval_wh": (
                    {
                        "low": display(interval[0]),
                        "high": display(interval[1]),
                    }
                    if interval is not None
                    else None
                ),
            }
        return {
            "cell": self.key,
            "weight_bp": self.weight_bp,
            "arms": accounts,
            "gaps": self.gaps,
            "metrics": {
                name: pair(name, self.arms["standard"].mean(name), self.arms["assisted"].mean(name))
                for name in METRICS
            },
        }


def coverage_gaps(arm: str, summary: ArmSummary, policy: Policy) -> list[dict]:
    gaps = []
    usable = len(summary.eligible)
    energy = summary.energy_coverage
    requirements = (
        ("units", usable, policy.required_units_per_arm),
        ("enrollment_coverage", summary.enrollment_coverage * 10_000, policy.minimum_unit_coverage_bp),
        ("energy_units", summary.measured_units, policy.required_units_per_arm),
        ("energy_coverage", None if energy is None else energy * 10_000, 10_000 - policy.max_missing_energy_bp),
    )
    for reason, observed, minimum in requirements:
        if observed is None or observed < minimum:
            gaps.append(
                {"arm": arm, "reason": reason, "observed": display(observed), "minimum": minimum}
            )
    return gaps


def assess_cells(
    market: Market, placed: dict[tuple[str, str, str], list[UnitSummary]], policy: Policy
) -> list[CellEstimate]:
    estimates = []
    for cell in market.cells:
        arms = {
            arm: ArmSummary.assess(placed.get((market.market_id, cell.key, arm), []), policy)
            for arm in ARMS
        }
        gaps = [gap for arm, summary in arms.items() for gap in coverage_gaps(arm, summary, policy)]
        estimates.append(CellEstimate(cell.key, cell.weight_bp, arms, gaps))
    return estimates


def weighted(cells: list[CellEstimate], metric: str, arm: str) -> Fraction | None:
    total = Fraction(0)
    for cell in cells:
        if not cell.supports(metric):
            return None
        value = cell.arms[arm].mean(metric)
        if value is None:
            return None
        total += value * Fraction(cell.weight_bp, 10_000)
    return total


@dataclass
class MarketEstimate:
    market: Market
    cells: list[CellEstimate]
    standard: dict[str, Fraction | None]
    assisted: dict[str, Fraction | None]

    @classmethod
    def build(cls, market: Market, cells: list[CellEstimate]):
        arms = {
            arm: {metric: weighted(cells, metric, arm) for metric in METRICS}
            for arm in ARMS
        }
        return cls(market, cells, arms["standard"], arms["assisted"])

    def delta(self, name: str) -> Fraction | None:
        standard = self.standard[name]
        assisted = self.assisted[name]
        return assisted - standard if assisted is not None and standard is not None else None

    def interval_energy(self, arm: str) -> dict | None:
        """按预登记固定权重汇总的每周期平均耗电区间（瓦时）。"""
        low = Fraction(0)
        high = Fraction(0)
        supported = []
        bounded_units = []
        open_units = []
        for cell in self.cells:
            summary = cell.arms[arm]
            interval = summary.interval_mean()
            if interval is None:
                return None
            weight = Fraction(cell.weight_bp, 10_000)
            low += interval[0] * weight
            high += interval[1] * weight
            bounded_units.extend(summary.bounded_units)
            open_units.extend(summary.open_units)
            if summary.bounded_units or summary.measured_units:
                supported.append(cell.key)
        return {
            "low_wh": display(low),
            "high_wh": display(high),
            "supporting_cells": sorted(set(supported)),
            "bounded_units": sorted(bounded_units),
            "open_energy_units": sorted(open_units),
            "_low": low,
            "_high": high,
        }

    def integrity(self) -> dict:
        units = [
            unit
            for cell in self.cells
            for arm in cell.arms.values()
            for unit in arm.recruited
            if unit.integrity_issues
        ]
        return {
            "unresolved_units": len(units),
            "unresolved_with_alarm": sum(unit.unsafe_unresolved for unit in units),
            "by_site": integrity_by_site(units),
        }

    def energy_ranges(self) -> dict:
        standard = self.interval_energy("standard")
        assisted = self.interval_energy("assisted")
        payload = {
            "interpretation": (
                "Confirmed readings are fixed points; bounded stoppage intervals span "
                "the low-to-high range; open gaps cannot be bounded"
            ),
            "arms": {},
        }
        for arm, interval in (("standard", standard), ("assisted", assisted)):
            if interval is None:
                payload["arms"][arm] = None
                continue
            payload["arms"][arm] = {
                key: value
                for key, value in interval.items()
                if not key.startswith("_")
            }
        if standard is not None and assisted is not None:
            # 最坏情况相对增幅：辅助组上界相对标准组下界。
            worst = (
                (assisted["_high"] - standard["_low"]) / standard["_low"]
                if standard["_low"] > 0
                else None
            )
            best = (
                (assisted["_low"] - standard["_high"]) / standard["_high"]
                if standard["_high"] > 0
                else None
            )
            payload["worst_case_relative_increase_bp"] = (
                display(worst, 10_000) if worst is not None else None
            )
            payload["best_case_relative_change_bp"] = (
                display(best, 10_000) if best is not None else None
            )
            payload["zero_baseline_crossing"] = (
                standard["_low"] == 0 or standard["_high"] == 0
            )
            payload["_standard_low"] = standard["_low"]
            payload["_standard_high"] = standard["_high"]
            payload["_assisted_low"] = assisted["_low"]
            payload["_assisted_high"] = assisted["_high"]
        return payload

    def payload(self) -> dict:
        ranges = self.energy_ranges()
        return {
            "market_id": self.market.market_id,
            "label": self.market.label,
            "cells": [cell.payload() for cell in self.cells],
            "metrics": {
                name: pair(name, self.standard[name], self.assisted[name]) for name in METRICS
            },
            "energy_ranges": {
                key: value for key, value in ranges.items() if not key.startswith("_")
            },
            "integrity": self.integrity(),
        }


def integrity_by_site(units: list[UnitSummary]) -> dict[str, dict[str, int]]:
    from collections import Counter, defaultdict

    totals = defaultdict(Counter)
    for unit in units:
        totals[unit.site_id].update(unit.integrity_issues)
    return {site: dict(sorted(counts.items())) for site, counts in sorted(totals.items())}


def estimate(trial: Trial, units: dict[str, UnitSummary]) -> list[MarketEstimate]:
    placed = by_placement(units.values())
    return [
        MarketEstimate.build(market, assess_cells(market, placed, trial.plan.policy))
        for market in trial.plan.markets
    ]
