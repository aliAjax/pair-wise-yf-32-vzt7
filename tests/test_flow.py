import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, iso, utcnow


class OrganFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, expires_days=2):
        return self.svc.register_donor("coord", "coordinator", {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East", "available_at": iso(self.now - timedelta(days=3)), "expires_at": iso(self.now + timedelta(days=expires_days)), "clinical_match": 8})

    def candidate(self, name="患者甲", hospital="H2", urgency=5, wait=500):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": name, "blood_type": "B", "organ": "kidney", "hospital": hospital, "region": "East", "urgency": urgency, "wait_days": wait, "willing": True, "clinical_match": 9})

    def test_complete_allocation_and_cold_chain_flow(self):
        donor, candidate = self.donor(), self.candidate()
        rank = self.svc.ranking(donor["id"], "allocation_officer", "")
        self.assertEqual(rank["candidates"][0]["id"], candidate["id"])
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        self.assertEqual(transit["status"], "in_transit")
        handoff = self.svc.initiate_handoff(allocation["id"], "hospital-h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.assertEqual(handoff["handoff"]["status"], "initiated")
        received = self.svc.accept_handoff(allocation["id"], "hospital-h2", "hospital", "H2", {})
        self.assertEqual(received["status"], "handed_off")
        implanted = self.svc.implant(allocation["id"], "allocator", "allocation_officer", {})
        self.assertEqual(implanted["status"], "implanted")
        audit = self.svc.audit(allocation["id"], "auditor")
        self.assertEqual([item["action"] for item in audit], ["allocation_proposed", "allocation_accepted", "transfer_started", "handoff_initiated", "handoff_accepted", "organ_implanted"])

    def test_expiry_privacy_and_single_allocation(self):
        expired = self.donor(expires_days=-1); candidate = self.candidate()
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": expired["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "organ_expired")
        donor2 = self.donor(); allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor2["id"], "candidate_id": candidate["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "wrong", "hospital", "H1", {"expected_revision": 1})
        self.assertEqual(ctx.exception.status, 403)
        masked = self.svc.get_allocation(allocation["id"], "hospital", "H1")
        self.assertEqual(masked["patient_name"], "***")
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor2["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "donor_unavailable")
        other = self.candidate("患者乙", "H2", 4, 300)
        self.assertNotEqual(other["id"], candidate["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 12})
        self.assertEqual(ctx.exception.code, "cold_chain_violation")
    def test_response_deadline_validation(self):
        donor, candidate = self.donor(), self.candidate()
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"], "response_deadline": iso(self.now - timedelta(hours=1))})
        self.assertEqual(ctx.exception.code, "invalid_deadline")
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"], "response_deadline": iso(self.now + timedelta(days=3))})
        self.assertEqual(ctx.exception.code, "invalid_deadline")

    def test_response_deadline_and_deferral(self):
        donor = self.donor()
        first = self.candidate("患者甲", "H2", 5, 500)
        second = self.candidate("患者乙", "H3", 4, 300)
        self.candidate("患者丙", "H4", 3, 100)
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": first["id"], "response_deadline": iso(self.now + timedelta(hours=1))})
        self.assertEqual(allocation["deferral_count"], 0)
        self.assertIsNotNone(allocation["response_deadline"])
        past = iso(self.now - timedelta(minutes=1))
        self.svc.repo.conn.execute("UPDATE allocations SET response_deadline=? WHERE id=?", (past, allocation["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "offer_expired")
        state = self.svc.state("coord", "coordinator", "")
        current = next(a for a in state["allocations"] if a["id"] == allocation["id"])
        self.assertEqual(current["candidate_id"], second["id"])
        self.assertEqual(current["deferral_count"], 1)
        self.assertEqual(current["current_hospital"], "H3")
        self.assertIsNotNone(current["response_deadline"])
        detail = self.svc.get_allocation(allocation["id"], "allocation_officer", "")
        self.assertEqual(len(detail["defers"]), 1)
        defer = detail["defers"][0]
        self.assertEqual((defer["from_candidate_id"], defer["to_candidate_id"]), (first["id"], second["id"]))
        self.assertEqual((defer["from_hospital"], defer["to_hospital"]), ("H2", "H3"))
        self.assertTrue(defer["reason"] and defer["created_at"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": current["revision"]})
        self.assertEqual(ctx.exception.code, "wrong_hospital")
        accepted = self.svc.accept(allocation["id"], "hospital-h3", "hospital", "H3", {"expected_revision": current["revision"]})
        self.assertEqual(accepted["status"], "accepted")
        actions = [item["action"] for item in self.svc.audit(allocation["id"], "auditor")]
        self.assertEqual(actions, ["allocation_proposed", "allocation_deferred", "allocation_accepted"])

    def test_deferral_exhaustion_releases_donor(self):
        donor = self.donor()
        first = self.candidate("患者甲", "H2", 5, 500)
        second = self.candidate("患者乙", "H3", 4, 300)
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": first["id"], "response_deadline": iso(self.now + timedelta(hours=1))})
        past = iso(self.now - timedelta(minutes=1))
        self.svc.repo.conn.execute("UPDATE allocations SET response_deadline=? WHERE id=?", (past, allocation["id"]))
        state = self.svc.state("coord", "coordinator", "")
        current = next(a for a in state["allocations"] if a["id"] == allocation["id"])
        self.assertEqual((current["candidate_id"], current["deferral_count"]), (second["id"], 1))
        self.svc.repo.conn.execute("UPDATE allocations SET response_deadline=? WHERE id=?", (past, allocation["id"]))
        state = self.svc.state("coord", "coordinator", "")
        current = next(a for a in state["allocations"] if a["id"] == allocation["id"])
        self.assertEqual(current["status"], "withdrawn")
        donor_row = next(d for d in state["donors"] if d["id"] == donor["id"])
        self.assertEqual(donor_row["status"], "available")
        actions = [item["action"] for item in self.svc.audit(allocation["id"], "auditor")]
        self.assertEqual(actions, ["allocation_proposed", "allocation_deferred", "allocation_lapsed"])

    def test_allocation_without_deadline_unchanged(self):
        donor, candidate = self.donor(), self.candidate()
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        self.assertIsNone(allocation["response_deadline"])
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")


if __name__ == "__main__": unittest.main()
