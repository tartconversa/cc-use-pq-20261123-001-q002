from tests.support import ApiCase, MAIN, ROOT, event


class HandoverRegistrationTest(ApiCase):
    def setUp(self):
        super().setUp()
        self.setup_trial()
        self.swap_at = "2026-10-25T09:05:00+00:00"
        self.rows = self.swap_events("a-one")

    def submit_handover(self, handover_id="h1", submission="sub-h1", **extra):
        body = self.handover_body(
            handover_id, "a-one", self.swap_at, submission=submission, **extra
        )
        return self.handover(handover_id, submission, "register", **{
            key: value for key, value in body.items()
            if key not in {"handover_id", "submission_id", "action"}
        })

    def test_repeated_submission_is_idempotent_and_keeps_one_sequence(self):
        self.deliver("sig", self.rows)
        first = self.submit_handover()
        self.assertEqual((first["status"], first["revision"]), ("pending", 1))
        again = self.submit_handover()
        self.assertEqual(again["delivery_sequence"], first["delivery_sequence"])
        self.assertEqual(again["revision"], 1)
        listing = self.call("GET", ROOT + "/handovers")
        self.assertEqual(len(listing["items"]), 1)

    def test_submission_identifier_reused_with_other_content_is_rejected(self):
        self.deliver("sig", self.rows)
        self.submit_handover()
        problem = self.submit_handover(old_reading=121, status=409)
        self.assertEqual(problem["code"], "submission_changed")

    def test_duplicate_breakpoint_reading_and_meter_role_conflicts_are_flagged(self):
        self.deliver("sig", self.rows)
        self.submit_handover()
        # 同一旧表同一读数的第二次交接：重复提交同一读数。
        duplicate = self.handover_body(
            "h2", "a-one", self.swap_at, old_reading=120, new_id="other"
        )
        problem = self.handover("h2", "sub-h2", "register", status=409, **{
            key: value for key, value in duplicate.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.assertEqual(problem["code"], "duplicate_handover_reading")
        self.assertEqual(problem["details"]["handovers"], ["h1", "h2"])
        # 同一新表号以相同起点读数在两条现行交接中重复安装。
        clash = self.handover_body(
            "h3", "a-one", "2026-10-25T09:06:00+00:00",
            old_id="alpha", old_reading=9, new_id="spare", new_reading=0,
        )
        problem = self.handover("h3", "sub-h3", "register", status=409, **{
            key: value for key, value in clash.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.assertEqual(problem["code"], "duplicate_handover_reading")
        # 同一新表号但读数不同，则是相互冲突而非重复。
        clash = self.handover_body(
            "h4", "a-one", "2026-10-25T09:07:00+00:00",
            old_id="beta", old_reading=9, new_id="spare", new_reading=1,
        )
        problem = self.handover("h4", "sub-h4", "register", status=409, **{
            key: value for key, value in clash.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.assertEqual(problem["code"], "conflicting_handover_reading")

    def test_withdrawal_releases_the_claim_so_a_corrected_handover_can_replace_it(self):
        self.deliver("sig", self.rows)
        self.submit_handover()
        withdrawn = self.handover("h1", "sub-withdraw", "withdraw")
        self.assertEqual(withdrawn["status"], "withdrawn")
        replacement = self.handover_body(
            "h2", "a-one", self.swap_at, old_reading=120, new_reading=0
        )
        accepted = self.handover("h2", "sub-h2", "register", **{
            key: value for key, value in replacement.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.assertEqual(accepted["status"], "pending")
        history = self.call("GET", ROOT + "/handovers/h1")["revisions"]
        self.assertEqual([row["status"] for row in history], ["pending", "withdrawn"])

    def test_approve_twice_and_withdraw_twice_are_conflicts(self):
        self.deliver("sig", self.rows)
        self.submit_handover()
        self.handover("h1", "sub-approve", "approve")
        self.assertEqual(
            self.handover("h1", "sub-again", "approve", status=409)["code"],
            "handover_status_unchanged",
        )
        self.handover("h1", "sub-withdraw", "withdraw")
        self.assertEqual(
            self.handover("h1", "sub-withdraw-2", "withdraw", status=409)["code"],
            "handover_status_unchanged",
        )

    def test_correction_returns_to_pending_and_keeps_the_full_revision_trail(self):
        self.deliver("sig", self.rows)
        self.submit_handover()
        self.handover("h1", "sub-approve", "approve")
        corrected = self.handover(
            "h1", "sub-correct", "correct",
            **{
                key: value
                for key, value in self.handover_body(
                    "h1", "a-one", self.swap_at, old_reading=118
                ).items()
                if key not in {"handover_id", "submission_id", "action"}
            },
        )
        self.assertEqual((corrected["revision"], corrected["status"]), (3, "pending"))
        self.assertEqual(corrected["old_reading_wh"], 118)
        history = self.call("GET", ROOT + "/handovers/h1")["revisions"]
        self.assertEqual([row["revision"] for row in history], [1, 2, 3])
        self.assertEqual([row["status"] for row in history],
                         ["pending", "approved", "pending"])

    def test_handover_requires_an_open_trial_and_known_unit(self):
        body = self.handover_body("h9", "ghost", self.swap_at)
        response = self.handover("h9", "sub-h9", "register", status=404, **{
            key: value for key, value in body.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.assertEqual(response["code"], "unit_missing")

    def test_partial_stoppage_bound_and_reversed_bounds_are_rejected(self):
        self.deliver("sig", self.rows)
        body = self.handover_body("h1", "a-one", self.swap_at, gap_low_wh=5)
        response = self.handover("h1", "sub-h1", "register", status=422, **{
            key: value for key, value in body.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.assertIn("stoppage", response["detail"][0]["msg"].lower())
        body = self.handover_body(
            "h2", "a-one", self.swap_at, gap_low_wh=9, gap_high_wh=2
        )
        response = self.handover("h2", "sub-h2", "register", status=422, **{
            key: value for key, value in body.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.assertIn("lower bound", response["detail"][0]["msg"])


class MeterChainEnergyTest(ApiCase):
    def setUp(self):
        super().setUp()
        self.setup_trial()
        self.swap_at = "2026-10-25T09:05:00+00:00"

    def cycle(self, unit="a-one"):
        return self.call("GET", ROOT + f"/units/{unit}/observations")["cycles"][0]

    def test_without_a_handover_the_chain_is_broken_and_queued_for_material(self):
        self.deliver("sig", self.swap_events("a-one"))
        cycle = self.cycle()
        self.assertEqual(cycle["energy_status"], "broken_meter_chain")
        self.assertEqual(cycle["energy_issue"], "broken_meter_chain")
        queue = self.call("GET", MAIN + "/inspection")
        self.assertEqual(queue["counts"]["handover"], 1)
        self.assertEqual(queue["items"][0]["needed_action"],
                         "register_connecting_handover")

    def test_pending_handover_blocks_the_splice_until_approved(self):
        self.deliver("sig", self.swap_events("a-one"))
        body = self.handover_body("h1", "a-one", self.swap_at)
        self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in body.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        cycle = self.cycle()
        self.assertEqual(cycle["energy_status"], "pending_splice")
        self.assertIsNone(cycle["energy_wh"])
        self.assertEqual(cycle["handover_ids"], ["h1"])
        self.handover("h1", "sub-approve", "approve")
        cycle = self.cycle()
        self.assertEqual(cycle["energy_status"], "spliced")
        # (120-20) + (30-0) = 130 Wh
        self.assertEqual(cycle["energy_wh"], 130)

    def test_breakpoint_mismatch_is_reported_against_the_specific_handover(self):
        self.deliver("sig", self.swap_events("a-one"))
        body = self.handover_body("h1", "a-one", self.swap_at, old_reading=119)
        self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in body.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.handover("h1", "sub-approve", "approve")
        cycle = self.cycle()
        self.assertEqual(cycle["energy_issue"], "handover_reading_mismatch")
        self.assertEqual(cycle["handover_ids"], ["h1"])
        worklist = self.call("GET", MAIN + "/handover-worklist")
        self.assertEqual(worklist["items"][0]["needed_action"],
                         "correct_breakpoint_reading")

    def test_two_swaps_require_a_physically_connected_chain(self):
        t0, t1, t2, t3 = (
            "2026-10-25T09:00:00+00:00", "2026-10-25T09:05:00+00:00",
            "2026-10-25T09:08:00+00:00", "2026-10-25T09:10:00+00:00",
        )
        rows = [
            event("a-one", "a", "start", "start", t0),
            event("a-one", "a", "m0", "meter", t0, meter_id="main", meter_wh=10),
            event("a-one", "a", "m1", "meter", t1, meter_id="main", meter_wh=60),
            event("a-one", "a", "m2", "meter", t1, meter_id="spare", meter_wh=0),
            event("a-one", "a", "m3", "meter", t2, meter_id="spare", meter_wh=20),
            event("a-one", "a", "m4", "meter", t2, meter_id="backup", meter_wh=5),
            event("a-one", "a", "m5", "meter", t3, meter_id="backup", meter_wh=25),
            event("a-one", "a", "end", "complete", t3),
        ]
        self.deliver("sig", rows)
        first = self.handover_body(
            "h1", "a-one", t1, old_reading=60, new_reading=0,
        )
        second = self.handover_body(
            "h2", "a-one", t2, old_id="spare", old_reading=20,
            new_id="backup", new_reading=5,
        )
        for handover_id, submission, body in (
            ("h1", "sub-h1", first),
            ("h2", "sub-h2", second),
        ):
            self.handover(handover_id, submission, "register", **{
                key: value for key, value in body.items()
                if key not in {"handover_id", "submission_id", "action"}
            })
        # 只核准第一条：链在第二条断点处仍待补证。
        self.handover("h1", "sub-approve-1", "approve")
        self.assertEqual(self.cycle()["energy_status"], "pending_splice")
        self.handover("h2", "sub-approve-2", "approve")
        cycle = self.cycle()
        self.assertEqual(cycle["energy_status"], "spliced")
        # 50 + 20 + 20 = 90 Wh across three meter segments
        self.assertEqual(cycle["energy_wh"], 90)
        self.assertEqual(cycle["handover_ids"], ["h1", "h2"])

    def test_bounded_stoppage_gap_yields_a_range_and_unbounded_gap_is_explicit(self):
        self.deliver("sig", self.swap_events("a-one"))
        body = self.handover_body(
            "h1", "a-one", self.swap_at,
            stoppage_gap=True, gap_low_wh=2, gap_high_wh=8,
        )
        self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in body.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.handover("h1", "sub-approve", "approve")
        cycle = self.cycle()
        self.assertEqual(cycle["energy_status"], "bounded")
        # 确认 130 Wh，停表界 2..8
        self.assertEqual((cycle["energy_low_wh"], cycle["energy_high_wh"]), (132, 138))
        # 更正为无界停表：区间消失，缺口显式标出。
        corrected = self.handover_body(
            "h1", "a-one", self.swap_at, stoppage_gap=True
        )
        self.handover("h1", "sub-correct", "correct", **{
            key: value for key, value in corrected.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.handover("h1", "sub-reapprove", "approve")
        cycle = self.cycle()
        self.assertEqual(cycle["energy_status"], "unbounded_gap")
        self.assertIsNone(cycle["energy_low_wh"])
        self.assertTrue(cycle["energy_gaps"][0]["stoppage_gap"])

    def test_as_of_older_cut_still_traces_the_basis_then_in_use(self):
        first_cut = self.deliver("sig", self.swap_events("a-one"))["current_cut"]
        body = self.handover_body("h1", "a-one", self.swap_at)
        registered = self.handover("h1", "sub-h1", "register", **{
            key: value for key, value in body.items()
            if key not in {"handover_id", "submission_id", "action"}
        })
        self.handover("h1", "sub-approve", "approve")
        old = self.call("GET", ROOT + "/units/a-one/observations",
                        params={"cut": first_cut})
        self.assertEqual(old["cycles"][0]["energy_issue"], "broken_meter_chain")
        self.assertEqual(old["handovers"], [])
        at_registration = self.call(
            "GET", ROOT + "/units/a-one/observations",
            params={"cut": registered["delivery_sequence"]},
        )
        self.assertEqual(at_registration["cycles"][0]["energy_status"], "pending_splice")
        current = self.call("GET", ROOT + "/units/a-one/observations")
        self.assertEqual(current["cycles"][0]["energy_status"], "spliced")
