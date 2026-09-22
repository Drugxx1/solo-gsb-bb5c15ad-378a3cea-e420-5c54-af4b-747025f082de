"""电池循环分析核心算法。

- 三态状态机（REST / DISCHARGE / CHARGE）识别充放电循环边界，含噪声段合并
- 梯形库仑积分估算每循环容量与能量
- 利用负载切换时刻的电压跳变与静置段电压恢复估算直流内阻
- 依据额定容量、安全电压范围与温度阈值生成可解释的判定标记
- 可选单体电压：按时间连续性计算每循环最大压差与持续超差区间（>0.10V 达 5s）
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import NamedTuple, Optional

from .coverage import LOW_COVERAGE_RATIO, STATUS_OK

# ---- 可调阈值 ----
REST_CURRENT_RATIO = 0.03    # 静置电流阈值 = 峰值电流的 3%
REST_CURRENT_FLOOR_A = 0.05  # 静置电流阈值下限 (A)
MIN_CYCLE_S = 20.0           # 短于该时长的充放电段视为噪声
MIN_REST_S = 10.0            # 短于该时长的静置不切断循环
CAPACITY_DROP_RATIO = 0.10   # 相邻放电循环容量突降告警阈值
CAPACITY_DROP_CRIT = 0.25    # 容量突降严重阈值
TEMP_WARN_C = 45.0           # 温度告警 (°C)
TEMP_CRIT_C = 60.0           # 温度严重告警 (°C)
TEMP_RISE_WARN_C = 15.0      # 单循环温升告警 (°C)
VOLT_MARGIN_V = 0.02         # 电压越限判定余量 (V)
OVER_CAPACITY_RATIO = 1.10   # 放电容量超过额定该比例时提示额定容量可能偏低

# ---- 单体失衡判定阈值 ----
CELL_DIFF_WARN_V = 0.10      # 单体最大压差超过该值视为超差 (V)
CELL_SUSTAIN_MIN_S = 5.0     # 压差连续超差达该时长才生成失衡标记 (s)
CELL_GAP_BREAK_S = 2.0       # 相邻单体采样时间差超过该值则中断连续区间 (s)
CELL_EVIDENCE_POINTS = 5     # 每段告警区间保留的证据样本点数


class Sample(NamedTuple):
    t: float      # 相对日志起点的秒
    v: float      # 电压 V
    i: float      # 电流 A，正=放电，负=充电
    temp: float   # 温度 °C
    cells: Optional[tuple] = None  # 各单体电压 V；该行缺值/单体数漂移时为 None
    line_no: Optional[int] = None  # CSV 物理行号（质量审计证据）


@dataclass
class Cycle:
    seq: int
    kind: str               # 'discharge' | 'charge'
    start_t: float
    end_t: float
    duration_s: float
    capacity_ah: float
    energy_wh: float
    avg_current_a: float
    min_voltage: float
    max_voltage: float
    max_temp_c: float
    temp_rise_c: float
    ir_mohm: Optional[float]
    closed: bool
    cell_count: int = 0          # 该循环内可用单体采样的单体数（0=无单体数据）
    cell_max_diff_v: Optional[float] = None  # 循环内单体最大压差 V
    cell_sustain_s: float = 0.0  # 连续超差（满足时长）区间累计持续 s
    cell_intervals: list = field(default_factory=list)  # 持续超差区间与证据
    flags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("start_t", "end_t", "duration_s", "capacity_ah", "energy_wh",
                  "avg_current_a", "min_voltage", "max_voltage", "max_temp_c",
                  "temp_rise_c", "cell_sustain_s"):
            d[k] = round(d[k], 4)
        if d["ir_mohm"] is not None:
            d["ir_mohm"] = round(d["ir_mohm"], 2)
        if d["cell_max_diff_v"] is not None:
            d["cell_max_diff_v"] = round(d["cell_max_diff_v"], 4)
        for iv in d["cell_intervals"]:
            iv["start_t"] = round(iv["start_t"], 3)
            iv["end_t"] = round(iv["end_t"], 3)
            iv["duration_s"] = round(iv["duration_s"], 3)
            iv["max_diff_v"] = round(iv["max_diff_v"], 4)
            iv["peak_t"] = round(iv["peak_t"], 3)
            for ev in iv["evidence"]:
                ev["t"] = round(ev["t"], 3)
                ev["diff_v"] = round(ev["diff_v"], 4)
        return d


# ---------------------------------------------------------------- 状态机

def _classify(samples: list[Sample], thr: float) -> list[str]:
    states = []
    for s in samples:
        if s.i > thr:
            states.append("D")
        elif s.i < -thr:
            states.append("C")
        else:
            states.append("R")
    return states


def _segments(states: list[str]) -> list[list]:
    segs = []
    start = 0
    for k in range(1, len(states) + 1):
        if k == len(states) or states[k] != states[start]:
            segs.append([states[start], start, k - 1])
            start = k
    return segs


def _merge_segments(samples: list[Sample], segs: list[list],
                    min_cycle_s: float, min_rest_s: float) -> list[list]:
    # 1) 过短的充放电段视为噪声，归为静置
    out = []
    for kind, a, b in segs:
        dur = samples[b].t - samples[a].t
        if kind != "R" and dur < min_cycle_s:
            kind = "R"
        out.append([kind, a, b])
    # 合并相邻同状态段
    merged: list[list] = []
    for seg in out:
        if merged and merged[-1][0] == seg[0]:
            merged[-1][2] = seg[2]
        else:
            merged.append(seg)
    # 2) 两侧状态相同的过短静置段并入（飞行中瞬时掉电流不应切断循环）
    changed = True
    while changed:
        changed = False
        for k in range(1, len(merged) - 1):
            kind, a, b = merged[k]
            if kind == "R":
                dur = samples[b].t - samples[a].t
                if dur < min_rest_s and merged[k - 1][0] == merged[k + 1][0] != "R":
                    merged[k - 1][2] = merged[k + 1][2]
                    del merged[k:k + 2]
                    changed = True
                    break
    return merged


def _integrate(samples: list[Sample], a: int, b: int) -> tuple[float, float]:
    """梯形库仑/能量积分，返回 (Ah, Wh)。"""
    cap = 0.0
    energy = 0.0
    for k in range(a, b):
        dt = samples[k + 1].t - samples[k].t
        if dt <= 0:
            continue
        i0, i1 = abs(samples[k].i), abs(samples[k + 1].i)
        cap += 0.5 * (i0 + i1) * dt
        energy += 0.5 * (i0 * samples[k].v + i1 * samples[k + 1].v) * dt
    return cap / 3600.0, energy / 3600.0


def _estimate_ir(samples: list[Sample], a: int, b: int,
                 kind: str, thr: float) -> Optional[float]:
    """由负载加载/卸载瞬间的电压跳变估算直流内阻 (Ω)。

    放电：IR = (V静置 - V带载) / |I|；充电取相反符号。
    起点用段前静置电压与加载后电压，终点用卸载前电压与静置恢复电压。
    """
    ests = []
    pre = [s.v for s in samples[max(0, a - 8):a] if abs(s.i) <= thr]
    if pre:
        j = min(a + 3, b)
        load_i = abs(samples[j].i)
        if load_i > 3 * thr:
            dv = (sum(pre) / len(pre)) - samples[j].v
            if kind == "C":
                dv = -dv
            ir = dv / load_i
            if 0 < ir < 1.0:
                ests.append(ir)
    post = [s.v for s in samples[b + 1:min(len(samples), b + 9)] if abs(s.i) <= thr]
    if post:
        load_i = abs(samples[b].i)
        if load_i > 3 * thr:
            dv = (sum(post) / len(post)) - samples[b].v
            if kind == "C":
                dv = -dv
            ir = dv / load_i
            if 0 < ir < 1.0:
                ests.append(ir)
    return sum(ests) / len(ests) if ests else None


def _build_cycle(samples: list[Sample], a: int, b: int,
                 kind: str, thr: float) -> Cycle:
    seg = samples[a:b + 1]
    cap_ah, energy_wh = _integrate(samples, a, b)
    duration = samples[b].t - samples[a].t
    ir = _estimate_ir(samples, a, b, kind, thr)
    max_temp = max(s.temp for s in seg)
    return Cycle(
        seq=0,
        kind="discharge" if kind == "D" else "charge",
        start_t=samples[a].t,
        end_t=samples[b].t,
        duration_s=duration,
        capacity_ah=cap_ah,
        energy_wh=energy_wh,
        avg_current_a=(cap_ah * 3600.0 / duration) if duration > 0 else 0.0,
        min_voltage=min(s.v for s in seg),
        max_voltage=max(s.v for s in seg),
        max_temp_c=max_temp,
        temp_rise_c=max_temp - seg[0].temp,
        ir_mohm=(ir * 1000.0) if ir is not None else None,
        closed=True,
    )


def _cell_stats(samples: list[Sample], a: int, b: int) -> dict:
    """按时间连续性统计循环 [a, b] 内单体压差。

    - 仅使用带有效单体数据的样本（缺值/单体数漂移的样本跳过单体分析）；
    - 每点压差 = max(cell) - min(cell)；
    - 采样间隔按时间戳累计；相邻有效单体样本间隔 > CELL_GAP_BREAK_S 中断连续区间；
    - 只有压差连续超过 CELL_DIFF_WARN_V 达 CELL_SUSTAIN_MIN_S 才计入告警区间。
    """
    pts = [(samples[k].t, samples[k].cells)
           for k in range(a, b + 1) if samples[k].cells]
    result = {"cell_count": 0, "max_diff": None, "sustain_s": 0.0,
              "intervals": []}
    if len(pts) < 2:
        return result

    cell_n = len(pts[0][1])
    diffs: list[tuple[float, float, tuple]] = []  # (t, diff, cells)
    max_diff = 0.0
    for t, cells in pts:
        if len(cells) != cell_n:
            continue  # 单体数量漂移的行：跳过该行单体分析
        diff = max(cells) - min(cells)
        diffs.append((t, diff, cells))
        if diff > max_diff:
            max_diff = diff
    result["cell_count"] = cell_n
    result["max_diff"] = max_diff

    intervals: list[dict] = []
    run: list[tuple] = []          # 当前连续超差样本
    prev_t: Optional[float] = None

    def close_run() -> None:
        if not run:
            return
        r0, rn = run[0], run[-1]
        dur = rn[0] - r0[0]
        if dur >= CELL_SUSTAIN_MIN_S:
            peak = max(run, key=lambda p: p[1])
            peak_t, peak_diff, peak_cells = peak
            weak = peak_cells.index(min(peak_cells)) + 1
            strong = peak_cells.index(max(peak_cells)) + 1
            # 证据片段：首、尾、峰值及中间点，最多 CELL_EVIDENCE_POINTS 个
            idxs = sorted(set([
                0, len(run) - 1,
                *[int(k * (len(run) - 1) / (CELL_EVIDENCE_POINTS - 1))
                  for k in range(CELL_EVIDENCE_POINTS)],
            ]))
            evidence = [{"t": round(run[k][0], 3),
                         "diff_v": round(run[k][1], 4),
                         "min_cell": run[k][2].index(min(run[k][2])) + 1,
                         "max_cell": run[k][2].index(max(run[k][2])) + 1}
                        for k in idxs]
            intervals.append({
                "start_t": r0[0], "end_t": rn[0], "duration_s": dur,
                "max_diff_v": peak_diff, "peak_t": peak_t,
                "weak_cell": weak, "strong_cell": strong,
                "cell_count": cell_n, "evidence": evidence,
            })

    for t, diff, cells in diffs:
        if diff > CELL_DIFF_WARN_V:
            if run and (t - prev_t) > CELL_GAP_BREAK_S:
                close_run()
                run = []
            run.append((t, diff, cells))
        else:
            close_run()
            run = []
        prev_t = t
    close_run()

    intervals.sort(key=lambda iv: -iv["max_diff_v"])
    result["sustain_s"] = sum(iv["duration_s"] for iv in intervals)
    result["intervals"] = intervals
    return result


def evaluate_cell_balance(cyc: Cycle) -> list[dict]:
    """对单个循环生成单体失衡标记（无单体数据或不满足持续时长则不告警）。"""
    if not cyc.cell_intervals:
        return []
    worst = max(cyc.cell_intervals, key=lambda iv: iv["max_diff_v"])
    where = f"日志内第 {cyc.seq} 段（{'放电' if cyc.kind == 'discharge' else '充电'}）"
    return [{
        "code": "CELL_IMBALANCE", "severity": "warning",
        "message": f"{where}单体最大压差 {worst['max_diff_v']:.3f}V（"
                   f"第 {worst['weak_cell']} 节偏低 / 第 {worst['strong_cell']} 节偏高），"
                   f"连续超过 {CELL_DIFF_WARN_V:.2f}V 达 {worst['duration_s']:.1f}s，"
                   f"存在单体失衡。",
        "evidence": (f"max_diff={worst['max_diff_v']:.3f}V"
                     f" > {CELL_DIFF_WARN_V:.2f}V, duration={worst['duration_s']:.1f}s"
                     f" >= {CELL_SUSTAIN_MIN_S:.0f}s, t={worst['start_t']:.0f}"
                     f"~{worst['end_t']:.0f}s, cell#{worst['weak_cell']} lowest"),
    }]


def detect_cycles(samples: list[Sample],
                  min_cycle_s: float = MIN_CYCLE_S,
                  min_rest_s: float = MIN_REST_S,
                  rest_current_a: Optional[float] = None) -> list[Cycle]:
    """状态机主入口：样本序列 -> 循环列表。"""
    if len(samples) < 2:
        return []
    peak = max(abs(s.i) for s in samples)
    thr = rest_current_a if rest_current_a is not None else max(
        REST_CURRENT_FLOOR_A, REST_CURRENT_RATIO * peak)
    segs = _merge_segments(samples, _segments(_classify(samples, thr)),
                           min_cycle_s, min_rest_s)
    cycles: list[Cycle] = []
    for si, (kind, a, b) in enumerate(segs):
        if kind == "R":
            continue
        cyc = _build_cycle(samples, a, b, kind, thr)
        cyc.seq = len(cycles) + 1
        cyc.closed = si < len(segs) - 1   # 日志结束时仍处于充/放电 -> 未闭合
        # 内部使用：该循环在时间排序样本序列中的下标区间，供容量可信覆盖评估
        # 关联审计证据（普通实例属性，不是 dataclass 字段，不会被 asdict 序列化）
        cyc.span = (a, b)
        cell = _cell_stats(samples, a, b)  # 单体分析独立于总压状态机，失败不影响主结果
        cyc.cell_count = cell["cell_count"]
        cyc.cell_max_diff_v = cell["max_diff"]
        cyc.cell_sustain_s = cell["sustain_s"]
        cyc.cell_intervals = cell["intervals"]
        cycles.append(cyc)
    return cycles


# ---------------------------------------------------------------- 判定规则

def evaluate_cycle(cyc: Cycle, rated_ah: float,
                   safe_vmin: float, safe_vmax: float) -> list[dict]:
    """对单个循环套用安全/健康规则，返回可解释的标记列表。"""
    flags: list[dict] = []
    where = f"日志内第 {cyc.seq} 段（{'放电' if cyc.kind == 'discharge' else '充电'}）"

    if not cyc.closed:
        flags.append({
            "code": "UNCLOSED_CYCLE", "severity": "warning",
            "message": f"{where}在日志结束时仍未回到静置，循环未闭合，容量统计可能偏小。",
            "evidence": f"end_t={cyc.end_t:.0f}s, capacity={cyc.capacity_ah:.3f}Ah",
        })
    if cyc.kind == "discharge" and rated_ah > 0:
        ratio = cyc.capacity_ah / rated_ah
        if ratio > OVER_CAPACITY_RATIO:
            flags.append({
                "code": "OVER_CAPACITY", "severity": "info",
                "message": f"{where}放出 {cyc.capacity_ah:.2f}Ah，达额定容量的 "
                           f"{ratio * 100:.0f}%，额定容量设置可能偏低或电流采样存在偏差。",
                "evidence": f"capacity={cyc.capacity_ah:.3f}Ah, rated={rated_ah:.3f}Ah",
            })
    if cyc.kind == "discharge" and cyc.min_voltage < safe_vmin - VOLT_MARGIN_V:
        flags.append({
            "code": "OVER_DISCHARGE", "severity": "critical",
            "message": f"{where}最低电压 {cyc.min_voltage:.2f}V 低于安全下限 "
                       f"{safe_vmin:.2f}V，存在过放风险。",
            "evidence": f"min_voltage={cyc.min_voltage:.3f}V, safe_min={safe_vmin:.3f}V",
        })
    if cyc.kind == "charge" and cyc.max_voltage > safe_vmax + VOLT_MARGIN_V:
        flags.append({
            "code": "OVER_VOLTAGE", "severity": "critical",
            "message": f"{where}最高电压 {cyc.max_voltage:.2f}V 高于安全上限 "
                       f"{safe_vmax:.2f}V，存在过压风险。",
            "evidence": f"max_voltage={cyc.max_voltage:.3f}V, safe_max={safe_vmax:.3f}V",
        })
    if cyc.max_temp_c >= TEMP_CRIT_C:
        flags.append({
            "code": "TEMP_CRITICAL", "severity": "critical",
            "message": f"{where}最高温度 {cyc.max_temp_c:.1f}°C 达到严重阈值 "
                       f"{TEMP_CRIT_C:.0f}°C，异常发热。",
            "evidence": f"max_temp={cyc.max_temp_c:.1f}°C",
        })
    elif cyc.max_temp_c >= TEMP_WARN_C:
        flags.append({
            "code": "TEMP_HIGH", "severity": "warning",
            "message": f"{where}最高温度 {cyc.max_temp_c:.1f}°C 超过告警阈值 "
                       f"{TEMP_WARN_C:.0f}°C。",
            "evidence": f"max_temp={cyc.max_temp_c:.1f}°C",
        })
    if cyc.temp_rise_c >= TEMP_RISE_WARN_C:
        flags.append({
            "code": "TEMP_RISE", "severity": "warning",
            "message": f"{where}温升 {cyc.temp_rise_c:.1f}°C 超过 "
                       f"{TEMP_RISE_WARN_C:.0f}°C，发热异常。",
            "evidence": f"temp_rise={cyc.temp_rise_c:.1f}°C",
        })
    return flags


# ---------------------------------------------------------------- 汇总评估

def _linregress(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    return slope, my - slope * mx


GRADE_LABELS = {"good": "良好", "fair": "一般", "poor": "偏差", "critical": "严重"}


def build_assessment(battery: dict, cycles: list[dict], log_count: int) -> dict:
    """跨日志汇总：SOH、容量突降、趋势与健康等级。"""
    rated = battery["rated_capacity_ah"]
    dis = [c for c in cycles if c["kind"] == "discharge"]
    chg = [c for c in cycles if c["kind"] == "charge"]

    # 1) 汇总各循环在导入时已生成的标记
    flags: list[dict] = []
    for c in cycles:
        for f in c["flags"]:
            flags.append({**f, "log_id": c["log_id"], "cycle_seq": c["seq"],
                          "kind": c["kind"], "start_iso": c.get("start_iso")})

    # 2) 相邻放电循环容量突降
    caps = [c["capacity_ah"] for c in dis]
    for idx, (prev, cur) in enumerate(zip(dis, dis[1:]), start=2):
        if prev["capacity_ah"] > 0:
            drop = (prev["capacity_ah"] - cur["capacity_ah"]) / prev["capacity_ah"]
            if drop >= CAPACITY_DROP_RATIO:
                sev = "critical" if drop >= CAPACITY_DROP_CRIT else "warning"
                flags.append({
                    "code": "CAPACITY_DROP", "severity": sev,
                    "log_id": cur["log_id"], "cycle_seq": cur["seq"],
                    "kind": "discharge", "start_iso": cur.get("start_iso"),
                    "message": f"第 {idx} 个放电循环容量 {cur['capacity_ah']:.2f}Ah，"
                               f"较上一循环 {prev['capacity_ah']:.2f}Ah 突降 "
                               f"{drop * 100:.1f}%（阈值 {CAPACITY_DROP_RATIO * 100:.0f}%）。",
                    "evidence": f"prev={prev['capacity_ah']:.3f}Ah, cur={cur['capacity_ah']:.3f}Ah",
                })

    # 3) SOH / 衰减 / 趋势
    best = max(caps) if caps else 0.0
    recent = caps[-3:]
    recent_avg = sum(recent) / len(recent) if recent else 0.0
    soh = best / rated * 100.0 if rated else None
    fade = (best - recent_avg) / best * 100.0 if best else 0.0
    slope, _ = _linregress([float(i) for i in range(1, len(caps) + 1)], caps)
    fade_rate = slope / rated * 100.0 if rated else 0.0
    irs = [c["ir_mohm"] for c in cycles if c.get("ir_mohm") is not None]
    avg_ir = sum(irs) / len(irs) if irs else None
    max_temp = max((c["max_temp_c"] for c in cycles), default=None)

    # 单体失衡汇总（仅有单体数据的循环参与）
    cell_cycles = [c for c in cycles if c.get("cell_max_diff_v") is not None]
    cell_worst = max((c["cell_max_diff_v"] for c in cell_cycles), default=None)
    cell_imbalanced = sum(1 for c in cell_cycles
                          if any(f["code"] == "CELL_IMBALANCE" for f in c["flags"]))
    cell_count = max((c.get("cell_count", 0) for c in cell_cycles), default=0)

    # 4) 健康等级
    rank = {"info": 0, "warning": 1, "critical": 2}
    worst = max((rank.get(f["severity"], 0) for f in flags), default=0)
    if worst >= 2 or (soh is not None and soh < 60):
        grade = "critical"
    elif (soh is not None and soh < 70) or fade > 30:
        grade = "poor"
    elif worst >= 1 or (soh is not None and soh < 85) or fade > 15:
        grade = "fair"
    else:
        grade = "good"

    flags.sort(key=lambda f: -rank.get(f["severity"], 0))

    trend = [{"n": i + 1, "capacity_ah": round(c["capacity_ah"], 4),
              "log_id": c["log_id"], "cycle_seq": c["seq"],
              "start_iso": c.get("start_iso"), "ir_mohm": c.get("ir_mohm"),
              "max_temp_c": c["max_temp_c"],
              "cell_max_diff_v": (round(c["cell_max_diff_v"], 4)
                                  if c.get("cell_max_diff_v") is not None else None),
              "cell_sustain_s": round(c.get("cell_sustain_s", 0.0), 3)}
             for i, c in enumerate(dis)]

    parts = [
        f"共导入 {log_count} 份日志，识别放电循环 {len(dis)} 个、充电循环 {len(chg)} 个。",
    ]
    if dis:
        parts.append(
            f"最佳放电容量 {best:.2f}Ah（额定 {rated:.2f}Ah），估算 SOH {soh:.1f}%；"
            f"最近 {len(recent)} 次平均 {recent_avg:.2f}Ah，较最佳值衰减 {fade:.1f}%。")
        if len(caps) >= 2:
            parts.append(f"容量趋势约每循环 {fade_rate:+.2f}% 额定容量。")
    if avg_ir is not None:
        parts.append(f"平均估算内阻 {avg_ir:.1f}mΩ。")
    if max_temp is not None:
        parts.append(f"历史最高温度 {max_temp:.1f}°C。")
    if cell_cycles:
        parts.append(
            f"单体电压（{cell_count} 节）覆盖 {len(cell_cycles)} 个循环，"
            f"最大压差 {cell_worst:.3f}V，持续超差告警循环 {cell_imbalanced} 个。")
    n_crit = sum(1 for f in flags if f["severity"] == "critical")
    n_warn = sum(1 for f in flags if f["severity"] == "warning")
    parts.append(f"判定标记：严重 {n_crit} 条、警告 {n_warn} 条；综合健康等级：{GRADE_LABELS[grade]}。")

    # 放电容量可信覆盖（独立于健康判定：不修改容量/SOH/健康等级，
    # 可信容量只是可信时段的部分积分，不代表真实总容量）
    coverage = _build_coverage_summary(dis)
    if dis:
        parts.append(
            f"放电容量可信覆盖：已评估 {coverage['assessed_cycles']} 个，"
            f"其中全可信 {coverage['fully_trusted_cycles']} 个、"
            f"含可疑段 {coverage['suspicious_cycles']} 个（低覆盖 "
            f"{coverage['low_coverage_cycles']} 个，可信时长占比阈值 "
            f"{LOW_COVERAGE_RATIO * 100:.0f}%），"
            f"{coverage['unavailable_cycles']} 个因缺少审计记录无法评估"
            f"（不按全可信处理）；可信容量仅为可信时段的部分积分，"
            f"不代表真实总容量。")

    return {
        "summary": {
            "total_logs": log_count,
            "total_cycles": len(cycles),
            "discharge_cycles": len(dis),
            "charge_cycles": len(chg),
            "rated_capacity_ah": rated,
            "best_discharge_ah": round(best, 4),
            "recent_avg_ah": round(recent_avg, 4),
            "soh_percent": round(soh, 1) if soh is not None else None,
            "fade_percent": round(fade, 1),
            "fade_per_cycle_percent": round(fade_rate, 3),
            "avg_ir_mohm": round(avg_ir, 2) if avg_ir is not None else None,
            "max_temp_c": max_temp,
            "cell_count": cell_count,
            "cell_monitored_cycles": len(cell_cycles),
            "cell_imbalance_cycles": cell_imbalanced,
            "cell_worst_diff_v": round(cell_worst, 4)
            if cell_worst is not None else None,
            "cell_diff_warn_v": CELL_DIFF_WARN_V,
            "cell_sustain_min_s": CELL_SUSTAIN_MIN_S,
            "health_grade": grade,
            "health_label": GRADE_LABELS[grade],
        },
        "summary_text": "".join(parts),
        "flags": flags,
        "trend": trend,
        "cycles": cycles,
        "coverage": coverage,
    }


def _build_coverage_summary(dis: list[dict]) -> dict:
    """跨放电循环汇总容量可信覆盖评估结果（仅统计，不参与健康等级判定）。"""
    assessed_pairs: list[tuple[dict, dict]] = []   # (cycle, coverage)
    unavailable: list[dict] = []
    for c in dis:
        cov = c.get("coverage")
        if cov and cov.get("status") == STATUS_OK:
            assessed_pairs.append((c, cov))
        else:
            unavailable.append(c)

    suspicious_pairs = [(c, cov) for c, cov in assessed_pairs
                        if cov["suspicious_duration_s"] > 0]
    low_pairs = [(c, cov) for c, cov in suspicious_pairs
                 if (cov.get("trusted_duration_ratio") is not None
                     and cov["trusted_duration_ratio"] < LOW_COVERAGE_RATIO)]

    def detail(c: dict, cov: dict) -> dict:
        return {
            "log_id": c["log_id"], "cycle_seq": c["seq"],
            "start_iso": c.get("start_iso"),
            "trusted_duration_ratio": cov.get("trusted_duration_ratio"),
            "trusted_capacity_ah": cov.get("trusted_capacity_ah"),
            "suspicious_capacity_ah": cov.get("suspicious_capacity_ah"),
            "trusted_duration_s": cov.get("trusted_duration_s"),
            "suspicious_duration_s": cov.get("suspicious_duration_s"),
            "low_coverage": (cov.get("trusted_duration_ratio") is not None
                             and cov["trusted_duration_ratio"] < LOW_COVERAGE_RATIO),
            "suspicious_segments": cov.get("suspicious_segments", []),
        }

    ratios = [cov["trusted_duration_ratio"] for _, cov in assessed_pairs
              if cov["trusted_duration_ratio"] is not None]
    return {
        "discharge_cycles": len(dis),
        "assessed_cycles": len(assessed_pairs),
        "unavailable_cycles": len(unavailable),
        "fully_trusted_cycles": len(assessed_pairs) - len(suspicious_pairs),
        "suspicious_cycles": len(suspicious_pairs),
        "low_coverage_cycles": len(low_pairs),
        "low_coverage_threshold": LOW_COVERAGE_RATIO,
        "min_trusted_duration_ratio": round(min(ratios), 4) if ratios else None,
        "suspicious_detail": [detail(c, cov) for c, cov in suspicious_pairs],
        "unavailable_detail": [
            {"log_id": c["log_id"], "cycle_seq": c["seq"],
             "start_iso": c.get("start_iso")} for c in unavailable],
        "note": "可信容量仅为剔除可疑段后可信时段的部分积分，不代表电池真实总容量；"
                "容量、SOH 与健康等级仍基于完整积分，不受本评估影响。"
                "无审计记录的旧日志标记为无法评估，不按全可信处理。",
    }
