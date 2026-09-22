"""导入前数据质量审计（在时间排序之前、按 CSV 原始行序执行）。

检查项：
- 时间戳乱序（out_of_order）：后行时间戳早于前行
- 重复时间（duplicate_time）：两行时间戳完全相同
- 异常采样间隔（interval_gap / interval_short）：基于正间隔中位数的稳健判定
- 电压 / 电流 / 温度稳健离群点（outlier）：逐点局部邻域基线残差 + MAD
  修正 z 分数（避开多工况序列的全局中位数误报）

相邻（行号差 <= ``MERGE_GAP_LINES``）的同类问题会被合并为一个"质量区段"，
区段记录类型、严重度、起止行、时间范围、受影响字段与逐条证据。

审计只产出证据，绝不抛错阻断导入：任何质量问题都不影响可解析行的循环分析。
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import NamedTuple, Optional, Sequence

# ---- 可调阈值 ----
MAD_Z_THRESHOLD = 3.5        # 修正 z 分数离群阈值（经典推荐值 3.5）
MAD_K = 1.4826               # MAD -> 正态标准差的换算系数
MEAN_AD_K = 1.253398         # MAD=0 时的退化方案：平均绝对偏差换算系数
LOCAL_WINDOW = 8             # 局部上下文半窗：±8 个邻点（飞行数据多工况，全局中位数不适用）
# 各通道传感器噪声地板：飞行数据整体高度平滑（残差 MAD 常接近 0），
# 小于该尺度的偏离视为正常测量噪声而非数据质量问题。
NOISE_FLOOR = {"voltage": 0.08, "current": 1.5, "temperature": 1.0}
GAP_RATIO = 3.0              # 间隔 >= 该倍数典型间隔视为采样缺口
GAP_MIN_DELTA_S = 2.0        # 采样缺口的最小绝对间隔 (s)，避免规则采样下的小抖动误报
SHORT_RATIO = 1.0 / 3.0      # 间隔 <= 该比例典型间隔视为过密采样
SHORT_MAX_DELTA_S = 0.5      # 过密采样的最大绝对间隔 (s)
MERGE_GAP_LINES = 2          # 同类相邻问题行号差 <= 该值时合并为同一区段
MAX_DETAIL_POINTS = 20       # 每区段保留的逐条证据上限
MAX_SKIPPED_LINES = 20       # 无法解析行保留的行号证据上限

TYPE_OUTLIER = "outlier"
TYPE_OUT_OF_ORDER = "out_of_order"
TYPE_DUPLICATE = "duplicate_time"
TYPE_GAP = "interval_gap"
TYPE_SHORT = "interval_short"

TYPE_LABELS = {
    TYPE_OUTLIER: "稳健离群点",
    TYPE_OUT_OF_ORDER: "时间戳乱序",
    TYPE_DUPLICATE: "重复时间戳",
    TYPE_GAP: "采样间隔异常（缺口）",
    TYPE_SHORT: "采样间隔异常（过密）",
}

FIELD_TIME = "time"
FIELD_LABELS = {
    FIELD_TIME: "时间戳",
    "voltage": "电压",
    "current": "电流",
    "temperature": "温度",
}

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


class RawRecord(NamedTuple):
    """CSV 中一行可解析数据（未经时间归一化与排序）。"""
    line_no: int    # CSV 物理行号（表头为第 1 行，首条数据为第 2 行）
    t: float        # 绝对秒（epoch）或数值秒原值
    v: float        # 电压 V
    i: float        # 电流 A
    temp: float     # 温度 °C


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _t_label(t: float, epoch_based: bool) -> Optional[str]:
    """时间证据标签：epoch 时间戳给 ISO 字符串，数值秒给相对秒值，由调用方组装。"""
    return _iso(t) if epoch_based else None


def _robust_scale(values: Sequence[float]) -> tuple[float, float]:
    """返回 (中位数, 稳健标准差)。

    主尺度为 1.4826×MAD；MAD 为 0（半数以上取值相同，如静置段恒流）时退化为
    平均绝对偏差，仍可在恒定基线上检出单个尖峰；两者皆为 0 时返回 0 尺度
    （该通道为常量，无法也无需检出离群点）。
    """
    if not values:
        return 0.0, 0.0
    med = median(values)
    abs_dev = [abs(x - med) for x in values]
    mad = median(abs_dev)
    sigma = MAD_K * mad
    if sigma == 0.0:
        mean_ad = sum(abs_dev) / len(abs_dev)
        sigma = MEAN_AD_K * mean_ad
    return med, sigma


def _local_bases(values: Sequence[float], half: int) -> list[float]:
    """逐点局部基线（排除自身），边缘用等量的一侧窗口。

    飞行数据是静置/放电/充电多工况序列，全局中位数会把正常工况切换误判为离群。
    基线取"前向邻域中位数"与"后向邻域中位数"中离该点更近的一个：工况切换的
    首点贴合后向新工况、末点贴合前向旧工况，均不构成离群；孤立尖峰两侧均为
    旧工况基线，仍能被准确检出。
    """
    n = len(values)
    out: list[float] = []
    for k in range(n):
        back = values[max(0, k - half):k]
        fwd = values[k + 1:min(n, k + half + 1)]
        cand = []
        if back:
            cand.append(median(back))
        if fwd:
            cand.append(median(fwd))
        if not cand:
            out.append(values[k])
        else:
            out.append(min(cand, key=lambda c: abs(c - values[k])))
    return out


def _local_residual_scale(values: Sequence[float]
                          ) -> tuple[list[float], float, float]:
    """返回 (逐点局部基线, 残差中位数, 残差稳健标准差)。"""
    local = _local_bases(values, LOCAL_WINDOW)
    residuals = [x - c for x, c in zip(values, local)]
    rmed, sigma = _robust_scale(residuals)
    return local, rmed, sigma


def _outlier_detail(line_no: int, field_name: str, value: float,
                    local_center: float, sigma: float) -> dict:
    return {
        "line": line_no,
        "field": field_name,
        "field_label": FIELD_LABELS[field_name],
        "value": round(value, 3),
        "local_center": round(local_center, 3),
        "z_score": round((value - local_center) / sigma, 2),
    }


def _time_detail(line_no: int, prev_line: int, t: float, prev_t: float,
                 epoch_based: bool) -> dict:
    return {
        "line": line_no,
        "prev_line": prev_line,
        "time": _t_label(t, epoch_based),
        "prev_time": _t_label(prev_t, epoch_based),
        "value_s": round(t, 3),
        "prev_value_s": round(prev_t, 3),
        "delta_s": round(t - prev_t, 3),
    }


def audit_records(records: Sequence[RawRecord], *,
                  time_is_epoch: bool,
                  skipped_rows: int = 0,
                  skipped_line_numbers: Optional[Sequence[int]] = None) -> dict:
    """对原始行序的可解析记录执行质量审计，返回可直接 JSON 序列化的审计证据。"""
    n = len(records)
    t_base = records[0].t if records else 0.0
    issues: list[dict] = []

    # ------------------------------------------------------------ 时间戳审计
    seen_first: dict[float, int] = {}
    t_counts: dict[float, int] = {}
    positive_dts: list[float] = []

    for k, rec in enumerate(records):
        t_counts[rec.t] = t_counts.get(rec.t, 0) + 1
        if rec.t not in seen_first:
            seen_first[rec.t] = rec.line_no
        elif k > 0:
            prev = records[k - 1]
            issues.append({
                "type": TYPE_DUPLICATE, "severity": "warning",
                "line_lo": rec.line_no, "line_hi": rec.line_no,
                "lines": [rec.line_no], "times": [rec.t],
                "fields": [FIELD_TIME],
                "details": [_time_detail(rec.line_no, seen_first[rec.t],
                                         rec.t, rec.t, time_is_epoch)],
            })
        if k > 0:
            prev = records[k - 1]
            dt = rec.t - prev.t
            if dt < 0:
                issues.append({
                    "type": TYPE_OUT_OF_ORDER, "severity": "warning",
                    "line_lo": rec.line_no, "line_hi": rec.line_no,
                    "lines": [rec.line_no], "times": [prev.t, rec.t],
                    "fields": [FIELD_TIME],
                    "details": [_time_detail(rec.line_no, prev.line_no,
                                             rec.t, prev.t, time_is_epoch)],
                })
            else:
                positive_dts.append(dt)

    dt_median = median(positive_dts) if positive_dts else None
    dt_min = min(positive_dts) if positive_dts else None
    dt_max = max(positive_dts) if positive_dts else None
    abnormal_interval_count = 0
    if dt_median is not None and dt_median > 0:
        for k in range(1, n):
            a, b = records[k - 1], records[k]
            dt = b.t - a.t
            if dt <= 0:
                continue  # 0 由重复时间覆盖、负值由乱序覆盖，避免一事多罚
            typ = None
            if dt >= GAP_MIN_DELTA_S and dt >= GAP_RATIO * dt_median:
                typ = TYPE_GAP
            elif dt <= SHORT_MAX_DELTA_S and dt <= SHORT_RATIO * dt_median:
                typ = TYPE_SHORT
            if typ is None:
                continue
            abnormal_interval_count += 1
            issues.append({
                "type": typ,
                "severity": "warning" if typ == TYPE_GAP else "info",
                "line_lo": a.line_no, "line_hi": b.line_no,
                "lines": [a.line_no, b.line_no], "times": [a.t, b.t],
                "fields": [FIELD_TIME],
                "details": [{
                    "line_start": a.line_no, "line_end": b.line_no,
                    "delta_s": round(dt, 3),
                    "time_start": _t_label(a.t, time_is_epoch),
                    "time_end": _t_label(b.t, time_is_epoch),
                    "value_start_s": round(a.t, 3),
                    "value_end_s": round(b.t, 3),
                }],
            })

    duplicate_groups = sum(1 for c in t_counts.values() if c > 1)
    ordered = not any(x["type"] == TYPE_OUT_OF_ORDER for x in issues)

    # ------------------------------------------------------------ 稳健离群点
    # 逐点相对其局部上下文中位数的残差做 MAD 判定：工况切换/爬升斜坡属于
    # 局部正常形态不会误报，只有脱离邻居的孤立尖峰/深跌才算离群。
    channels = [("voltage", "v"), ("current", "i"), ("temperature", "temp")]
    scales: dict[str, dict] = {}
    outlier_by_line: dict[int, dict] = {}
    outlier_counts = {"voltage": 0, "current": 0, "temperature": 0}
    for field_name, attr in channels:
        values = [getattr(r, attr) for r in records]
        local_centers, _, raw_sigma = _local_residual_scale(values)
        sigma = max(raw_sigma, NOISE_FLOOR[field_name])
        g_med, _ = _robust_scale(values)
        scales[field_name] = {"median": round(g_med, 4),
                              "residual_sigma": round(raw_sigma, 4),
                              "effective_sigma": round(sigma, 4),
                              "noise_floor": NOISE_FLOOR[field_name]}
        if sigma <= 0:
            continue
        for rec, center in zip(records, local_centers):
            value = getattr(rec, attr)
            z = (value - center) / sigma
            if abs(z) < MAD_Z_THRESHOLD:
                continue
            outlier_counts[field_name] += 1
            detail = _outlier_detail(rec.line_no, field_name,
                                     value, center, sigma)
            if rec.line_no not in outlier_by_line:
                issue = {
                    "type": TYPE_OUTLIER, "severity": "warning",
                    "line_lo": rec.line_no, "line_hi": rec.line_no,
                    "lines": [rec.line_no], "times": [rec.t],
                    "fields": [field_name], "details": [detail],
                }
                outlier_by_line[rec.line_no] = issue
                issues.append(issue)
            else:
                issue = outlier_by_line[rec.line_no]
                issue["fields"].append(field_name)
                issue["details"].append(detail)

    # ---------------------------------------------------- 相邻问题合并区段
    segments: list[dict] = []
    by_type: dict[str, list[dict]] = {}
    for iss in issues:
        by_type.setdefault(iss["type"], []).append(iss)

    for typ, lst in by_type.items():
        lst.sort(key=lambda x: (x["line_lo"], x["line_hi"]))
        cur: Optional[dict] = None
        for iss in lst:
            if cur is not None and iss["line_lo"] <= cur["line_hi"] + MERGE_GAP_LINES:
                cur["line_hi"] = max(cur["line_hi"], iss["line_hi"])
                cur["lines"].extend(iss["lines"])
                cur["times"].extend(iss["times"])
                for f in iss["fields"]:
                    if f not in cur["fields"]:
                        cur["fields"].append(f)
                cur["details"].extend(iss["details"])
            else:
                if cur is not None:
                    segments.append(cur)
                cur = {
                    "type": typ, "severity": iss["severity"],
                    "line_lo": iss["line_lo"], "line_hi": iss["line_hi"],
                    "lines": list(iss["lines"]), "times": list(iss["times"]),
                    "fields": list(iss["fields"]),
                    "details": list(iss["details"]),
                }
        if cur is not None:
            segments.append(cur)

    segments.sort(key=lambda s: (s["line_lo"],
                                 -SEVERITY_ORDER[s["severity"]], s["type"]))

    counts_by_type = {t: 0 for t in TYPE_LABELS}
    counts_by_severity = {"info": 0, "warning": 0, "critical": 0}
    affected: set[str] = set()
    for iss in issues:
        counts_by_type[iss["type"]] += 1
        counts_by_severity[iss["severity"]] += 1
        affected.update(iss["fields"])

    out_segments: list[dict] = []
    for seq, seg in enumerate(segments, start=1):
        lines = sorted(set(seg["lines"]))
        t_lo, t_hi = min(seg["times"]), max(seg["times"])
        details = seg["details"]
        truncated = len(details) > MAX_DETAIL_POINTS
        fields = sorted(set(seg["fields"]))
        out = {
            "seq": seq,
            "type": seg["type"],
            "type_label": TYPE_LABELS[seg["type"]],
            "severity": seg["severity"],
            "line_start": seg["line_lo"],
            "line_end": seg["line_hi"],
            "sample_count": len(lines),
            "time_start": _iso(t_lo) if time_is_epoch else None,
            "time_end": _iso(t_hi) if time_is_epoch else None,
            "rel_start_s": round(t_lo - t_base, 3),
            "rel_end_s": round(t_hi - t_base, 3),
            "fields": fields,
            "field_labels": [FIELD_LABELS[f] for f in fields],
            "issue_count": len(details),
            "message": _segment_message(seg["type"], details, dt_median),
            "evidence": _segment_evidence(seg["type"], details, scales,
                                          dt_median),
            "details_truncated": truncated,
            "details": details[:MAX_DETAIL_POINTS],
        }
        out_segments.append(out)

    skipped_lines = list(skipped_line_numbers or [])[:MAX_SKIPPED_LINES]
    return {
        "version": 1,
        "time_is_epoch": time_is_epoch,
        "parsed_rows": n,
        "skipped_rows": skipped_rows,
        "skipped_line_numbers": skipped_lines,
        "skipped_lines_truncated": skipped_rows > len(skipped_lines),
        "time_ordered": ordered,
        "unique_timestamps": len(t_counts),
        "duplicate_timestamp_groups": duplicate_groups,
        "interval": {
            "sample_interval_s": round(dt_median, 3) if dt_median is not None else None,
            "min_s": round(dt_min, 3) if dt_min is not None else None,
            "max_s": round(dt_max, 3) if dt_max is not None else None,
            "abnormal_count": abnormal_interval_count,
        },
        "outliers": {
            "threshold_z": MAD_Z_THRESHOLD,
            "scales": scales,
            "counts": outlier_counts,
        },
        "counts_by_type": counts_by_type,
        "counts_by_severity": counts_by_severity,
        "issue_count": len(issues),
        "segment_count": len(out_segments),
        "affected_fields": sorted(affected),
        "segments": out_segments,
    }


def _segment_message(typ: str, details: list[dict],
                     dt_median: Optional[float]) -> str:
    med_txt = f"{dt_median:.2f}s" if dt_median is not None else "—"
    if typ == TYPE_OUTLIER:
        d0 = details[0]
        fields = sorted({d["field_label"] for d in details})
        head = "、".join(fields)
        more = f"等 {len(details)} 个离群点" if len(details) > 1 else "离群"
        return (f"{head}稳健{more}（局部上下文修正 z 分数 ≥ {MAD_Z_THRESHOLD}）："
                f"首个为第 {d0['line']} 行 {d0['field_label']}"
                f"={d0['value']}（邻近 {d0['local_center']}，"
                f"z={d0['z_score']}）。")
    if typ == TYPE_OUT_OF_ORDER:
        d0 = details[0]
        return (f"{len(details)} 处时间戳乱序：第 {d0['line']} 行时间早于"
                f"第 {d0['prev_line']} 行（回退 {abs(d0['delta_s']):.2f}s），"
                f"循环分析仍按时间排序后进行。")
    if typ == TYPE_DUPLICATE:
        d0 = details[0]
        when = d0["time"] or f"t={d0['value_s']:.2f}s"
        return (f"{len(details)} 个重复时间戳：第 {d0['line']} 行与"
                f"第 {d0['prev_line']} 行时间相同（{when}）。")
    if typ == TYPE_GAP:
        mx = max(d["delta_s"] for d in details)
        return (f"{len(details)} 处采样缺口：间隔 ≥ {GAP_RATIO:.0f}×典型间隔"
                f"（{med_txt}）且 ≥ {GAP_MIN_DELTA_S:.0f}s，最大 {mx:.2f}s，"
                f"区间内积分/时长统计可能偏低。")
    # TYPE_SHORT
    mn = min(d["delta_s"] for d in details)
    return (f"{len(details)} 处过密采样：间隔 ≤ 典型间隔（{med_txt}）的 1/3"
            f"且 ≤ {SHORT_MAX_DELTA_S:.1f}s，最小 {mn:.2f}s。")


def _segment_evidence(typ: str, details: list[dict], scales: dict,
                      dt_median: Optional[float]) -> dict:
    if typ == TYPE_OUTLIER:
        per_field: dict[str, dict] = {}
        for d in details:
            st = per_field.setdefault(d["field"], {
                "field_label": d["field_label"], "count": 0,
                "max_abs_z": 0.0, "extreme_line": None})
            st["count"] += 1
            if abs(d["z_score"]) > abs(st["max_abs_z"]):
                st["max_abs_z"] = d["z_score"]
                st["extreme_line"] = d["line"]
        return {
            "method": "local-median residual + MAD (modified z-score)",
            "z_threshold": MAD_Z_THRESHOLD,
            "local_window": LOCAL_WINDOW,
            "scales": {f: scales.get(f) for f in per_field},
            "fields": per_field,
        }
    if typ in (TYPE_GAP, TYPE_SHORT):
        return {
            "sample_interval_s": round(dt_median, 3) if dt_median is not None else None,
            "max_delta_s": round(max(d["delta_s"] for d in details), 3),
            "min_delta_s": round(min(d["delta_s"] for d in details), 3),
            "gap_ratio": GAP_RATIO, "gap_min_delta_s": GAP_MIN_DELTA_S,
            "short_ratio": round(SHORT_RATIO, 3),
            "short_max_delta_s": SHORT_MAX_DELTA_S,
        }
    return {"count": len(details)}
