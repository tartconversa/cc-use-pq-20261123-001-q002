from contextlib import redirect_stderr, redirect_stdout
import json
from io import StringIO

from fieldproof.cli import main
from tests.support import ApiCase, ROOT


class HandoverCliTest(ApiCase):
    def setUp(self):
        super().setUp()
        self.setup_trial()
        self.document = self.db.parent / "handover.json"

    def run_cli(self, payload):
        self.document.write_text(json.dumps(payload), encoding="utf-8")
        out, error = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            code = main([
                "--db", str(self.db), "submit-handover", "valley", str(self.document),
            ])
        return code, json.loads(out.getvalue() if code == 0 else error.getvalue())

    def test_cli_registers_and_approves_a_handover_against_the_same_contract(self):
        payload = {
            "handover_id": "h1", "submission_id": "sub-h1", "action": "register",
            "unit_id": "a-one", "occurred_at": "2026-10-25T09:05:00+00:00",
            "old_meter_id": "main", "old_reading_wh": 120,
            "new_meter_id": "spare", "new_reading_wh": 0,
        }
        code, result = self.run_cli(payload)
        self.assertEqual((code, result["status"], result["revision"]), (0, "pending", 1))
        code, result = self.run_cli({
            "handover_id": "h1", "submission_id": "sub-approve", "action": "approve",
        })
        self.assertEqual((code, result["status"]), (0, "approved"))
        listing = self.call("GET", ROOT + "/handovers/h1")
        self.assertEqual([row["status"] for row in listing["revisions"]],
                         ["pending", "approved"])

    def test_cli_rejects_a_malformed_document_without_touching_the_trial(self):
        code, result = self.run_cli({
            "handover_id": "h9", "submission_id": "sub-h9", "action": "register",
            "unit_id": "a-one",
        })
        self.assertEqual(code, 1)
        self.assertEqual(result["code"], "input_error")
        self.assertEqual(self.call("GET", ROOT + "/handovers")["items"], [])
