"""放电容量的可信覆盖评估。

以 CSV **原始行号** 把导入前质量审计（``app/audit.py``）与时间排序后的样本
关联，对每个放电循环的每个梯形积分段（相邻两样本之间）重新做库仑积分：

- **可疑段**：
  - 电流稳健离群点（审计 ``outlier`` 区段且 ``fields`` 含 ``current``）所在行
    作为端点的相邻积分段；
  - 采样缺口（审计 ``interval_gap`` 区段）两侧样本之间的跨缺口积分段。
- **可信段**：其余所有积分段。

两类段分别统计容量 (Ah)、累计时长 (s)，并给出可信时长占比。注意容量口径：
梯形积分跨过缺口/坏点时只是"按直线补"的估计，可疑段容量**不是**真实缺失
容量，可信段容量也**不能**被称为真实总容量；循环的总容量、SOH 与健康等级
仍沿用原结果，本评估只给出覆盖度证据。

保守原则：审计区段的逐条证据 ``details`` 最多保留 20 条（截断时
``details_truncated=True``），此时无法知道区段内全部问题行，一律按
``line_start..line_end`` 覆盖的物理行范围保守处理，宁可疑不可漏。

旧库日志没有审计记录（或样本缺少原始行号）时返回 ``unassessed``：
无法评估，**不得**当作全部可信。
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from .analysis import Sample

# 可信时长占比低于该值的放电循环进入"低覆盖"汇总
LOW_COVERAGE_TIME_RATIO = 0.90

# 段明细在单循环内的保留上限（超出则截断，统计不受影响）
MAX_SEGMENTS = 100

STATUS_OK = "ok"
STATUS_LOW = "low_coverage"
STATUS_UNASSESSED = "unassessed"

DEFAULT_NO_AUDIT_REASON = "该日志无导入前质量审计记录，无法评估容量可信覆盖"

_AUDIT_OUTLIER = "outlier"
_AUDIT_GAP = "interval_gap"
_FIELD_CURRENT = "current"


class _Rule(NamedTuple):
    """单条审计区段推导出来的可疑段判定规则。

    kind:
      - 'outlier_exact'：端点行号命中离群行集合（details 未截断）
      - 'gap_span'：积分段落在缺口两行按时间序展开的跨度之内
      - 'line_range'：端点行号落入 [lo, hi] 物理行范围
        （outlier/gap 的 details 截断时使用的保守规则）
    """
    kind: str
    lo: int
    hi: int
    reason_code: str
    message: str


def unknown_coverage(reason: str = DEFAULT_NO_AUDIT_REASON) -> dict:
    """无法评估时的占位结果（旧库无审计 / 样本无法关联原始行号）。"""
    return {
        "status": STATUS_UNASSESSED,
        "assessable": False,
        "reason": reason,
        "total_capacity_ah": None,
        "trusted_capacity_ah": None,
        "suspicious_capacity_ah": None,
        "total_duration_s": None,
        "trusted_duration_s": None,
        "suspicious_duration_s": None,
        "trusted_time_ratio": None,
        "trusted_intervals": 0,
        "suspicious_intervals": 0,
        "nonpositive_intervals": 0,
        "trusted_segments": [],
        "suspicious_segments": [],
        "trusted_segments_truncated": False,
        "suspicious_segments_truncated": False,
        "low_coverage_threshold": LOW_COVERAGE_TIME_RATIO,
    }


def build_coverage_plan(audit: Optional[dict],
                        samples: list[Sample]) -> Optional[list[_Rule]]:
    """把审计区段翻译成积分段判定规则，并建立原始行号 -> 时间序索引映射。

    审计完全缺失或样本全部缺少原始行号时返回 None（调用方据此标记无法评估）。
    审计存在但零区段（干净日志）返回空规则列表：可评估，全部积分段均可信。
    """
    if not audit:
        return None
    line_to_idx: dict[int, int] = {}
    for k, s in enumerate(samples):
        if s.line_no is not None:
            line_to_idx[s.line_no] = k
    if not line_to_idx:
        return None

    rules: list[_Rule] = []
    for seg in audit.get("segments", []):
        typ = seg.get("type")
        seq = seg.get("seq")
        truncated = bool(seg.get("details_truncated"))
        lo, hi = seg.get("line_start"), seg.get("line_end")
        if typ == _AUDIT_OUTLIER:
            if _FIELD_CURRENT not in (seg.get("fields") or []):
                continue  # 只有电流离群影响库仑积分；电压/温度离群不疑容量
            if truncated:
                rules.append(_Rule(
                    "line_range", lo, hi, "current_outlier",
                    f"电流离群点相邻区间：审计区段 #{seq} 逐条证据截断，"
                    f"保守将第 {lo}~{hi} 行（CSV 原始行号）相邻积分段全部计为可疑"))
            else:
                lines = sorted({d["line"] for d in seg.get("details", [])
                                if d.get("field") == _FIELD_CURRENT})
                if not lines:
                    continue
                for ln in lines:
                    rules.append(_Rule(
                        "outlier_exact", ln, ln, "current_outlier",
                        f"电流离群点相邻区间：第 {ln} 行电流为稳健离群点"
                        f"（审计区段 #{seq}，CSV 原始行号）"))
        elif typ == _AUDIT_GAP:
            if truncated:
                rules.append(_Rule(
                    "line_range", lo, hi, "sampling_gap",
                    f"采样缺口区间：审计区段 #{seq} 逐条证据截断，"
                    f"保守将第 {lo}~{hi} 行（CSV 原始行号）涉及的积分段全部计为可疑"))
            else:
                for d in seg.get("details", []):
                    l1, l2 = d.get("line_start"), d.get("line_end")
                    if l1 is None or l2 is None:
                        continue
                    i1, i2 = line_to_idx.get(l1), line_to_idx.get(l2)
                    delta = d.get("delta_s")
                    msg = (f"采样缺口区间：第 {l1} 行 → 第 {l2} 行之间存在 "
                           f"{delta}s 采样缺口（审计区段 #{seq}，CSV 原始行号），"
                           f"跨缺口梯形积分不可信")
                    if i1 is None or i2 is None:
                        # 极端情况下行号无法关联：退化为行范围保守判定
                        rules.append(_Rule(
                            "line_range", min(l1, l2), max(l1, l2),
                            "sampling_gap", msg))
                    else:
                        rules.append(_Rule(
                            "gap_span", min(i1, i2), max(i1, i2),
                            "sampling_gap", msg))
    return rules


def _rule_hits(rule: _Rule, k: int, s0: Sample, s1: Sample) -> bool:
    if rule.kind == "outlier_exact":
        return s0.line_no == rule.lo or s1.line_no == rule.lo
    if rule.kind == "gap_span":
        # 缺口两行在时间序上的跨度内的积分段一律可疑（乱序导致中间插入
        # 其他样本时同样保守计入）
        return k >= rule.lo and k + 1 <= rule.hi
    # line_range：端点物理行号落在保守范围内即可疑
    for ln in (s0.line_no, s1.line_no):
        if ln is not None and rule.lo <= ln <= rule.hi:
            return True
    return False


def _round_seg(seg: dict) -> dict:
    seg["start_t"] = round(seg["start_t"], 3)
    seg["end_t"] = round(seg["end_t"], 3)
    seg["duration_s"] = round(seg["duration_s"], 3)
    seg["capacity_ah"] = round(seg["capacity_ah"], 6)
    return seg


def assess_cycle_coverage(samples: list[Sample], a: int, b: int,
                          plan: Optional[list[_Rule]], *,
                          no_audit_reason: str = DEFAULT_NO_AUDIT_REASON) -> dict:
    """对时间序样本的循环 [a, b] 逐梯形段重算，返回可信/可疑覆盖统计。

    容量口径与 ``analysis._integrate`` 完全一致（|I| 梯形积分，dt<=0 的
    重复/乱序相邻段跳过），只是把每个积分段按审计证据分别累计。
    """
    if plan is None:
        return unknown_coverage(no_audit_reason)

    cap_t = cap_s = 0.0
    dur_t = dur_s = 0.0
    n_t = n_s = n_nonpos = 0
    trusted_runs: list[dict] = []
    suspect_runs: list[dict] = []

    cur_suspect: Optional[dict] = None
    cur_trusted: Optional[dict] = None

    def close(run: Optional[dict], bucket: list[dict]) -> None:
        if run is not None:
            bucket.append(run)

    for k in range(a, b):
        s0, s1 = samples[k], samples[k + 1]
        dt = s1.t - s0.t
        if dt <= 0:
            n_nonpos += 1
            continue  # 与原积分一致：重复时间戳/乱序回退段不产生容量
        i0, i1 = abs(s0.i), abs(s1.i)
        cap = 0.5 * (i0 + i1) * dt / 3600.0

        hits = [r for r in plan if _rule_hits(r, k, s0, s1)]
        if hits:
            cap_s += cap
            dur_s += dt
            n_s += 1
            if cur_suspect is None:
                close(cur_trusted, trusted_runs)
                cur_trusted = None
                cur_suspect = {"start_idx": k, "end_idx": k + 1,
                               "start_t": s0.t, "end_t": s1.t,
                               "duration_s": dt, "capacity_ah": cap,
                               "messages": [], "reason_codes": set()}
            else:
                cur_suspect["end_idx"] = k + 1
                cur_suspect["end_t"] = s1.t
                cur_suspect["duration_s"] += dt
                cur_suspect["capacity_ah"] += cap
            for r in hits:
                if r.message not in cur_suspect["messages"]:
                    cur_suspect["messages"].append(r.message)
                cur_suspect["reason_codes"].add(r.reason_code)
        else:
            cap_t += cap
            dur_t += dt
            n_t += 1
            if cur_trusted is None:
                close(cur_suspect, suspect_runs)
                cur_suspect = None
                cur_trusted = {"start_idx": k, "end_idx": k + 1,
                               "start_t": s0.t, "end_t": s1.t,
                               "duration_s": dt, "capacity_ah": cap}
            else:
                cur_trusted["end_idx"] = k + 1
                cur_trusted["end_t"] = s1.t
                cur_trusted["duration_s"] += dt
                cur_trusted["capacity_ah"] += cap
    close(cur_suspect, suspect_runs)
    close(cur_trusted, trusted_runs)

    def finalize(run: dict, suspicious: bool) -> dict:
        l0 = samples[run["start_idx"]].line_no
        l1 = samples[run["end_idx"]].line_no
        out = {
            "line_start": l0, "line_end": l1,
            "start_t": run["start_t"], "end_t": run["end_t"],
            "duration_s": run["duration_s"], "capacity_ah": run["capacity_ah"],
        }
        if suspicious:
            out["reasons"] = list(run["messages"])
            out["reason_codes"] = sorted(run["reason_codes"])
        return _round_seg(out)

    trusted_segs = [finalize(r, False) for r in trusted_runs]
    suspect_segs = [finalize(r, True) for r in suspect_runs]
    suspect_segs.sort(key=lambda g: -g["duration_s"])

    t_truncated = len(trusted_segs) > MAX_SEGMENTS
    s_truncated = len(suspect_segs) > MAX_SEGMENTS
    trusted_segs = trusted_segs[:MAX_SEGMENTS]
    suspect_segs = suspect_segs[:MAX_SEGMENTS]

    total_dur = dur_t + dur_s
    ratio = dur_t / total_dur if total_dur > 0 else 0.0
    status = STATUS_OK if ratio >= LOW_COVERAGE_TIME_RATIO else STATUS_LOW
    return {
        "status": status,
        "assessable": True,
        "reason": None,
        "total_capacity_ah": round(cap_t + cap_s, 6),
        "trusted_capacity_ah": round(cap_t, 6),
        "suspicious_capacity_ah": round(cap_s, 6),
        "total_duration_s": round(total_dur, 3),
        "trusted_duration_s": round(dur_t, 3),
        "suspicious_duration_s": round(dur_s, 3),
        "trusted_time_ratio": round(ratio, 4),
        "trusted_intervals": n_t,
        "suspicious_intervals": n_s,
        "nonpositive_intervals": n_nonpos,
        "trusted_segments": trusted_segs,
        "suspicious_segments": suspect_segs,
        "trusted_segments_truncated": t_truncated,
        "suspicious_segments_truncated": s_truncated,
        "low_coverage_threshold": LOW_COVERAGE_TIME_RATIO,
    }
