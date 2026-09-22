"""FastAPI 入口：电池管理、日志导入、循环分析与评估导出。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import quote

from .analysis import (Sample, build_assessment, detect_cycles,
                       evaluate_cycle, evaluate_cell_balance)
from .coverage import (LOW_COVERAGE_TIME_RATIO, STATUS_UNASSESSED,
                       assess_cycle_coverage, build_coverage_plan,
                       unknown_coverage)
from .database import get_conn, init_db
from .importer import CsvFormatError, decode_text, parse_flight_csv

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="无人机电池循环健康评估", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------- 模型

class BatteryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    rated_capacity_ah: float = Field(gt=0)
    safe_voltage_min: float = Field(gt=0)
    safe_voltage_max: float = Field(gt=0)
    notes: str = ""


class BatteryUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    rated_capacity_ah: Optional[float] = Field(default=None, gt=0)
    safe_voltage_min: Optional[float] = Field(default=None, gt=0)
    safe_voltage_max: Optional[float] = Field(default=None, gt=0)
    notes: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _get_battery(conn, bid: int) -> dict:
    row = conn.execute("SELECT * FROM batteries WHERE id=?", (bid,)).fetchone()
    if row is None:
        raise HTTPException(404, f"电池 {bid} 不存在")
    return dict(row)


def _check_voltages(vmin: float, vmax: float) -> None:
    if vmin >= vmax:
        raise HTTPException(422, "安全电压下限必须小于上限")


def _quality_brief(audit: Optional[str]) -> Optional[dict]:
    """从 logs.quality_audit JSON 提取列表页用的紧凑质量摘要。"""
    if not audit:
        return None
    data = json.loads(audit)
    return {
        "segment_count": data.get("segment_count", 0),
        "issue_count": data.get("issue_count", 0),
        "time_ordered": data.get("time_ordered", True),
        "counts_by_severity": data.get("counts_by_severity", {}),
        "counts_by_type": data.get("counts_by_type", {}),
        "affected_fields": data.get("affected_fields", []),
        "skipped_rows": data.get("skipped_rows", 0),
    }


def _load_samples_for_coverage(conn, log_id: int) -> list[Sample]:
    """加载某日志全部样本（时间序），用于旧库循环的可信覆盖惰性重算。"""
    rows = conn.execute(
        "SELECT t, current, line_no FROM samples WHERE log_id=? ORDER BY t",
        (log_id,)).fetchall()
    return [Sample(t=r["t"], v=0.0, i=r["current"], temp=0.0, cells=None,
                   line_no=r["line_no"])
            for r in rows]


def _attach_legacy_coverage(conn, cycles: list[dict],
                            audits_by_log: dict[int, Optional[dict]]) -> None:
    """对旧库（cycles.coverage 为空）的放电循环按原始行号惰性重算覆盖评估。

    - 有审计记录：加载该日志样本，用导入时同一份审计证据重算；
    - 无审计记录：标记 unassessed（无法评估，不得视为全可信）。
    """
    pending: dict[int, list[dict]] = {}
    for c in cycles:
        if c.get("coverage") is not None or c["kind"] != "discharge":
            continue
        pending.setdefault(c["log_id"], []).append(c)
    for log_id, cs in pending.items():
        audit = audits_by_log.get(log_id)
        if audit is None:
            for c in cs:
                cov = unknown_coverage(
                    "该日志为旧版导入，无导入前质量审计记录，"
                    "无法评估容量可信覆盖（不得视为全部可信）")
                c["coverage"] = cov
            continue
        samples = _load_samples_for_coverage(conn, log_id)
        plan = build_coverage_plan(audit, samples)
        for c in cs:
            c["coverage"] = assess_cycle_coverage(
                samples,
                _index_at(samples, c["start_t"]),
                _index_at(samples, c["end_t"]),
                plan,
                no_audit_reason="该日志样本缺少 CSV 原始行号，"
                                "无法关联审计证据评估容量可信覆盖")


def _index_at(samples: list[Sample], t: float) -> int:
    """循环边界（端点时刻）-> 时间序样本索引（容差匹配，找不到则最近）。"""
    best, best_i = None, 0
    for k, s in enumerate(samples):
        d = abs(s.t - t)
        if best is None or d < best:
            best, best_i = d, k
    return best_i


# ---------------------------------------------------------------- 电池 CRUD

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/batteries", status_code=201)
def create_battery(body: BatteryIn):
    _check_voltages(body.safe_voltage_min, body.safe_voltage_max)
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO batteries(name, rated_capacity_ah, safe_voltage_min,"
            " safe_voltage_max, notes, created_at) VALUES(?,?,?,?,?,?)",
            (body.name, body.rated_capacity_ah, body.safe_voltage_min,
             body.safe_voltage_max, body.notes, _now_iso()))
        conn.commit()
        return _get_battery(conn, cur.lastrowid)
    finally:
        conn.close()


@app.get("/api/batteries")
def list_batteries():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT b.*, "
            " (SELECT COUNT(*) FROM logs l WHERE l.battery_id=b.id) AS log_count,"
            " (SELECT COUNT(*) FROM cycles c WHERE c.battery_id=b.id"
            "   AND c.kind='discharge') AS discharge_cycles"
            " FROM batteries b ORDER BY b.id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/batteries/{bid}")
def get_battery(bid: int):
    conn = get_conn()
    try:
        return _get_battery(conn, bid)
    finally:
        conn.close()


@app.patch("/api/batteries/{bid}")
def update_battery(bid: int, body: BatteryUpdate):
    conn = get_conn()
    try:
        cur = _get_battery(conn, bid)
        merged = {**cur, **{k: v for k, v in body.model_dump().items()
                            if v is not None}}
        _check_voltages(merged["safe_voltage_min"], merged["safe_voltage_max"])
        conn.execute(
            "UPDATE batteries SET name=?, rated_capacity_ah=?,"
            " safe_voltage_min=?, safe_voltage_max=?, notes=? WHERE id=?",
            (merged["name"], merged["rated_capacity_ah"],
             merged["safe_voltage_min"], merged["safe_voltage_max"],
             merged["notes"], bid))
        conn.commit()
        return _get_battery(conn, bid)
    finally:
        conn.close()


@app.delete("/api/batteries/{bid}", status_code=204)
def delete_battery(bid: int):
    conn = get_conn()
    try:
        _get_battery(conn, bid)
        conn.execute("DELETE FROM batteries WHERE id=?", (bid,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 日志导入

@app.post("/api/batteries/{bid}/logs", status_code=201)
def import_log(bid: int, file: UploadFile = File(...)):
    conn = get_conn()
    try:
        battery = _get_battery(conn, bid)
        raw = file.file.read()
        if not raw:
            raise HTTPException(400, "上传文件为空")
        try:
            samples, meta = parse_flight_csv(decode_text(raw))
        except CsvFormatError as exc:
            raise HTTPException(422, f"CSV 解析失败: {exc}")

        cycles = detect_cycles(
            samples,
            coverage_plan=build_coverage_plan(meta["quality_audit"], samples))
        for cyc in cycles:
            cyc.flags = evaluate_cycle(
                cyc, battery["rated_capacity_ah"],
                battery["safe_voltage_min"], battery["safe_voltage_max"])
            cyc.flags += evaluate_cell_balance(cyc)

        cur = conn.execute(
            "INSERT INTO logs(battery_id, filename, imported_at, start_time,"
            " duration_s, row_count, max_current_a, min_voltage, max_voltage,"
            " max_temp_c, cell_info, quality_audit)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, file.filename or "upload.csv", _now_iso(), meta["start_time"],
             meta["duration_s"], meta["row_count"], meta["max_current_a"],
             meta["min_voltage"], meta["max_voltage"], meta["max_temp_c"],
             json.dumps(meta["cells"], ensure_ascii=False),
             json.dumps(meta["quality_audit"], ensure_ascii=False)))
        log_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO samples(log_id, t, voltage, current, temperature, cells, line_no)"
            " VALUES(?,?,?,?,?,?,?)",
            [(log_id, s.t, s.v, s.i, s.temp,
              json.dumps(list(s.cells)) if s.cells is not None else None,
              s.line_no)
             for s in samples])
        start_epoch = None
        if meta["start_time"]:
            start_epoch = datetime.fromisoformat(
                meta["start_time"]).timestamp()
        for cyc in cycles:
            start_iso = (datetime.fromtimestamp(
                start_epoch + cyc.start_t, tz=timezone.utc).isoformat()
                if start_epoch is not None else None)
            conn.execute(
                "INSERT INTO cycles(log_id, battery_id, seq, kind, start_t,"
                " end_t, start_iso, duration_s, capacity_ah, energy_wh,"
                " avg_current_a, min_voltage, max_voltage, max_temp_c,"
                " temp_rise_c, ir_mohm, closed, flags, cell_count,"
                " cell_max_diff_v, cell_sustain_s, cell_intervals, coverage)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (log_id, bid, cyc.seq, cyc.kind, cyc.start_t, cyc.end_t,
                 start_iso, cyc.duration_s, cyc.capacity_ah, cyc.energy_wh,
                 cyc.avg_current_a, cyc.min_voltage, cyc.max_voltage,
                 cyc.max_temp_c, cyc.temp_rise_c, cyc.ir_mohm,
                 1 if cyc.closed else 0, json.dumps(cyc.flags,
                                                    ensure_ascii=False),
                 cyc.cell_count, cyc.cell_max_diff_v, cyc.cell_sustain_s,
                 json.dumps(cyc.cell_intervals, ensure_ascii=False),
                 json.dumps(cyc.coverage, ensure_ascii=False)
                 if cyc.coverage is not None else None))
        conn.commit()
        log = dict(conn.execute("SELECT * FROM logs WHERE id=?",
                                (log_id,)).fetchone())
        log["cell_info"] = (json.loads(log["cell_info"])
                            if log["cell_info"] else None)
        log["quality_audit"] = (json.loads(log["quality_audit"])
                                if log["quality_audit"] else None)
        return {"log": log, "skipped_rows": meta["skipped_rows"],
                "cells": meta["cells"],
                "quality_audit": meta["quality_audit"],
                "cycles": [c.to_dict() for c in cycles]}
    finally:
        conn.close()


@app.get("/api/batteries/{bid}/logs")
def list_logs(bid: int):
    conn = get_conn()
    try:
        _get_battery(conn, bid)
        rows = conn.execute(
            "SELECT l.*, "
            " (SELECT COUNT(*) FROM cycles c WHERE c.log_id=l.id"
            "   AND c.kind='discharge') AS discharge_cycles,"
            " (SELECT COUNT(*) FROM cycles c WHERE c.log_id=l.id"
            "   AND c.kind='charge') AS charge_cycles"
            " FROM logs l WHERE l.battery_id=? ORDER BY l.id", (bid,)).fetchall()
        result = [dict(r) for r in rows]
        for r in result:
            r["quality"] = _quality_brief(r.pop("quality_audit"))
        return result
    finally:
        conn.close()


@app.get("/api/logs/{log_id}")
def get_log(log_id: int, max_points: int = 3000):
    conn = get_conn()
    try:
        log = conn.execute("SELECT * FROM logs WHERE id=?", (log_id,)).fetchone()
        if log is None:
            raise HTTPException(404, f"日志 {log_id} 不存在")
        rows = conn.execute(
            "SELECT t, voltage, current, temperature, cells, line_no FROM samples"
            " WHERE log_id=? ORDER BY t", (log_id,)).fetchall()
        stride = max(1, len(rows) // max_points + (1 if len(rows) % max_points else 0))
        samples = [[round(r["t"], 2), r["voltage"], r["current"], r["temperature"],
                    json.loads(r["cells"]) if r["cells"] else None, r["line_no"]]
                   for r in rows[::stride]]
        cycles = [dict(r) for r in conn.execute(
            "SELECT * FROM cycles WHERE log_id=? ORDER BY seq",
            (log_id,)).fetchall()]
        for c in cycles:
            c["flags"] = json.loads(c["flags"])
            c["closed"] = bool(c["closed"])
            c["cell_intervals"] = json.loads(c["cell_intervals"])
            c["coverage"] = json.loads(c["coverage"]) if c["coverage"] else None
        audit = json.loads(log["quality_audit"]) if log["quality_audit"] else None
        _attach_legacy_coverage(conn, cycles, {log_id: audit})
        log_dict = dict(log)
        log_dict["cell_info"] = (json.loads(log_dict["cell_info"])
                                 if log_dict["cell_info"] else None)
        log_dict["quality_audit"] = audit
        return {"log": log_dict, "samples": samples, "cycles": cycles}
    finally:
        conn.close()


@app.delete("/api/logs/{log_id}", status_code=204)
def delete_log(log_id: int):
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM logs WHERE id=?", (log_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, f"日志 {log_id} 不存在")
    finally:
        conn.close()


# ---------------------------------------------------------------- 评估与导出

def _build_battery_assessment(conn, bid: int) -> dict:
    battery = _get_battery(conn, bid)
    rows = conn.execute(
        "SELECT * FROM cycles WHERE battery_id=? ORDER BY id", (bid,)).fetchall()
    cycles = []
    for r in rows:
        c = dict(r)
        c["flags"] = json.loads(c["flags"])
        c["closed"] = bool(c["closed"])
        c["cell_intervals"] = json.loads(c["cell_intervals"])
        c["coverage"] = json.loads(c["coverage"]) if c["coverage"] else None
        cycles.append(c)
    log_count = conn.execute(
        "SELECT COUNT(*) AS n FROM logs WHERE battery_id=?", (bid,)).fetchone()["n"]
    # 数据质量审计证据随评估/导出保留（按日志完整区段与逐条证据）
    log_rows = conn.execute(
        "SELECT id, filename, imported_at, row_count, quality_audit"
        " FROM logs WHERE battery_id=? ORDER BY id", (bid,)).fetchall()
    log_audits = []
    audits_by_log: dict[int, Optional[dict]] = {}
    for r in log_rows:
        audit = json.loads(r["quality_audit"]) if r["quality_audit"] else None
        audits_by_log[r["id"]] = audit
        log_audits.append({
            "log_id": r["id"], "filename": r["filename"],
            "imported_at": r["imported_at"], "row_count": r["row_count"],
            "quality_audit": audit,
        })
    # 旧库放电循环无 coverage 时，按审计与原始行号惰性重算或标记无法评估
    _attach_legacy_coverage(conn, cycles, audits_by_log)
    result = build_assessment(battery, cycles, log_count)
    result["battery"] = battery
    result["data_quality"] = _summarize_quality(log_audits)
    result["capacity_coverage"] = _summarize_capacity_coverage(cycles)
    result["logs"] = log_audits
    result["generated_at"] = _now_iso()
    return result


def _summarize_capacity_coverage(cycles: list[dict]) -> dict:
    """跨日志汇总放电容量可信覆盖：低覆盖循环与无法评估循环清单。"""
    dis = [c for c in cycles if c["kind"] == "discharge"]
    assessable, low, unknown = [], [], []
    for c in dis:
        cov = c.get("coverage")
        entry = {"log_id": c["log_id"], "cycle_seq": c["seq"],
                 "capacity_ah": c["capacity_ah"],
                 "start_iso": c.get("start_iso")}
        if not cov or cov.get("status") == STATUS_UNASSESSED:
            entry["reason"] = (cov or {}).get(
                "reason", "无导入前质量审计记录，无法评估")
            unknown.append(entry)
            continue
        entry["trusted_time_ratio"] = cov.get("trusted_time_ratio")
        entry["trusted_capacity_ah"] = cov.get("trusted_capacity_ah")
        entry["suspicious_capacity_ah"] = cov.get("suspicious_capacity_ah")
        entry["suspicious_duration_s"] = cov.get("suspicious_duration_s")
        assessable.append(entry)
        if cov.get("trusted_time_ratio", 0.0) < LOW_COVERAGE_TIME_RATIO:
            entry["reasons"] = sorted({
                code for seg in cov.get("suspicious_segments", [])
                for code in seg.get("reason_codes", [])})
            low.append(entry)
    ratios = [c["coverage"]["trusted_time_ratio"] for c in dis
              if c.get("coverage") and c["coverage"].get("assessable")]
    return {
        "threshold": LOW_COVERAGE_TIME_RATIO,
        "discharge_cycles": len(dis),
        "assessable_cycles": len(assessable),
        "low_coverage_cycles": low,
        "unassessed_cycles": unknown,
        "min_trusted_time_ratio": min(ratios) if ratios else None,
    }


def _summarize_quality(log_audits: list[dict]) -> dict:
    """跨日志聚合导入前质量审计结果，供评估页与导出 JSON 展示。"""
    type_totals: dict[str, int] = {}
    sev_totals = {"info": 0, "warning": 0, "critical": 0}
    affected: set[str] = set()
    logs_with_issues = 0
    total_segments = 0
    total_issues = 0
    unordered_logs = 0
    skipped_rows = 0
    for entry in log_audits:
        audit = entry["quality_audit"]
        if not audit:
            continue
        skipped_rows += audit.get("skipped_rows", 0)
        if not audit.get("time_ordered", True):
            unordered_logs += 1
        if audit.get("segment_count", 0):
            logs_with_issues += 1
        total_segments += audit.get("segment_count", 0)
        total_issues += audit.get("issue_count", 0)
        for typ, cnt in audit.get("counts_by_type", {}).items():
            type_totals[typ] = type_totals.get(typ, 0) + cnt
        for sev, cnt in audit.get("counts_by_severity", {}).items():
            sev_totals[sev] = sev_totals.get(sev, 0) + cnt
        affected.update(audit.get("affected_fields", []))
    grade = "good"
    if sev_totals["critical"] or unordered_logs:
        grade = "critical"
    elif sev_totals["warning"]:
        grade = "fair"
    elif sev_totals["info"]:
        grade = "fair"
    return {
        "total_logs": len(log_audits),
        "logs_with_issues": logs_with_issues,
        "total_segments": total_segments,
        "total_issues": total_issues,
        "unordered_logs": unordered_logs,
        "skipped_rows": skipped_rows,
        "counts_by_type": type_totals,
        "counts_by_severity": sev_totals,
        "affected_fields": sorted(affected),
        "quality_grade": grade,
    }


@app.get("/api/batteries/{bid}/assessment")
def get_assessment(bid: int):
    conn = get_conn()
    try:
        return _build_battery_assessment(conn, bid)
    finally:
        conn.close()


@app.get("/api/batteries/{bid}/export")
def export_assessment(bid: int):
    conn = get_conn()
    try:
        result = _build_battery_assessment(conn, bid)
    finally:
        conn.close()
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    safe_name = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_")
                        else "_" for ch in result["battery"]["name"])
    filename = f"battery_{bid}_{safe_name}_assessment.json"
    # ASCII 回退文件名 + RFC 5987 UTF-8 文件名，兼容中文电池名
    disposition = (f"attachment; filename=\"{filename}\"; "
                   f"filename*=UTF-8''{quote(filename)}")
    return Response(
        content=payload, media_type="application/json",
        headers={"Content-Disposition": disposition})


# ---------------------------------------------------------------- 前端

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
