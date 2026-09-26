#!/usr/bin/env python3
"""Organ allocation and cold-chain coordination service (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PORT = 8203
ROLES = {"viewer", "hospital", "coordinator", "allocation_officer", "auditor"}
STATUSES = {"proposed", "accepted", "in_transit", "handed_off", "implanted", "withdrawn", "expired", "timed_out"}
# 提出分配时未显式给截止时间，则默认给接收医院的应答窗口（分钟）
DEFAULT_RESPONSE_MINUTES = 120
PRIVILEGED_ROLES = {"coordinator", "allocation_officer", "auditor"}
DESK_ROLES = {"coordinator", "allocation_officer"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message); self.status, self.code, self.message = status, code, message


def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def parse_time(value: str | None) -> datetime:
    if not value: raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def blood_compatible(donor: str, recipient: str) -> bool:
    return {
        "O": {"O", "A", "B", "AB"}, "A": {"A", "AB"}, "B": {"B", "AB"}, "AB": {"AB"},
    }.get(donor.upper(), set()) and recipient.upper() in {"O", "A", "B", "AB"} and recipient.upper() in {
        "O": {"O", "A", "B", "AB"}, "A": {"A", "AB"}, "B": {"B", "AB"}, "AB": {"AB"},
    }.get(donor.upper(), set())


class Repository:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS donors(
            id INTEGER PRIMARY KEY AUTOINCREMENT, blood_type TEXT NOT NULL, organ TEXT NOT NULL, hospital TEXT NOT NULL,
            region TEXT NOT NULL, available_at TEXT NOT NULL, expires_at TEXT NOT NULL, clinical_match INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'available', revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT, patient_name TEXT NOT NULL, blood_type TEXT NOT NULL, organ TEXT NOT NULL,
            hospital TEXT NOT NULL, region TEXT NOT NULL, urgency INTEGER NOT NULL, wait_days INTEGER NOT NULL,
            willing INTEGER NOT NULL DEFAULT 1, clinical_match INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
            created_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS allocations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, donor_id INTEGER NOT NULL REFERENCES donors(id), candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', revision INTEGER NOT NULL DEFAULT 1,
            cold_chain_temp REAL, delayed_minutes INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, accepted_at TEXT, implanted_at TEXT,
            respond_by TEXT, escalation_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS allocations_active_donor_ux
            ON allocations(donor_id) WHERE status NOT IN ('withdrawn','expired','timed_out');
        CREATE TABLE IF NOT EXISTS allocation_offers(
            id INTEGER PRIMARY KEY AUTOINCREMENT, allocation_id INTEGER NOT NULL REFERENCES allocations(id), round INTEGER NOT NULL,
            donor_patient_hospital TEXT NOT NULL, candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            candidate_patient_name TEXT NOT NULL, candidate_hospital TEXT NOT NULL, reason TEXT NOT NULL,
            offered_at TEXT NOT NULL, responded_at TEXT, triggered_by TEXT,
            UNIQUE(allocation_id, round)
        );
        CREATE TABLE IF NOT EXISTS handoffs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, allocation_id INTEGER NOT NULL REFERENCES allocations(id), from_hospital TEXT NOT NULL,
            to_hospital TEXT NOT NULL, cold_chain_temp REAL NOT NULL, status TEXT NOT NULL DEFAULT 'initiated',
            initiated_by TEXT NOT NULL, accepted_by TEXT, initiated_at TEXT NOT NULL, accepted_at TEXT,
            UNIQUE(allocation_id)
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, allocation_id INTEGER, donor_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL,
            action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        # 旧库迁移：限期应答与顺延计数字段
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(allocations)")}
        if "respond_by" not in existing:
            self.conn.execute("ALTER TABLE allocations ADD COLUMN respond_by TEXT")
        if "escalation_count" not in existing:
            self.conn.execute("ALTER TABLE allocations ADD COLUMN escalation_count INTEGER NOT NULL DEFAULT 0")
        # donor_id 列级唯一改为"仅有效分配唯一"的部分索引，使超时/撤回关闭后可重新提出
        table_sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='allocations'").fetchone()[0]
        needs_rebuild = "donor_id INTEGER NOT NULL UNIQUE" in table_sql.replace("\n", " ")
        if needs_rebuild:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            self.conn.executescript("""
            CREATE TABLE allocations_new(
                id INTEGER PRIMARY KEY AUTOINCREMENT, donor_id INTEGER NOT NULL REFERENCES donors(id), candidate_id INTEGER NOT NULL REFERENCES candidates(id),
                score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', revision INTEGER NOT NULL DEFAULT 1,
                cold_chain_temp REAL, delayed_minutes INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, accepted_at TEXT, implanted_at TEXT, respond_by TEXT, escalation_count INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO allocations_new SELECT id,donor_id,candidate_id,score,status,revision,cold_chain_temp,delayed_minutes,created_by,created_at,updated_at,accepted_at,implanted_at,respond_by,escalation_count FROM allocations;
            DROP TABLE allocations;
            ALTER TABLE allocations_new RENAME TO allocations;
            """)
            self.conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn; self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK"); raise

    @staticmethod
    def audit(conn: sqlite3.Connection, allocation_id: int | None, donor_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute("INSERT INTO audit_log(allocation_id,donor_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
                     (allocation_id, donor_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))


class OrganAllocationService:
    def __init__(self, path: str | Path): self.repo = Repository(path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, hospital = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Hospital", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "hospital" and not hospital: raise ApiError(401, "hospital_required", "医院角色必须提供 X-Hospital")
        return actor, role, hospital

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    def register_donor(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"coordinator", "allocation_officer"}: raise ApiError(403, "donor_forbidden", "当前角色不能登记器官")
        required = ("blood_type", "organ", "hospital", "region", "available_at", "expires_at")
        if any(not body.get(k) for k in required): raise ApiError(400, "missing_fields", f"缺少字段: {', '.join(k for k in required if not body.get(k))}")
        blood, organ = str(body["blood_type"]).upper(), str(body["organ"]).lower()
        if blood not in {"O", "A", "B", "AB"}: raise ApiError(400, "invalid_blood_type", "血型必须为 O/A/B/AB")
        available, expires = parse_time(body["available_at"]), parse_time(body["expires_at"])
        if expires <= available: raise ApiError(400, "invalid_window", "可用窗口结束时间必须晚于开始时间")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO donors(blood_type,organ,hospital,region,available_at,expires_at,clinical_match,created_by,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?)""",
                               (blood, organ, body["hospital"], body["region"], iso(available), iso(expires), int(body.get("clinical_match", 0)), actor, iso()))
            donor_id = cur.lastrowid; Repository.audit(conn, None, donor_id, actor, role, "donor_registered", {"organ": organ, "expires_at": iso(expires)})
            return dict(conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone())

    def register_candidate(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"coordinator", "allocation_officer"}: raise ApiError(403, "candidate_forbidden", "当前角色不能登记候选患者")
        required = ("patient_name", "blood_type", "organ", "hospital", "region")
        if any(not body.get(k) for k in required): raise ApiError(400, "missing_fields", "候选患者基础信息不完整")
        blood = str(body["blood_type"]).upper(); urgency = body.get("urgency"); wait_days = body.get("wait_days", 0)
        if blood not in {"O", "A", "B", "AB"} or not isinstance(urgency, int) or not 1 <= urgency <= 5 or not isinstance(wait_days, int) or wait_days < 0:
            raise ApiError(400, "invalid_candidate", "血型、1-5 紧急度和非负等待天数无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO candidates(patient_name,blood_type,organ,hospital,region,urgency,wait_days,willing,clinical_match,created_by,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                               (body["patient_name"], blood, str(body["organ"]).lower(), body["hospital"], body["region"], urgency, wait_days,
                                int(body.get("willing", True)), int(body.get("clinical_match", 0)), actor, iso()))
            return dict(conn.execute("SELECT * FROM candidates WHERE id=?", (cur.lastrowid,)).fetchone())

    @staticmethod
    def _score(donor: sqlite3.Row, candidate: sqlite3.Row) -> dict[str, float]:
        region = 250 if donor["region"] == candidate["region"] else 0
        clinical = min(donor["clinical_match"], candidate["clinical_match"]) * 30
        return {"urgency": candidate["urgency"] * 1000, "waiting": candidate["wait_days"] * 2, "region": region, "clinical": clinical,
                "total": candidate["urgency"] * 1000 + candidate["wait_days"] * 2 + region + clinical}

    def ranking(self, donor_id: int, role: str, hospital: str) -> dict[str, Any]:
        if role not in {"allocation_officer", "auditor"}: raise ApiError(403, "ranking_forbidden", "只有分配员或审计员可以查看完整候选排序")
        with self.repo.tx() as conn:
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone()
            if not donor: raise ApiError(404, "donor_not_found", "器官不存在")
            rows = []
            for candidate in conn.execute("SELECT * FROM candidates WHERE organ=? AND status='active' AND willing=1", (donor["organ"],)):
                if blood_compatible(donor["blood_type"], candidate["blood_type"]):
                    item = dict(candidate); item["match"] = self._score(donor, candidate); rows.append(item)
            rows.sort(key=lambda item: (-item["match"]["total"], item["id"]))
            for index, item in enumerate(rows, 1): item["rank"] = index
            return {"donor": dict(donor), "candidates": rows}

    def propose(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "allocation_officer": raise ApiError(403, "allocate_forbidden", "只有分配员可以提出分配")
        donor_id, candidate_id = body.get("donor_id"), body.get("candidate_id")
        if not isinstance(donor_id, int) or not isinstance(candidate_id, int): raise ApiError(400, "ids_required", "donor_id 和 candidate_id 必填")
        with self.repo.tx() as conn:
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone(); candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if not donor or not candidate: raise ApiError(404, "not_found", "器官或候选患者不存在")
            if donor["status"] != "available": raise ApiError(409, "donor_unavailable", "器官当前不可分配")
            if parse_time(donor["expires_at"]) <= utcnow():
                conn.execute("UPDATE donors SET status='expired',revision=revision+1 WHERE id=?", (donor_id,))
                Repository.audit(conn, None, donor_id, actor, role, "organ_expired", {"candidate_id": candidate_id})
                raise ApiError(409, "organ_expired", "器官可用窗口已结束")
            if candidate["status"] != "active" or not candidate["willing"]: raise ApiError(409, "candidate_unavailable", "候选患者当前不可接受分配")
            if donor["organ"] != candidate["organ"] or not blood_compatible(donor["blood_type"], candidate["blood_type"]):
                raise ApiError(409, "medical_mismatch", "器官类型或血型不匹配")
            if conn.execute("SELECT 1 FROM allocations WHERE donor_id=? AND status NOT IN ('withdrawn','expired','timed_out')", (donor_id,)).fetchone():
                raise ApiError(409, "already_allocated", "该器官已有有效分配")
            # 限期应答：默认应答窗口；显式传 null 表示旧方式（不设截止时间、不做超时顺延）
            now = utcnow()
            if "respond_by" not in body:
                respond_by = now + timedelta(minutes=DEFAULT_RESPONSE_MINUTES)
            elif body["respond_by"] is None:
                respond_by = None
            else:
                respond_by = parse_time(body["respond_by"])
                if respond_by <= now: raise ApiError(400, "invalid_deadline", "应答截止时间必须晚于当前时间")
                if respond_by > parse_time(donor["expires_at"]): raise ApiError(400, "invalid_deadline", "应答截止时间不能晚于器官可用窗口结束时间")
            score = self._score(donor, candidate)
            proposed_at = iso(now)
            cur = conn.execute("""INSERT INTO allocations(donor_id,candidate_id,score,created_by,created_at,updated_at,respond_by,escalation_count)
                                  VALUES(?,?,?,?,?,?,?,0)""",
                               (donor_id, candidate_id, score["total"], actor, proposed_at, proposed_at, iso(respond_by) if respond_by else None))
            allocation_id = cur.lastrowid
            conn.execute("""INSERT INTO allocation_offers(allocation_id,round,donor_patient_hospital,candidate_id,candidate_patient_name,candidate_hospital,reason,offered_at)
                            VALUES(?,?,?,?,?,?,?,?)""",
                         (allocation_id, 0, donor["hospital"], candidate_id, candidate["patient_name"], candidate["hospital"], "initial", proposed_at))
            conn.execute("UPDATE donors SET status='allocated',revision=revision+1 WHERE id=?", (donor_id,))
            Repository.audit(conn, allocation_id, donor_id, actor, role, "allocation_proposed",
                             {"candidate_id": candidate_id, "score": score, "respond_by": iso(respond_by) if respond_by else None,
                              "response_window_minutes": None if respond_by is None else int((respond_by - now).total_seconds() // 60)})
            return self._allocation(conn, allocation_id, role, "")

    def _ranked_candidates(self, conn: sqlite3.Connection, donor: sqlite3.Row) -> list[dict[str, Any]]:
        """按原排序规则（评分降序、id 升序）返回当前仍符合条件的候选患者。"""
        rows = []
        for candidate in conn.execute("SELECT * FROM candidates WHERE organ=? AND status='active' AND willing=1", (donor["organ"],)):
            if blood_compatible(donor["blood_type"], candidate["blood_type"]):
                item = dict(candidate); item["match"] = self._score(donor, candidate); rows.append(item)
        rows.sort(key=lambda item: (-item["match"]["total"], item["id"]))
        return rows

    def _offers(self, conn: sqlite3.Connection, allocation_id: int, role: str, hospital: str) -> list[dict[str, Any]]:
        offers = [dict(r) for r in conn.execute("SELECT * FROM allocation_offers WHERE allocation_id=? ORDER BY round", (allocation_id,))]
        if role == "hospital":
            for offer in offers:
                if offer["candidate_hospital"] != hospital: offer["candidate_patient_name"] = "***"
        return offers

    def _attach_queue(self, conn: sqlite3.Connection, result: dict[str, Any]) -> None:
        """协调台视角：按原排序列出后面仍符合条件且未拒绝过本次分配的患者。"""
        donor = conn.execute("SELECT * FROM donors WHERE id=?", (result["donor_id"],)).fetchone()
        blocked = {o["candidate_id"] for o in conn.execute("SELECT candidate_id FROM allocation_offers WHERE allocation_id=?", (result["id"],))}
        queue = []
        for item in self._ranked_candidates(conn, donor):
            if item["id"] in blocked: continue
            queue.append({"rank": len(queue) + 1, "candidate_id": item["id"], "patient_name": item["patient_name"],
                          "hospital": item["hospital"], "urgency": item["urgency"], "wait_days": item["wait_days"], "score": item["match"]["total"]})
        result["queue"] = queue

    def _allocation(self, conn: sqlite3.Connection, allocation_id: int, role: str, hospital: str, *, with_queue: bool = False, allow_former: bool = False) -> dict[str, Any]:
        row = conn.execute("""SELECT a.*,d.blood_type donor_blood,d.organ,d.hospital donor_hospital,d.region donor_region,d.available_at,d.expires_at,d.status donor_status,
                                     c.patient_name,c.blood_type candidate_blood,c.hospital candidate_hospital,c.region candidate_region,c.urgency,c.wait_days
                              FROM allocations a JOIN donors d ON d.id=a.donor_id JOIN candidates c ON c.id=a.candidate_id WHERE a.id=?""", (allocation_id,)).fetchone()
        if not row: raise ApiError(404, "allocation_not_found", "分配不存在")
        if role == "hospital" and hospital not in {row["donor_hospital"], row["candidate_hospital"]}:
            was_offered = allow_former and conn.execute("SELECT 1 FROM allocation_offers WHERE allocation_id=? AND candidate_hospital=?", (allocation_id, hospital)).fetchone()
            if not was_offered: raise ApiError(403, "allocation_forbidden", "医院不能查看与本机构无关的分配")
        result = dict(row)
        if role == "hospital" and hospital != row["candidate_hospital"]:
            result["patient_name"] = "***"
        # 当前接收医院与顺延次数直接反映在页面上
        result["current_hospital"] = row["candidate_hospital"]
        result["handoff"] = self._row(conn.execute("SELECT * FROM handoffs WHERE allocation_id=?", (allocation_id,)).fetchone())
        result["offers"] = self._offers(conn, allocation_id, role, hospital)
        if with_queue or role not in {"hospital", "viewer"}:
            self._attach_queue(conn, result)
        return result

    def _ensure_active(self, conn: sqlite3.Connection, allocation_id: int, actor: str, role: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM allocations WHERE id=?", (allocation_id,)).fetchone()
        if not row: raise ApiError(404, "allocation_not_found", "分配不存在")
        if row["status"] in {"withdrawn", "expired", "implanted", "timed_out"}: raise ApiError(409, "allocation_closed", "分配已结束")
        donor = conn.execute("SELECT * FROM donors WHERE id=?", (row["donor_id"],)).fetchone()
        if parse_time(donor["expires_at"]) <= utcnow():
            conn.execute("UPDATE allocations SET status='expired',revision=revision+1,updated_at=? WHERE id=?", (iso(), allocation_id))
            conn.execute("UPDATE donors SET status='expired',revision=revision+1 WHERE id=?", (donor["id"],))
            Repository.audit(conn, allocation_id, donor["id"], actor, role, "allocation_expired", {"reason": "organ_window_elapsed"})
            raise ApiError(409, "organ_expired", "器官已经超过可用时间，禁止继续流转")
        return row

    def _roll_offer(self, conn: sqlite3.Connection, row: sqlite3.Row, actor: str, role: str, reason: str) -> bool:
        """关闭当前应答名额，按原排序顺延给下一位仍符合条件且未拒绝过本次分配的患者。

        每次顺延都在 allocation_offers 保留双方患者、时间和原因。返回是否成功顺延。
        """
        now = utcnow(); now_s = iso(now); aid = row["id"]; round_no = row["escalation_count"]
        donor = conn.execute("SELECT * FROM donors WHERE id=?", (row["donor_id"],)).fetchone()
        prev = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
        conn.execute("UPDATE allocation_offers SET responded_at=?,reason=?,triggered_by=? WHERE allocation_id=? AND round=?",
                     (now_s, reason, actor, aid, round_no))
        blocked = {r["candidate_id"] for r in conn.execute("SELECT candidate_id FROM allocation_offers WHERE allocation_id=?", (aid,))}
        nxt = next((c for c in self._ranked_candidates(conn, donor) if c["id"] not in blocked), None)
        base_detail = {"round": round_no, "candidate_id": prev["id"], "hospital": prev["hospital"],
                       "patient_name": prev["patient_name"], "deadline": row["respond_by"], "responded_at": now_s, "reason": reason}
        if nxt is None:
            donor_status = "expired" if parse_time(donor["expires_at"]) <= now else "available"
            conn.execute("UPDATE allocations SET status='timed_out',revision=revision+1,updated_at=? WHERE id=?", (now_s, aid))
            conn.execute("UPDATE donors SET status=?,revision=revision+1 WHERE id=?", (donor_status, donor["id"]))
            Repository.audit(conn, aid, donor["id"], actor, role, "offer_" + reason, base_detail)
            Repository.audit(conn, aid, donor["id"], actor, role, "allocation_timed_out", {**base_detail, "reason": "no_eligible_candidate"})
            return False
        new_deadline = min(now + timedelta(minutes=DEFAULT_RESPONSE_MINUTES), parse_time(donor["expires_at"]))
        conn.execute("""INSERT INTO allocation_offers(allocation_id,round,donor_patient_hospital,candidate_id,candidate_patient_name,candidate_hospital,reason,offered_at)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (aid, round_no + 1, donor["hospital"], nxt["id"], nxt["patient_name"], nxt["hospital"], "rollover", now_s))
        conn.execute("""UPDATE allocations SET candidate_id=?,score=?,escalation_count=escalation_count+1,revision=revision+1,updated_at=?,respond_by=?
                        WHERE id=?""", (nxt["id"], nxt["match"]["total"], now_s, iso(new_deadline), aid))
        Repository.audit(conn, aid, donor["id"], actor, role, "offer_" + reason, base_detail)
        Repository.audit(conn, aid, donor["id"], actor, role, "allocation_rolled",
                         {"round": round_no + 1, "reason": reason, "from_candidate_id": prev["id"], "from_hospital": prev["hospital"],
                          "to_candidate_id": nxt["id"], "to_hospital": nxt["hospital"], "respond_by": iso(new_deadline)})
        return True

    def sweep_timeouts(self, actor: str, role: str) -> list[int]:
        """协调员/分配员查看协调台时，把所有已过应答期限的 proposed 名额顺延给下一位。"""
        if role not in DESK_ROLES: raise ApiError(403, "desk_forbidden", "只有协调员或分配员可以扫描应答期限")
        rolled: list[int] = []
        with self.repo.tx() as conn:
            rows = conn.execute("SELECT * FROM allocations WHERE status='proposed' AND respond_by IS NOT NULL").fetchall()
            for row in rows:
                donor = conn.execute("SELECT * FROM donors WHERE id=?", (row["donor_id"],)).fetchone()
                if parse_time(donor["expires_at"]) <= utcnow():
                    # 器官窗口已过：关闭该名额，不影响其他名额继续顺延
                    conn.execute("UPDATE allocations SET status='expired',revision=revision+1,updated_at=? WHERE id=?", (iso(), row["id"]))
                    conn.execute("UPDATE donors SET status='expired',revision=revision+1 WHERE id=?", (donor["id"],))
                    Repository.audit(conn, row["id"], donor["id"], actor, role, "allocation_expired", {"reason": "organ_window_elapsed"})
                    continue
                if parse_time(row["respond_by"]) <= utcnow():
                    self._roll_offer(conn, row, actor, role, "response_timeout"); rolled.append(row["id"])
        return rolled

    def accept(self, allocation_id: int, actor: str, role: str, hospital: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "hospital": raise ApiError(403, "hospital_required", "只有接收医院可以接受器官")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
            if candidate["hospital"] != hospital:
                # 非当前接收医院：已拒绝或错过过上一轮时，明确提示已失去机会
                prior = conn.execute("""SELECT reason FROM allocation_offers
                                        WHERE allocation_id=? AND candidate_hospital=? AND responded_at IS NOT NULL
                                        ORDER BY round DESC LIMIT 1""", (allocation_id, hospital)).fetchone()
                if prior: raise ApiError(409, "opportunity_lost", f"该医院已{('拒绝' if prior['reason'] == 'rejected' else '错过')}本次分配，名额已顺延给下一位")
                raise ApiError(403, "wrong_hospital", "只能由候选患者所在医院接受")
            if row["status"] == "accepted": return self._allocation(conn, allocation_id, role, hospital)
            if row["status"] != "proposed": raise ApiError(409, "invalid_transition", "当前状态不能接受")
            if row["revision"] != expected: raise ApiError(409, "revision_conflict", "分配信息已发生变化")
            # 限期应答：期限过后再确认，名额顺延先落库，提交后再返回失去机会提示（旧分配 respond_by 为空，照原方式处理）
            if row["respond_by"] is not None and parse_time(row["respond_by"]) <= utcnow():
                self._roll_offer(conn, row, actor, role, "response_timeout"); deadline = row["respond_by"]
            else:
                deadline = None
                conn.execute("UPDATE allocations SET status='accepted',accepted_at=?,revision=revision+1,updated_at=? WHERE id=?", (iso(), iso(), allocation_id))
                conn.execute("UPDATE allocation_offers SET responded_at=?,reason=?,triggered_by=? WHERE allocation_id=? AND round=?",
                             (iso(), "accepted", actor, allocation_id, row["escalation_count"]))
                Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "allocation_accepted", {"hospital": hospital, "round": row["escalation_count"]})
        if deadline is not None:
            raise ApiError(409, "opportunity_lost", f"应答期限 {deadline} 已过，名额已顺延给下一位候选患者")
        return self.get_allocation(allocation_id, role, hospital)

    def mark_transit(self, allocation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "allocation_officer": raise ApiError(403, "transit_forbidden", "只有分配员可以登记转运")
        temp = body.get("cold_chain_temp")
        if not isinstance(temp, (int, float)) or not -2 <= float(temp) <= 8:
            raise ApiError(409, "cold_chain_violation", "冷链温度必须保持在 -2°C 到 8°C")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            if row["status"] != "accepted": raise ApiError(409, "invalid_transition", "只有已接受分配可以进入转运")
            conn.execute("UPDATE allocations SET status='in_transit',cold_chain_temp=?,revision=revision+1,updated_at=? WHERE id=?", (float(temp), iso(), allocation_id))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "transfer_started", {"cold_chain_temp": temp})
            return self._allocation(conn, allocation_id, role, "")

    def report_delay(self, allocation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"allocation_officer", "hospital"}: raise ApiError(403, "delay_forbidden", "当前角色不能上报延误")
        minutes, reason = body.get("delayed_minutes"), str(body.get("reason", "")).strip()
        if not isinstance(minutes, int) or minutes <= 0 or not reason: raise ApiError(400, "invalid_delay", "delayed_minutes 必须为正整数且 reason 必填")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            conn.execute("UPDATE allocations SET delayed_minutes=delayed_minutes+?,revision=revision+1,updated_at=? WHERE id=?", (minutes, iso(), allocation_id))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "logistics_delay", {"minutes": minutes, "reason": reason, "at_risk": minutes >= 120})
            return self._allocation(conn, allocation_id, role, "")

    def initiate_handoff(self, allocation_id: int, actor: str, role: str, hospital: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "hospital": raise ApiError(403, "handoff_forbidden", "只有医院可以发起交接")
        expected = body.get("expected_revision"); target = str(body.get("to_hospital", "")).strip(); temp = body.get("cold_chain_temp")
        if not isinstance(expected, int) or not target or not isinstance(temp, (int, float)) or not -2 <= float(temp) <= 8:
            raise ApiError(400, "invalid_handoff", "expected_revision、to_hospital 和合规冷链温度必填")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (row["donor_id"],)).fetchone()
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
            if hospital != donor["hospital"]: raise ApiError(403, "wrong_hospital", "只能由器官来源医院发起交接")
            if target != candidate["hospital"]: raise ApiError(409, "wrong_destination", "交接目标必须与候选患者医院一致")
            if row["status"] != "in_transit": raise ApiError(409, "invalid_transition", "只有转运中分配可以交接")
            if row["revision"] != expected: raise ApiError(409, "revision_conflict", "分配版本已变化")
            try:
                cur = conn.execute("""INSERT INTO handoffs(allocation_id,from_hospital,to_hospital,cold_chain_temp,initiated_by,initiated_at)
                                      VALUES(?,?,?,?,?,?)""", (allocation_id, hospital, target, float(temp), actor, iso()))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "handoff_exists", "交接已经登记") from exc
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "handoff_initiated", {"target": target, "cold_chain_temp": temp})
            return {"handoff": dict(conn.execute("SELECT * FROM handoffs WHERE id=?", (cur.lastrowid,)).fetchone()), "allocation": self._allocation(conn, allocation_id, role, hospital)}

    def accept_handoff(self, allocation_id: int, actor: str, role: str, hospital: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "hospital": raise ApiError(403, "handoff_forbidden", "只有医院可以确认交接")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            handoff = conn.execute("SELECT * FROM handoffs WHERE allocation_id=?", (allocation_id,)).fetchone()
            if not handoff: raise ApiError(409, "handoff_missing", "尚未发起交接")
            if handoff["to_hospital"] != hospital: raise ApiError(403, "wrong_hospital", "只能由接收医院确认交接")
            if handoff["status"] == "accepted": return self._allocation(conn, allocation_id, role, hospital)
            conn.execute("UPDATE handoffs SET status='accepted',accepted_by=?,accepted_at=? WHERE id=?", (actor, iso(), handoff["id"]))
            conn.execute("UPDATE allocations SET status='handed_off',revision=revision+1,updated_at=? WHERE id=?", (iso(), allocation_id))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "handoff_accepted", {"handoff_id": handoff["id"]})
            return self._allocation(conn, allocation_id, role, hospital)

    def implant(self, allocation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "allocation_officer": raise ApiError(403, "implant_forbidden", "只有分配员可以确认植入")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            if row["status"] != "handed_off": raise ApiError(409, "invalid_transition", "交接完成后才能确认植入")
            conn.execute("UPDATE allocations SET status='implanted',implanted_at=?,revision=revision+1,updated_at=? WHERE id=?", (iso(), iso(), allocation_id))
            conn.execute("UPDATE donors SET status='used',revision=revision+1 WHERE id=?", (row["donor_id"],))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "organ_implanted", {"candidate_id": row["candidate_id"]})
            return self._allocation(conn, allocation_id, role, "")

    def withdraw(self, allocation_id: int, actor: str, role: str, hospital: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "hospital": raise ApiError(403, "withdraw_forbidden", "只有医院可以撤回")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "撤回原因必填")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
            if hospital != candidate["hospital"]: raise ApiError(403, "wrong_hospital", "只能由候选患者医院撤回")
            if row["status"] not in {"proposed", "accepted"}: raise ApiError(409, "invalid_transition", "转运开始后不能直接撤回")
            # 带截止时间的待应答名额被拒绝：记录拒绝并按原排序自动顺延，该患者不再参与本次分配
            if row["status"] == "proposed" and row["respond_by"] is not None:
                self._roll_offer(conn, row, actor, role, "rejected")
                result = self._allocation(conn, allocation_id, role, hospital, allow_former=True)
                result["reject_reason"] = reason
                return result
            # 已接受后的撤回，以及无截止时间的旧分配：照原方式结束分配、释放器官
            conn.execute("UPDATE allocations SET status='withdrawn',revision=revision+1,updated_at=? WHERE id=?", (iso(), allocation_id))
            conn.execute("UPDATE allocation_offers SET responded_at=?,reason=?,triggered_by=? WHERE allocation_id=? AND round=?",
                         (iso(), "withdrawn", actor, allocation_id, row["escalation_count"]))
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (row["donor_id"],)).fetchone()
            donor_status = "available" if parse_time(donor["expires_at"]) > utcnow() else "expired"
            conn.execute("UPDATE donors SET status=?,revision=revision+1 WHERE id=?", (donor_status, row["donor_id"]))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "allocation_withdrawn", {"reason": reason, "round": row["escalation_count"]})
            result = self._allocation(conn, allocation_id, role, hospital)
            result["reject_reason"] = reason
            return result

    def get_allocation(self, allocation_id: int, role: str, hospital: str) -> dict[str, Any]:
        return self._allocation(self.repo.conn, allocation_id, role, hospital)

    def audit(self, allocation_id: int, role: str) -> list[dict[str, Any]]:
        if role not in {"auditor", "allocation_officer"}: raise ApiError(403, "audit_forbidden", "当前角色不能查看审计记录")
        return [dict(r) for r in self.repo.conn.execute("SELECT actor,role,action,detail_json,created_at FROM audit_log WHERE allocation_id=? ORDER BY id", (allocation_id,))]

    def state(self, actor: str, role: str, hospital: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "hospital":
            donors = [dict(r) for r in conn.execute("SELECT * FROM donors WHERE hospital=?", (hospital,))]
            candidates = [dict(r) for r in conn.execute("SELECT * FROM candidates WHERE hospital=?", (hospital,))]
            allocated = [self._allocation(conn, r["id"], role, hospital)
                         for r in conn.execute("SELECT a.id FROM allocations a JOIN candidates c ON c.id=a.candidate_id WHERE c.hospital=?", (hospital,))]
            missed = [dict(r) for r in conn.execute("""SELECT o.allocation_id,o.round,o.reason,o.offered_at,o.responded_at,a.respond_by
                                                       FROM allocation_offers o JOIN allocations a ON a.id=o.allocation_id
                                                       WHERE o.candidate_hospital=? AND o.responded_at IS NOT NULL AND o.reason IN ('response_timeout','rejected')
                                                       ORDER BY o.allocation_id DESC, o.round DESC""", (hospital,))]
            return {"donors": donors, "candidates": candidates, "allocations": allocated, "missed_offers": missed, "server_time": iso()}
        elif role == "viewer":
            donors = []
            candidates = []
            allocated = [dict(r) for r in conn.execute("SELECT id,status,updated_at FROM allocations WHERE status='implanted' ORDER BY id DESC")]
            return {"donors": donors, "candidates": candidates, "allocations": allocated, "server_time": iso()}
        # 协调员/分配员/审计员：协调台视图。协调员/分配员打开时先扫描逾期名额并自动顺延
        rolled_ids = self.sweep_timeouts(actor, role) if role in DESK_ROLES else []
        donors = [dict(r) for r in conn.execute("SELECT * FROM donors ORDER BY id DESC")]
        candidates = [dict(r) for r in conn.execute("SELECT * FROM candidates ORDER BY id DESC")]
        allocated = [self._allocation(conn, r["id"], role, "") for r in conn.execute("SELECT id FROM allocations ORDER BY id DESC")]
        return {"donors": donors, "candidates": candidates, "allocations": allocated, "rolled_allocation_ids": rolled_ids, "server_time": iso()}


def json_reply(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode(); handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: OrganAllocationService; web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def read_body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if not size: return {}
        try: body = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(body, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return body
    def dispatch_get(self, path: str) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "organ-allocation"}
        actor, role, hospital = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state(actor, role, hospital)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[:2] == ["api", "donors"] and parts[2].isdigit() and parts[3] == "ranking": return 200, self.service.ranking(int(parts[2]), role, hospital)
        if len(parts) == 3 and parts[:2] == ["api", "allocations"] and parts[2].isdigit(): return 200, self.service.get_allocation(int(parts[2]), role, hospital)
        if len(parts) == 4 and parts[:2] == ["api", "allocations"] and parts[2].isdigit() and parts[3] == "audit": return 200, {"audit": self.service.audit(int(parts[2]), role)}
        raise ApiError(404, "not_found", "接口不存在")
    def dispatch_post(self, path: str) -> tuple[int, Any]:
        actor, role, hospital = self.service.identity(self.headers); body = self.read_body(); parts = [p for p in path.split("/") if p]
        actions = {
            "/api/donors": lambda: (201, self.service.register_donor(actor, role, body)),
            "/api/candidates": lambda: (201, self.service.register_candidate(actor, role, body)),
            "/api/allocations": lambda: (201, self.service.propose(actor, role, body)),
        }
        if path in actions: return actions[path]()
        if len(parts) == 4 and parts[:2] == ["api", "allocations"] and parts[2].isdigit():
            aid, action = int(parts[2]), parts[3]
            routes = {
                "accept": lambda: self.service.accept(aid, actor, role, hospital, body),
                "withdraw": lambda: self.service.withdraw(aid, actor, role, hospital, body),
                "transit": lambda: self.service.mark_transit(aid, actor, role, body),
                "delay": lambda: self.service.report_delay(aid, actor, role, body),
                "handoff": lambda: self.service.initiate_handoff(aid, actor, role, hospital, body),
                "handoff-accept": lambda: self.service.accept_handoff(aid, actor, role, hospital, body),
                "implant": lambda: self.service.implant(aid, actor, role, body),
            }
            if action in routes: return 200, routes[action]()
        raise ApiError(404, "not_found", "接口不存在")
    def handle_any(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.dispatch_get(parsed.path) if method == "GET" else self.dispatch_post(parsed.path)
            json_reply(self, status, payload)
        except ApiError as exc: json_reply(self, exc.status, {"error": exc.code, "message": exc.message})
        except Exception as exc: print(f"unhandled error: {exc!r}"); json_reply(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_any("GET")
    def do_POST(self) -> None: self.handle_any("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = OrganAllocationService(db_path); handler = type("OrganHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"}); return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("ORGAN_DB", "organ_allocation.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"organ-allocation listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
