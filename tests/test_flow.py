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


class ResponseDeadlineTest(unittest.TestCase):
    """限期应答：截止时间、逾期失去机会、协调台自动顺延与留痕。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()
        self.donor = self.svc.register_donor("coord", "coordinator", {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East",
                                                                      "available_at": iso(self.now - timedelta(days=1)), "expires_at": iso(self.now + timedelta(days=2)), "clinical_match": 8})
        self.jia = self.svc.register_candidate("coord", "coordinator", {"patient_name": "患者甲", "blood_type": "B", "organ": "kidney", "hospital": "H2", "region": "East", "urgency": 5, "wait_days": 500, "willing": True, "clinical_match": 9})
        self.yi = self.svc.register_candidate("coord", "coordinator", {"patient_name": "患者乙", "blood_type": "B", "organ": "kidney", "hospital": "H3", "region": "East", "urgency": 4, "wait_days": 300, "willing": True, "clinical_match": 9})
        self.bing = self.svc.register_candidate("coord", "coordinator", {"patient_name": "患者丙", "blood_type": "B", "organ": "kidney", "hospital": "H4", "region": "East", "urgency": 3, "wait_days": 100, "willing": True, "clinical_match": 9})

    def tearDown(self): self.tmp.cleanup()

    def expire_deadline(self, allocation_id):
        self.svc.repo.conn.execute("UPDATE allocations SET respond_by=? WHERE id=?", (iso(self.now - timedelta(minutes=1)), allocation_id))

    def propose(self, respond_minutes=30, candidate=None):
        return self.svc.propose("allocator", "allocation_officer",
                                {"donor_id": self.donor["id"], "candidate_id": (candidate or self.jia)["id"],
                                 "respond_by": iso(self.now + timedelta(minutes=respond_minutes))})

    def test_propose_carries_deadline_queue_and_initial_offer(self):
        allocation = self.propose()
        self.assertEqual(allocation["status"], "proposed")
        self.assertIsNotNone(allocation["respond_by"])
        self.assertEqual(allocation["escalation_count"], 0)
        self.assertEqual(allocation["current_hospital"], "H2")
        # 协调台看得出后面还有谁：按原排序乙、丙
        self.assertEqual([q["hospital"] for q in allocation["queue"]], ["H3", "H4"])
        # 首轮应答留痕：双方患者、医院、提出时间
        offer = allocation["offers"][0]
        self.assertEqual(offer["round"], 0)
        self.assertEqual(offer["candidate_patient_name"], "患者甲")
        self.assertEqual(offer["candidate_hospital"], "H2")
        self.assertEqual(offer["donor_patient_hospital"], "H1")
        self.assertIsNone(offer["responded_at"])
        self.assertEqual(offer["reason"], "initial")
        # 医院视图不暴露后续候选队列
        hospital_view = self.svc.get_allocation(allocation["id"], "hospital", "H2")
        self.assertNotIn("queue", hospital_view)

    def test_deadline_validation(self):
        with self.assertRaises(ApiError) as ctx:
            self.propose(respond_minutes=-1)
        self.assertEqual(ctx.exception.code, "invalid_deadline")
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer",
                             {"donor_id": self.donor["id"], "candidate_id": self.jia["id"], "respond_by": iso(self.now + timedelta(days=9))})
        self.assertEqual(ctx.exception.code, "invalid_deadline")

    def test_late_confirmation_loses_opportunity_and_rolls_to_next(self):
        allocation = self.propose()
        self.expire_deadline(allocation["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "opportunity_lost")
        rolled = self.svc.get_allocation(allocation["id"], "allocation_officer", "")
        # 当前医院改为排序下一位乙，顺延次数 1，甲不再占名额也不在队列
        self.assertEqual(rolled["candidate_id"], self.yi["id"])
        self.assertEqual(rolled["current_hospital"], "H3")
        self.assertEqual(rolled["escalation_count"], 1)
        self.assertEqual([q["patient_name"] for q in rolled["queue"]], ["患者丙"])
        # 留痕保留双方患者、时间和原因
        closed, opened = rolled["offers"]
        self.assertEqual(closed["candidate_patient_name"], "患者甲")
        self.assertEqual(closed["reason"], "response_timeout")
        self.assertIsNotNone(closed["responded_at"])
        self.assertEqual(opened["candidate_patient_name"], "患者乙")
        self.assertEqual(opened["reason"], "rollover")
        self.assertIsNone(opened["responded_at"])
        # 原医院再确认仍提示失去机会
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": rolled["revision"]})
        self.assertEqual(ctx.exception.code, "opportunity_lost")
        # 下一位医院在新期限内确认成功
        accepted = self.svc.accept(allocation["id"], "hospital-h3", "hospital", "H3", {"expected_revision": rolled["revision"]})
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["candidate_hospital"], "H3")
        # 原医院能在协调台看到自己错过的名额
        h2 = self.svc.state("hospital-h2", "hospital", "H2")
        self.assertEqual([m["reason"] for m in h2["missed_offers"]], ["response_timeout"])

    def test_desk_sweep_rolls_in_ranking_order_until_no_candidate(self):
        allocation = self.propose()
        self.expire_deadline(allocation["id"])
        # 协调员查看协调台触发顺延：甲 -> 乙
        state = self.svc.state("coord", "coordinator", "")
        self.assertEqual(state["rolled_allocation_ids"], [allocation["id"]])
        current = next(a for a in state["allocations"] if a["id"] == allocation["id"])
        self.assertEqual(current["current_hospital"], "H3")
        self.assertEqual(current["escalation_count"], 1)
        # 乙也逾期：查看协调台再顺延到丙，且跳过已错过的甲
        self.expire_deadline(allocation["id"])
        state = self.svc.state("coord", "coordinator", "")
        current = next(a for a in state["allocations"] if a["id"] == allocation["id"])
        self.assertEqual(current["current_hospital"], "H4")
        self.assertEqual(current["escalation_count"], 2)
        # 丙再逾期且后面无人：名额超时关闭，器官释放可供重新分配
        self.expire_deadline(allocation["id"])
        state = self.svc.state("coord", "coordinator", "")
        current = next(a for a in state["allocations"] if a["id"] == allocation["id"])
        self.assertEqual(current["status"], "timed_out")
        donor = next(d for d in state["donors"] if d["id"] == self.donor["id"])
        self.assertEqual(donor["status"], "available")
        # 关闭后可以为该器官重新提出分配
        realloc = self.svc.propose("allocator", "allocation_officer",
                                   {"donor_id": self.donor["id"], "candidate_id": self.bing["id"],
                                    "respond_by": iso(self.now + timedelta(minutes=30))})
        self.assertEqual(realloc["status"], "proposed")
        actions = [item["action"] for item in self.svc.audit(allocation["id"], "auditor")]
        self.assertEqual(actions.count("allocation_rolled"), 2)
        self.assertIn("allocation_timed_out", actions)

    def test_rejected_candidate_is_skipped_on_rollover(self):
        allocation = self.propose()
        result = self.svc.withdraw(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "患者暂不适合"})
        # 拒绝即顺延：当前医院为乙，顺延次数 1，拒绝的甲不在后续队列
        self.assertEqual(result["current_hospital"], "H3")
        self.assertEqual(result["escalation_count"], 1)
        officer_view = self.svc.get_allocation(allocation["id"], "allocation_officer", "")
        self.assertEqual([q["patient_name"] for q in officer_view["queue"]], ["患者丙"])
        self.assertEqual(officer_view["offers"][0]["reason"], "rejected")
        # 乙也逾期后直接跳到丙，不会回到拒绝过的甲
        self.expire_deadline(allocation["id"])
        state = self.svc.state("coord", "coordinator", "")
        current = next(a for a in state["allocations"] if a["id"] == allocation["id"])
        self.assertEqual(current["current_hospital"], "H4")
        self.assertEqual(current["escalation_count"], 2)

    def test_legacy_allocation_without_deadline_keeps_old_behavior(self):
        allocation = self.svc.propose("allocator", "allocation_officer",
                                      {"donor_id": self.donor["id"], "candidate_id": self.jia["id"], "respond_by": None})
        self.assertIsNone(allocation["respond_by"])
        # 不设截止时间的旧分配即使时间走过也照常确认，协调台扫描也不顺延
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")
        state = self.svc.state("coord", "coordinator", "")
        self.assertEqual(state["rolled_allocation_ids"], [])
        actions = [item["action"] for item in self.svc.audit(allocation["id"], "auditor")]
        self.assertEqual(actions, ["allocation_proposed", "allocation_accepted"])

    def test_default_response_window_when_deadline_omitted(self):
        allocation = self.svc.propose("allocator", "allocation_officer",
                                      {"donor_id": self.donor["id"], "candidate_id": self.jia["id"]})
        self.assertIsNotNone(allocation["respond_by"])
        self.assertEqual(allocation["escalation_count"], 0)
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")


if __name__ == "__main__": unittest.main()
