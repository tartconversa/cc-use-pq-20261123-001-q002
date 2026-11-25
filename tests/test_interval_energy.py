from tests.support import ApiCase, MAIN, ROOT, enrollment, event, observations, plan

SWAP = "2026-10-25T09:05:00+00:00"


def meter(unit, cyc, label, at, meter_id, wh):
    return event(unit, cyc, label, "meter", at, meter_id=meter_id, meter_wh=wh)


def assisted_swap(gap_low=None, gap_high=None, stoppage=None, *, old_reading=80,
                  new_end=30, old_start=20):
    t0, t1, t2 = (
        "2026-10-25T09:00:00+00:00", SWAP, "2026-10-25T09:10:00+00:00",
    )
    rows = [
        event("a-one", "a", "start", "start", t0),
        meter("a-one", "a", "m0", t0, "main", old_start),
        meter("a-one", "a", "m1", t1, "main", old_reading),
        meter("a-one", "a", "m2", t1, "spare", 0),
        meter("a-one", "a", "m3", t2, "spare", new_end),
        event("a-one", "a", "end", "complete", t2),
    ]
    body = {
        "handover_id": "h1", "submission_id": "sub-h1", "action": "register",
        "unit_id": "a-one", "occurred_at": t1,
        "old_meter_id": "main", "old_reading_wh": old_reading,
        "new_meter_id": "spare", "new_reading_wh": 0,
    }
    if stoppage:
        body["stoppage_gap"] = True
        if gap_low is not None:
            body["gap_low_wh"] = gap_low
            body["gap_high_wh"] = gap_high
    return rows, body


class IntervalEnergyTest(ApiCase):
    def open_with(self, missing_bp):
        trial_plan = plan()
        trial_plan["policy"]["max_missing_energy_bp"] = missing_bp
        self.setup_trial(trial_plan)

    def fill_standard_and_second_assisted(self, *, assisted_wh=90):
        rows = observations("s-one", manual=True) + observations("s-two", manual=True)
        rows += observations("a-two", wh=assisted_wh)
        return rows

    def test_range_reports_supporting_units_coverages_and_worst_case_increase(self):
        self.open_with(10_000)
        swap_rows, handover = assisted_swap(gap_low=0, gap_high=20, stoppage=True)
        self.deliver("sig", self.fill_standard_and_second_assisted() + swap_rows)
        self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in handover.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.handover("h1", "sub-approve", "approve")
        result = self.draft()["market_results"][0]
        ranges = result["energy_ranges"]
        self.assertEqual(ranges["arms"]["standard"]["low_wh"], "100.000")
        self.assertEqual(ranges["arms"]["assisted"]["high_wh"], "100.000")
        self.assertEqual(ranges["arms"]["assisted"]["bounded_units"], ["a-one"])
        self.assertEqual(ranges["worst_case_relative_increase_bp"], "0.000")
        cell = result["cells"][0]["arms"]["assisted"]
        self.assertEqual(cell["bounded_energy_units"], 1)
        # 完整计量覆盖只有 a-two，有界覆盖把 a-one 也计入。
        self.assertEqual(cell["energy_coverage_bp"], "5000.000")
        self.assertEqual(cell["bounded_energy_coverage_bp"], "10000.000")

    def test_favorable_interval_still_holds_when_worst_case_crosses_the_guardrail(self):
        self.open_with(10_000)
        # 确认 90，停表上界 200：辅助组均值上界 (290+90)/2=190，标准 100。
        swap_rows, handover = assisted_swap(gap_low=0, gap_high=200, stoppage=True)
        self.deliver("sig", self.fill_standard_and_second_assisted() + swap_rows)
        self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in handover.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.handover("h1", "sub-approve", "approve")
        result = self.draft()["market_results"][0]
        range_gate = next(g for g in result["gates"] if g["gate"] == "energy_change_range_bp")
        self.assertEqual(range_gate["state"], "fail")
        self.assertEqual(range_gate["observed"], "9000.000")
        self.assertEqual(result["recommendation"], "hold")

    def test_unbounded_gap_is_explicit_and_blocks_the_range_gate(self):
        self.open_with(10_000)
        swap_rows, handover = assisted_swap(stoppage=True)
        self.deliver("sig", self.fill_standard_and_second_assisted() + swap_rows)
        self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in handover.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.handover("h1", "sub-approve", "approve")
        result = self.draft()["market_results"][0]
        range_gate = next(g for g in result["gates"] if g["gate"] == "energy_change_range_bp")
        self.assertEqual(range_gate["state"], "unavailable")
        self.assertEqual(range_gate["reason"], "unbounded_gap")
        self.assertEqual(result["open_energy_units"], ["a-one"])
        self.assertEqual(result["recommendation"], "insufficient")
        worklist = self.call("GET", MAIN + "/handover-worklist")
        self.assertEqual(worklist["items"][0]["needed_action"],
                         "provide_stoppage_bounds_or_approve_chain")

    def test_interval_inside_guardrail_with_coverage_met_allows_conservative_advance(self):
        self.open_with(10_000)
        swap_rows, handover = assisted_swap(gap_low=0, gap_high=20, stoppage=True)
        self.deliver("sig", self.fill_standard_and_second_assisted() + swap_rows)
        self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in handover.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.handover("h1", "sub-approve", "approve")
        result = self.draft()["market_results"][0]
        range_gate = next(g for g in result["gates"] if g["gate"] == "energy_change_range_bp")
        self.assertEqual(range_gate["state"], "pass")
        self.assertEqual(result["recommendation"], "advance")

    def test_zero_baseline_crossing_never_reports_a_finite_relative_ratio(self):
        self.open_with(10_000)
        zero_rows = []
        t0, t2 = "2026-10-25T09:00:00+00:00", "2026-10-25T09:10:00+00:00"
        for unit in ("s-one", "s-two"):
            zero_rows += [
                event(unit, "a", "start", "start", t0),
                meter(unit, "a", "m0", t0, "main", 0),
                meter(unit, "a", "m1", t2, "main", 0),
                event(unit, "a", "end", "complete", t2),
            ]
        swap_rows, handover = assisted_swap(
            gap_low=0, gap_high=20, stoppage=True, old_reading=0, old_start=0
        )
        zero_rows += swap_rows
        zero_rows += observations("a-two", wh=0)
        self.deliver("sig", zero_rows)
        self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in handover.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.handover("h1", "sub-approve", "approve")
        result = self.draft()["market_results"][0]
        self.assertIsNone(result["energy_ranges"]["worst_case_relative_increase_bp"])
        self.assertTrue(result["energy_ranges"]["zero_baseline_crossing"])
        # 定点值不可用时，零基线规则由区间护栏承担，不产出有限比例。
        range_gate = next(
            g for g in result["gates"] if g["gate"] == "energy_change_range_bp"
        )
        self.assertEqual(range_gate["state"], "fail")
        self.assertEqual(range_gate["reason"], "zero_baseline_crossing")
        # 最坏情况相对增幅不给有限数值，只有上界电量用于判定。
        self.assertIsNone(result["energy_ranges"]["worst_case_relative_increase_bp"])
        self.assertEqual(range_gate["observed"], result["energy_ranges"]["arms"]["assisted"]["high_wh"])
        zero_gate = next(
            (g for g in result["gates"] if g["gate"] == "zero_baseline_energy"), None
        )
        if zero_gate is not None:
            # 定点值全为零时定点护栏通过，跨零风险完全由区间护栏拦下。
            self.assertEqual(zero_gate["state"], "pass")
        self.assertEqual(result["recommendation"], "hold")


class FrozenConsistencyTest(ApiCase):
    def test_preview_freeze_and_bulletin_share_the_same_interval_verdict(self):
        trial_plan = plan()
        trial_plan["policy"]["max_missing_energy_bp"] = 10_000
        opened = self.setup_trial(trial_plan)
        standard = observations("s-one", manual=True) + observations("s-two", manual=True)
        swap_rows, handover = assisted_swap(gap_low=0, gap_high=20, stoppage=True)
        rows = standard + swap_rows + observations("a-two", wh=90)
        cut = self.deliver("sig", rows)["current_cut"]
        self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in handover.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        cut = self.handover("h1", "sub-approve", "approve")["delivery_sequence"]
        preview = self.draft()["market_results"][0]
        self.close(opened["version"], cut)
        frozen = self.freeze(cut)["market_results"][0]
        self.assertEqual(
            preview["energy_ranges"], frozen["energy_ranges"]
        )
        self.assertEqual(preview["gates"], frozen["gates"])
        evidence_id = self.freeze(cut)["evidence_id"]
        self.review(evidence_id, "field", "ada")
        self.review(evidence_id, "methods", "bea")
        bulletin = self.issue(evidence_id)
        self.assertEqual(bulletin["result"]["recommendation"], "advance")
        self.assertEqual(
            bulletin["result"]["energy_ranges"], frozen["energy_ranges"]
        )
        # 封存证据内嵌当时的交接材料，旧截点的依据可以回读。
        document = self.call("GET", ROOT + f"/evidence/{evidence_id}")
        self.assertEqual(document["handovers"][0]["status"], "approved")
        self.assertEqual(document["handovers"][0]["handover_id"], "h1")

    def test_withdrawn_approval_moves_the_queue_and_period_reading_together(self):
        self.setup_trial()
        cut = self.deliver("sig", self.swap_events("a-one"))["current_cut"]
        body = self.handover_body("h1", "a-one", SWAP)
        self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in body.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.handover("h1", "sub-approve", "approve")
        spliced = self.draft()["market_results"][0]
        self.assertEqual(
            self.call("GET", ROOT + "/units/a-one/observations")["cycles"][0]["energy_status"],
            "spliced",
        )
        withdrawn_cut = self.handover(
            "h1", "sub-withdraw", "withdraw"
        )["delivery_sequence"]
        self.assertEqual(
            self.call("GET", ROOT + "/units/a-one/observations")["cycles"][0]["energy_status"],
            "pending_splice",
        )
        worklist = self.call("GET", MAIN + "/handover-worklist")
        self.assertEqual(worklist["items"][0]["handover_id"], "h1")
        self.assertEqual(worklist["items"][0]["status"], "withdrawn")
        # 旧截点仍能追到当时采用的核准依据。
        old = self.call("GET", ROOT + "/units/a-one/observations",
                        params={"cut": cut})
        self.assertEqual(old["cycles"][0]["energy_issue"], "broken_meter_chain")
        at_approval = self.call(
            "GET", ROOT + "/units/a-one/observations",
            params={"cut": withdrawn_cut - 1},
        )
        self.assertEqual(at_approval["cycles"][0]["energy_status"], "spliced")
        # 周期对账能反映这次状态翻转。
        reconciliation = self.call(
            "GET", ROOT + "/reconciliation",
            params={"before_cut": withdrawn_cut - 1, "after_cut": withdrawn_cut},
        )
        self.assertEqual(reconciliation["total_changed_cycles"], 1)
        self.assertIn("energy_status", reconciliation["cycles"][0]["changed_fields"])
        history = self.call("GET", ROOT + "/handovers/h1")["revisions"]
        self.assertEqual([row["status"] for row in history],
                         ["pending", "approved", "withdrawn"])
