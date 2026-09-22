"""放电容量可信覆盖评估。

以 CSV 原始行号为桥梁，把导入前质量审计（app/audit.py）的区段证据映射回
时间排序后的样本序列，对每个放电循环**逐段（每个梯形积分区间）重算库仑积分**，
并按审计证据把每个梯形区间划分为可信段或可疑段：

- **电流离群点相邻区间**（离群样本前后各一个梯形区间）列为可疑段：
  坏点电流直接参与相邻两个梯形的面积，这两段积分不可信；
- **采样缺口区间**（缺口两侧样本之间的梯形区间）列为可疑段：
  缺口内无采样，梯形面积只是对缺失数据的粗略近似；
- 其余区间为可信段。

审计区段的逐条明细被截断（``details_truncated``）时无法得知全部问题行号，
此时**保守处理**：该区段覆盖的整个行范围（电流离群点再向两侧各扩一个区间）
都按可疑处理，宁可多标不可漏标。

评估结果分别给出可信/可疑段的容量、累计时长与可信时长占比。它只是对既有
容量积分"有多可信"的描述：可信段容量只是可信时段的部分积分，**绝不是电池
真实总容量**；既有容量、SOH 与健康等级仍基于完整积分，不受本评估影响。
旧库日志没有导入质量审计记录时标记为"无法评估"，不能视作全可信。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence

from .audit import TYPE_GAP, TYPE_OUTLIER

if TYPE_CHECKING:  # 仅类型标注，避免与 analysis 循环导入
    from .analysis import Sample

# ---- 可调阈值 ----
LOW_COVERAGE_RATIO = 0.95   # 可信时长占比低于该值视为低覆盖循环
MAX_EVIDENCE_LINES = 20     # 每个可疑段保留的原始行号证据上限

REASON_CURRENT_OUTLIER = "current_outlier"
REASON_INTERVAL_GAP = "interval_gap"
REASON_LABELS = {
    REASON_CURRENT_OUTLIER: "电流离群点相邻区间",
    REASON_INTERVAL_GAP: "采样缺口区间",
}

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"


def unavailable_coverage() -> dict:
    """旧库日志无审计记录时的占位结果：无法评估，绝不视作全可信。"""
    return {
        "status": STATUS_UNAVAILABLE,
        "reason": "no_audit_record",
        "message": "该日志缺少导入质量审计记录（旧版数据），"
                   "无法评估放电容量可信覆盖，不能按全可信处理。",
    }


def _iter_suspicious_items(audit: dict):
    """从审计区段中提取可疑项。

    产出 (reason, line_lo, line_hi, evidence_lines, expand)：
    - line_lo..line_hi 为涉及的 CSV 原始行号范围（闭区间）；
    - evidence_lines 为展示用的证据行号（离群点行 / 缺口边界行）；
    - expand 为 True 时（电流离群点）向两侧各多标一个相邻梯形区间。
    """
    for seg in audit.get("segments", []):
        typ = seg.get("type")
        if typ == TYPE_GAP:
            reason = REASON_INTERVAL_GAP
            expand = False
        elif typ == TYPE_OUTLIER and "current" in (seg.get("fields") or []):
            reason = REASON_CURRENT_OUTLIER
            expand = True
        else:
            continue  # 电压/温度离群、过密采样等不影响电流积分，跳过
        if seg.get("details_truncated"):
            # 明细截断：无法确认全部问题行，保守地将区段整个行范围按可疑处理
            yield (reason, seg["line_start"], seg["line_end"],
                   [seg["line_start"], seg["line_end"]], expand)
            continue
        for d in seg.get("details", []):
            if reason == REASON_INTERVAL_GAP:
                yield (reason, d["line_start"], d["line_end"],
                       [d["line_start"], d["line_end"]], expand)
            elif d.get("field") == "current":
                yield (reason, d["line"], d["line"], [d["line"]], expand)


def assess_discharge_coverage(samples: Sequence["Sample"], a: int, b: int,
                              quality_audit: Optional[dict]) -> dict:
    """对放电循环的样本区间 [a, b] 逐段重算梯形积分并划分可信/可疑段。

    samples 为时间排序后的完整样本序列（携带 CSV 原始行号 line_no），
    a/b 为该放电循环在序列中的起止下标（与 detect_cycles 内部一致）。
    """
    if not quality_audit:
        return unavailable_coverage()

    idx_of_line = {s.line_no: k for k, s in enumerate(samples)
                   if s.line_no is not None}

    # 逐梯形标记可疑原因与证据行：梯形下标 k 覆盖样本 k..k+1
    marks: dict[int, dict] = {}

    def mark(k: int, reason: str, line: Optional[int]) -> None:
        if not (a <= k < b):
            return
        m = marks.setdefault(k, {"reasons": set(), "lines": set()})
        m["reasons"].add(reason)
        if line is not None:
            m["lines"].add(line)

    for reason, line_lo, line_hi, ev_lines, expand in _iter_suspicious_items(
            quality_audit):
        idxs = [idx_of_line[ln] for ln in range(line_lo, line_hi + 1)
                if ln in idx_of_line]
        if not idxs:
            continue  # 问题行不在可解析样本中（如落在无法解析行），无需标记
        lo, hi = min(idxs), max(idxs)
        if expand:
            lo -= 1  # 电流离群点：前后相邻区间一并按可疑处理
        for k in range(lo, hi + (1 if expand else 0)):
            for ln in ev_lines:
                mark(k, reason, ln)

    # 逐段重算梯形积分，分别累计可信/可疑段的容量与时长
    trusted_cap = trusted_dur = 0.0
    suspicious_cap = suspicious_dur = 0.0
    trap_dt: dict[int, tuple[float, float]] = {}  # k -> (dt, cap_ah)
    for k in range(a, b):
        dt = samples[k + 1].t - samples[k].t
        if dt <= 0:
            continue
        cap = 0.5 * (abs(samples[k].i) + abs(samples[k + 1].i)) * dt / 3600.0
        trap_dt[k] = (dt, cap)
        if k in marks:
            suspicious_dur += dt
            suspicious_cap += cap
        else:
            trusted_dur += dt
            trusted_cap += cap

    # 相邻可疑梯形合并为展示段，给出原始行号与原因
    suspicious_segments: list[dict] = []
    run: Optional[dict] = None
    for k in sorted(marks):
        if k not in trap_dt:
            continue
        dt, cap = trap_dt[k]
        if run is not None and k == run["k_end"] + 1:
            run["k_end"] = k
            run["reasons"] |= marks[k]["reasons"]
            run["lines"] |= marks[k]["lines"]
            run["duration_s"] += dt
            run["capacity_ah"] += cap
        else:
            if run is not None:
                suspicious_segments.append(_segment_out(samples, run))
            run = {"k_start": k, "k_end": k,
                   "reasons": set(marks[k]["reasons"]),
                   "lines": set(marks[k]["lines"]),
                   "duration_s": dt, "capacity_ah": cap}
    if run is not None:
        suspicious_segments.append(_segment_out(samples, run))

    total_dur = trusted_dur + suspicious_dur
    total_cap = trusted_cap + suspicious_cap
    return {
        "status": STATUS_OK,
        "trusted_capacity_ah": round(trusted_cap, 4),
        "trusted_duration_s": round(trusted_dur, 3),
        "suspicious_capacity_ah": round(suspicious_cap, 4),
        "suspicious_duration_s": round(suspicious_dur, 3),
        "trusted_capacity_ratio": (round(trusted_cap / total_cap, 4)
                                   if total_cap > 0 else None),
        "trusted_duration_ratio": (round(trusted_dur / total_dur, 4)
                                   if total_dur > 0 else None),
        "suspicious_segments": suspicious_segments,
    }


def _segment_out(samples: Sequence["Sample"], run: dict) -> dict:
    """把一段连续可疑梯形整理成可展示/可导出的区段（含原始行号与原因）。"""
    covered = [samples[j].line_no
               for j in range(run["k_start"], run["k_end"] + 2)
               if samples[j].line_no is not None]
    lines = sorted(run["lines"])
    reasons = sorted(run["reasons"])
    return {
        "reasons": reasons,
        "reason_labels": [REASON_LABELS[r] for r in reasons],
        "line_start": min(covered) if covered else None,
        "line_end": max(covered) if covered else None,
        "lines": lines[:MAX_EVIDENCE_LINES],
        "lines_truncated": len(lines) > MAX_EVIDENCE_LINES,
        "start_t": round(samples[run["k_start"]].t, 3),
        "end_t": round(samples[run["k_end"] + 1].t, 3),
        "duration_s": round(run["duration_s"], 3),
        "capacity_ah": round(run["capacity_ah"], 4),
    }
