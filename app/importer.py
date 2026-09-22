"""飞行日志 CSV 解析：表头别名识别、分隔符嗅探、时间列归一化、导入前质量审计。"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Optional

from .analysis import Sample
from .audit import RawRecord, audit_records


class CsvFormatError(ValueError):
    pass


_TIME_ALIASES = {"time", "timestamp", "datetime", "date", "time_s", "t_s",
                 "elapsed", "时间", "时刻", "时间戳"}
_VOLT_ALIASES = {"voltage", "volt", "volts", "v", "voltage_v", "vol", "电压"}
_CURR_ALIASES = {"current", "curr", "i", "a", "current_a", "amp", "amps",
                 "amperage", "电流"}
_TEMP_ALIASES = {"temperature", "temp", "t", "temp_c", "temperature_c",
                 "celsius", "温度"}

# 单体电压列（cell1_voltage / cell_voltage_1 / v1 / 单体1电压 / 电芯1电压 /
# 单体电压1 / 第1节(串)电压 等），编号必须显式出现，不会误匹配总压列
_CELL_HEADER_RES = [
    re.compile(r"^cell[\s_-]*v(?:olt(?:age)?)?[\s_-]*(?P<n>\d{1,2})$"),
    re.compile(r"^cell[\s_-]*(?P<n>\d{1,2})[\s_-]*v(?:olt(?:age)?)?$"),
    re.compile(r"^v(?:olt(?:age)?)?[\s_-]*(?P<n>\d{1,2})$"),
    re.compile(r"^单体[\s_]*电压[\s_]*(?P<n>\d{1,2})$"),
    re.compile(r"^单体[\s_]*(?P<n>\d{1,2})[\s_]*电压$"),
    re.compile(r"^电芯[\s_]*电压[\s_]*(?P<n>\d{1,2})$"),
    re.compile(r"^电芯[\s_]*(?P<n>\d{1,2})[\s_]*电压$"),
    re.compile(r"^电压[\s_]*单体[\s_]*(?P<n>\d{1,2})$"),
    re.compile(r"^第[\s_]*(?P<n>\d{1,2})[\s_]*(?:节|串|芯)[\s_]*电压$"),
]

_UNIT_RE = re.compile(r"[\(（\[].*?[\)）\]]")


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _norm(header: str) -> str:
    h = header.strip().lower()
    h = _UNIT_RE.sub("", h)          # 去掉 (V) /（A）等单位
    h = h.replace(" ", "_").strip("_")
    return h


def _match_column(headers: list[str], aliases: set[str]) -> int | None:
    norm = [_norm(h) for h in headers]
    for idx, h in enumerate(norm):
        if h in aliases:
            return idx
    return None


def _find_cell_columns(headers: list[str]) -> tuple[dict[int, int], list[str]]:
    """识别单体电压列，返回 ({编号: 列索引}, 禁用原因)。编号从 1 起且须连续成组。"""
    found: dict[int, int] = {}
    raw: dict[int, str] = {}
    for idx, h in enumerate(headers):
        nh = _norm(h)
        for rx in _CELL_HEADER_RES:
            m = rx.match(nh)
            if m:
                n = int(m.group("n"))
                if n in found:
                    return {}, f"单体 {n} 电压列重复（{raw[n]!r} 与 {h!r}）"
                found[n] = idx
                raw[n] = h
                break
    if not found:
        return {}, ""
    nums = sorted(found)
    expected = list(range(1, len(nums) + 1))
    if nums != expected:
        return {}, (f"单体电压列编号不连续：期望 {expected}，实际 {nums}"
                    f"（需按编号 1..N 成组）")
    return {n: found[n] for n in nums}, ""


def _parse_time_value(s: str):
    """返回 (秒数值, 是否 epoch 时间戳)。"""
    s = s.strip()
    try:
        val = float(s)
        return val, val > 1e8
    except ValueError:
        pass
    iso = s.replace("Z", "+00:00").replace("/", "-")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise CsvFormatError(f"无法解析时间值: {s!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp(), True


def parse_flight_csv(text: str) -> tuple[list[Sample], dict]:
    """解析 CSV 文本，返回 (按时间排序的样本列表, 元信息)。

    单体电压列为可选：识别到且编号连续时按行成组解析；某行缺值或单体数量漂移，
    仅跳过该行的单体分析（记录原因），总压/电流/温度的主分析不受影响。
    """
    sample_text = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if len(rows) < 2:
        raise CsvFormatError("CSV 内容为空或缺少数据行")

    headers = rows[0]
    ci_t = _match_column(headers, _TIME_ALIASES)
    ci_v = _match_column(headers, _VOLT_ALIASES)
    ci_i = _match_column(headers, _CURR_ALIASES)
    ci_tp = _match_column(headers, _TEMP_ALIASES)
    missing = [name for name, idx in
               (("time", ci_t), ("voltage", ci_v), ("current", ci_i),
                ("temperature", ci_tp)) if idx is None]
    if missing:
        raise CsvFormatError(
            f"缺少必需列: {', '.join(missing)}；识别到的表头: {headers}")

    cell_cols, cell_disabled_reason = _find_cell_columns(headers)
    cell_indices = [cell_cols[n] for n in sorted(cell_cols)]
    known_indices = {ci_t, ci_v, ci_i, ci_tp, *cell_indices}
    cell_skip: dict[str, dict] = {}

    def note_cell_skip(reason: str, line_no: int) -> None:
        rec = cell_skip.setdefault(reason, {"count": 0, "rows": []})
        rec["count"] += 1
        if len(rec["rows"]) < 5:
            rec["rows"].append(line_no)

    raw_records: list[RawRecord] = []
    samples: list[Sample] = []
    skipped = 0
    skipped_lines: list[int] = []
    t0 = None
    epoch_based = False
    for line_no, row in enumerate(rows[1:], start=2):
        try:
            tv, is_epoch = _parse_time_value(row[ci_t])
            v = float(row[ci_v])
            i = float(row[ci_i])
            tp = float(row[ci_tp])
        except (ValueError, IndexError, CsvFormatError):
            skipped += 1
            skipped_lines.append(line_no)
            continue
        if t0 is None:
            t0 = tv
            epoch_based = is_epoch
        raw_records.append(RawRecord(line_no=line_no, t=tv, v=v, i=i, temp=tp))

        cells: Optional[tuple] = None
        if cell_indices and not cell_disabled_reason:
            vals: list[float] = []
            malformed = 0
            missing = 0
            for ci in cell_indices:
                raw_v = row[ci].strip() if ci < len(row) else ""
                if not raw_v:
                    missing += 1
                    continue
                try:
                    vals.append(float(raw_v))
                except ValueError:
                    malformed += 1
            # 已知列之外出现电压量级数值列，视为单体数量漂移
            extra = False
            for xci, cell in enumerate(row):
                if xci in known_indices:
                    continue
                cell = cell.strip()
                if not cell:
                    continue
                try:
                    xv = float(cell)
                except ValueError:
                    continue
                if 0.0 < xv < 10.0:
                    extra = True
            if extra:
                note_cell_skip("单体数量漂移（出现编号之外的额外单体值）", line_no)
            elif malformed:
                note_cell_skip("单体电压无法解析（非数值）", line_no)
            elif missing:
                note_cell_skip("单体电压缺值", line_no)
            else:
                cells = tuple(vals)
        samples.append(Sample(t=tv - t0, v=v, i=i, temp=tp, cells=cells,
                              line_no=line_no))

    if len(samples) < 10:
        raise CsvFormatError(f"有效数据行不足（{len(samples)} 行），至少需要 10 行")

    # 导入前质量审计：基于 CSV 原始行序（排序之前），保留原始行号与绝对时间。
    # 审计只产出证据，不修改样本、不阻断后续循环分析。
    quality = audit_records(raw_records, time_is_epoch=epoch_based,
                            skipped_rows=skipped,
                            skipped_line_numbers=skipped_lines)

    samples.sort(key=lambda s: s.t)
    start_iso = (datetime.fromtimestamp(t0, tz=timezone.utc).isoformat()
                 if epoch_based and t0 is not None else None)
    cell_rows = sum(1 for s in samples if s.cells is not None)
    meta = {
        "row_count": len(samples),
        "skipped_rows": skipped,
        "start_time": start_iso,
        "duration_s": round(samples[-1].t - samples[0].t, 3),
        "max_current_a": round(max(abs(s.i) for s in samples), 3),
        "min_voltage": round(min(s.v for s in samples), 3),
        "max_voltage": round(max(s.v for s in samples), 3),
        "max_temp_c": round(max(s.temp for s in samples), 3),
        "cells": {
            "present": bool(cell_indices),
            "enabled": bool(cell_indices) and not cell_disabled_reason,
            "cell_count": len(cell_indices),
            "columns": [headers[cell_cols[n]] for n in sorted(cell_cols)],
            "disabled_reason": cell_disabled_reason,
            "rows_with_cells": cell_rows,
            "rows_skipped": (len(samples) - cell_rows)
            if cell_indices and not cell_disabled_reason else 0,
            "skip_reasons": cell_skip,
        },
        "quality_audit": quality,
    }
    return samples, meta
