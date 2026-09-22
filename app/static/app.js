/* 无人机电池循环健康评估 —— 原生前端逻辑 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = {
  batteries: [], currentId: null, currentLogId: null, logDetail: null,
  qaFilter: "all",
};

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "show" + (isError ? " error" : "");
  setTimeout(() => { el.className = ""; }, 3200);
}

const fmt = (v, d = 2) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d);
const fmtDur = (s) => {
  if (s === null || s === undefined) return "—";
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return `${m}分${String(sec).padStart(2, "0")}秒`;
};
const fmtTime = (iso) => iso ? new Date(iso).toLocaleString("zh-CN", { hour12: false }) : "—";

/* ---------------- 数据质量审计 ---------------- */

const Q_TYPE_LABELS = {
  outlier: "稳健离群点",
  out_of_order: "时间戳乱序",
  duplicate_time: "重复时间戳",
  interval_gap: "采样缺口",
  interval_short: "过密采样",
};
const Q_SEV_LABELS = { critical: "严重", warning: "警告", info: "提示" };
const Q_FIELD_LABELS = { time: "时间", voltage: "电压", current: "电流", temperature: "温度" };
const Q_TYPE_COLORS = {
  outlier: "#ef5350",
  out_of_order: "#ff8a65",
  duplicate_time: "#f0b429",
  interval_gap: "#b388ff",
  interval_short: "#64b5f6",
};

function qTypeLabel(t) { return Q_TYPE_LABELS[t] || t; }
function qSevClass(sev) { return sev === "critical" ? "critical" : sev === "warning" ? "warning" : "info"; }
function qFieldChips(fields) {
  return fields.map((f) => `<span class="field-chip">${Q_FIELD_LABELS[f] || f}</span>`).join("");
}
function qualityGradeOf(qa) {
  if (!qa) return "good";
  if (!qa.time_ordered || qa.counts_by_severity.critical > 0) return "critical";
  if (qa.counts_by_severity.warning > 0) return "fair";
  if (qa.counts_by_severity.info > 0) return "fair";
  return "good";
}
function qualityBadgeHtml(qa) {
  if (!qa) return `<span class="qbadge good">未审计</span>`;
  const g = qualityGradeOf(qa);
  if (qa.segment_count === 0) return `<span class="qbadge good">✓ 无质量问题</span>`;
  const label = g === "critical" ? "严重问题" : g === "fair" ? "存在问题" : "轻微提示";
  return `<span class="qbadge ${g}">${label} · ${qa.segment_count} 区段 / ${qa.issue_count} 项</span>`;
}

function formatAuditOneLine(qa) {
  if (!qa) return "";
  if (qa.segment_count === 0) {
    return `数据质量审计通过（${qa.parsed_rows} 行${qa.skipped_rows ? `，另有 ${qa.skipped_rows} 行无法解析已跳过` : ""}）。`;
  }
  const t = qa.counts_by_type;
  const parts = [];
  if (t.out_of_order) parts.push(`时间戳乱序 ${t.out_of_order} 处`);
  if (t.duplicate_time) parts.push(`重复时间 ${t.duplicate_time} 处`);
  if (t.interval_gap) parts.push(`采样缺口 ${t.interval_gap} 处`);
  if (t.interval_short) parts.push(`过密采样 ${t.interval_short} 处`);
  if (t.outlier) parts.push(`稳健离群点 ${t.outlier} 个`);
  return `质量审计发现 ${qa.segment_count} 个区段（${parts.join("、")}），已保留证据且不影响循环分析。`;
}

/* ---------------- 电池列表 ---------------- */

async function loadBatteries(keepSelection = false) {
  state.batteries = await api("/api/batteries");
  const ul = $("#battery-list");
  ul.innerHTML = "";
  for (const b of state.batteries) {
    const li = document.createElement("li");
    li.dataset.id = b.id;
    li.innerHTML = `<div class="b-name"></div>
      <div class="b-sub">额定 ${b.rated_capacity_ah}Ah · ${b.safe_voltage_min}~${b.safe_voltage_max}V</div>
      <div class="b-sub">日志 ${b.log_count} 份 · 放电循环 ${b.discharge_cycles} 个</div>`;
    li.querySelector(".b-name").textContent = b.name;
    if (Number(b.id) === Number(state.currentId)) li.classList.add("active");
    li.onclick = () => selectBattery(b.id);
    ul.appendChild(li);
  }
  if (!keepSelection && state.batteries.length && state.currentId === null) {
    selectBattery(state.batteries[0].id);
  }
  if (!state.batteries.length) {
    state.currentId = null;
    $("#battery-view").hidden = true;
    $("#empty-tip").hidden = false;
  }
}

$("#battery-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    name: f.name.value.trim(),
    rated_capacity_ah: parseFloat(f.rated_capacity_ah.value),
    safe_voltage_min: parseFloat(f.safe_voltage_min.value),
    safe_voltage_max: parseFloat(f.safe_voltage_max.value),
    notes: f.notes.value.trim(),
  };
  try {
    const b = await api("/api/batteries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    f.reset();
    state.currentId = b.id;
    await loadBatteries(true);
    selectBattery(b.id);
    toast("电池已创建");
  } catch (err) { toast(err.message, true); }
};

/* ---------------- 电池详情 ---------------- */

async function selectBattery(id) {
  state.currentId = id;
  state.currentLogId = null;
  state.logDetail = null;
  state.qaFilter = "all";
  document.querySelectorAll("#battery-list li").forEach((li) =>
    li.classList.toggle("active", Number(li.dataset.id) === Number(id)));
  $("#empty-tip").hidden = true;
  $("#battery-view").hidden = false;
  $("#log-detail").hidden = true;
  try {
    const b = await api(`/api/batteries/${id}`);
    $("#b-name").textContent = b.name;
    $("#b-meta").textContent =
      `创建于 ${fmtTime(b.created_at)}${b.notes ? " · 备注：" + b.notes : ""}`;
    const f = $("#settings-form");
    f.rated_capacity_ah.value = b.rated_capacity_ah;
    f.safe_voltage_min.value = b.safe_voltage_min;
    f.safe_voltage_max.value = b.safe_voltage_max;
    await Promise.all([loadAssessment(), loadLogs()]);
  } catch (err) { toast(err.message, true); }
}

$("#settings-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api(`/api/batteries/${state.currentId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rated_capacity_ah: parseFloat(f.rated_capacity_ah.value),
        safe_voltage_min: parseFloat(f.safe_voltage_min.value),
        safe_voltage_max: parseFloat(f.safe_voltage_max.value),
      }),
    });
    toast("设置已保存（对后续导入的日志生效）");
    loadBatteries(true);
  } catch (err) { toast(err.message, true); }
};

$("#btn-delete").onclick = async () => {
  if (!confirm("确定删除该电池及其全部日志与评估数据？")) return;
  try {
    await api(`/api/batteries/${state.currentId}`, { method: "DELETE" });
    state.currentId = null;
    await loadBatteries();
    toast("已删除");
  } catch (err) { toast(err.message, true); }
};

$("#btn-export").onclick = () => {
  window.open(`/api/batteries/${state.currentId}/export`, "_blank");
};

/* ---------------- 日志导入 ---------------- */

$("#btn-upload").onclick = async () => {
  const input = $("#csv-file");
  if (!input.files.length) { toast("请先选择 CSV 文件", true); return; }
  const fd = new FormData();
  fd.append("file", input.files[0]);
  $("#upload-result").textContent = "上传分析中…";
  try {
    const r = await api(`/api/batteries/${state.currentId}/logs`,
      { method: "POST", body: fd });
    const dis = r.cycles.filter((c) => c.kind === "discharge").length;
    const chg = r.cycles.length - dis;
    let msg =
      `导入成功：${r.log.row_count} 行样本，识别放电循环 ${dis} 个、充电循环 ${chg} 个` +
      (r.skipped_rows ? `，跳过无效行 ${r.skipped_rows} 行` : "") + "。";
    if (r.cells && r.cells.present) {
      if (r.cells.enabled) {
        msg += `识别单体电压列 ${r.cells.cell_count} 个，参与单体分析 ${r.cells.rows_with_cells} 行`;
        if (r.cells.rows_skipped) {
          const reasons = Object.entries(r.cells.skip_reasons)
            .map(([why, rec]) => `${why} ${rec.count} 行（如第 ${rec.rows.join("、")} 行）`)
            .join("；");
          msg += `；${r.cells.rows_skipped} 行跳过单体分析（${reasons}）`;
        }
        const imbalanced = r.cycles.filter((c) =>
          c.flags.some((f) => f.code === "CELL_IMBALANCE")).length;
        msg += imbalanced ? `；单体失衡告警循环 ${imbalanced} 个。` : "；未发现持续单体失衡。";
      } else {
        msg += `识别到单体列但未启用单体分析（${r.cells.disabled_reason}）。`;
      }
    }
    msg += formatAuditOneLine(r.quality_audit);
    $("#upload-result").textContent = msg;
    input.value = "";
    await Promise.all([loadAssessment(), loadLogs(), loadBatteries(true)]);
  } catch (err) {
    $("#upload-result").textContent = "";
    toast(err.message, true);
  }
};

async function loadLogs() {
  const logs = await api(`/api/batteries/${state.currentId}/logs`);
  const tbody = $("#logs-table tbody");
  tbody.innerHTML = "";
  for (const l of logs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${l.id}</td><td></td><td>${fmtTime(l.imported_at)}</td>
      <td>${fmtDur(l.duration_s)}</td><td>${l.row_count}</td>
      <td>${l.discharge_cycles} / ${l.charge_cycles}</td>
      <td class="qcell"></td>
      <td><button class="danger" data-del="${l.id}">删除</button></td>`;
    tr.children[1].textContent = l.filename;
    tr.children[6].innerHTML = qualityBadgeHtml(l.quality);
    tr.onclick = (e) => {
      if (e.target.dataset.del) return;
      selectLog(l.id, tr);
    };
    tr.querySelector("[data-del]").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`删除日志 #${l.id}？`)) return;
      try {
        await api(`/api/logs/${l.id}`, { method: "DELETE" });
        if (state.currentLogId === l.id) {
          state.currentLogId = null;
          $("#log-detail").hidden = true;
        }
        await Promise.all([loadAssessment(), loadLogs(), loadBatteries(true)]);
        toast("日志已删除");
      } catch (err) { toast(err.message, true); }
    };
    tbody.appendChild(tr);
  }
  if (!logs.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">暂无日志，请上传 CSV。</td></tr>`;
  }
}

/* ---------------- 评估展示 ---------------- */

async function loadAssessment() {
  const a = await api(`/api/batteries/${state.currentId}/assessment`);
  const card = $("#assessment-card");
  if (!a.summary.total_cycles) { card.hidden = true; return; }
  card.hidden = false;
  const s = a.summary;
  const cards = [
    ["健康等级", s.health_label, s.health_grade],
    ["SOH（最佳容量/额定）", s.soh_percent !== null ? fmt(s.soh_percent, 1) + "%" : "—", ""],
    ["最佳放电容量", fmt(s.best_discharge_ah) + " Ah", ""],
    ["近期平均容量", fmt(s.recent_avg_ah) + " Ah", ""],
    ["较最佳值衰减", fmt(s.fade_percent, 1) + "%", ""],
    ["每循环衰减", fmt(s.fade_per_cycle_percent, 2) + "%", ""],
    ["平均内阻", s.avg_ir_mohm !== null ? fmt(s.avg_ir_mohm, 1) + " mΩ" : "—", ""],
    ["历史最高温度", s.max_temp_c !== null ? fmt(s.max_temp_c, 1) + " °C" : "—", ""],
    ["单体最大压差", s.cell_worst_diff_v !== null
      ? fmt(s.cell_worst_diff_v, 3) + " V"
      + (s.cell_imbalance_cycles ? `（${s.cell_imbalance_cycles} 段失衡）` : "")
      : "—", s.cell_imbalance_cycles ? "fair" : ""],
    ["放电 / 充电循环", `${s.discharge_cycles} / ${s.charge_cycles}`, ""],
  ];
  $("#summary-cards").innerHTML = cards.map(([k, v, cls]) =>
    `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`
  ).join("");
  $("#summary-text").textContent = a.summary_text;

  const sevLabel = { critical: "严重", warning: "警告", info: "提示" };
  $("#flag-count").textContent = `共 ${a.flags.length} 条`;
  $("#flag-list").innerHTML = a.flags.length
    ? a.flags.map((f) => `<li class="${f.severity}">
        <span class="badge">${sevLabel[f.severity] || f.severity}</span>${f.message}
        <span class="evidence">依据：${f.evidence} · 日志 #${f.log_id} 循环 #${f.cycle_seq}${f.start_iso ? " · " + fmtTime(f.start_iso) : ""}</span>
      </li>`).join("")
    : `<li class="info"><span class="badge">正常</span>未发现异常标记。</li>`;

  drawTrend(a.trend, a.battery.rated_capacity_ah);
  renderQualitySummary(a.data_quality, a.logs);
  renderCoverageSummary(a.coverage);
}

function renderCoverageSummary(cov) {
  const el = $("#coverage-summary");
  if (!cov || !cov.discharge_cycles) {
    el.innerHTML = `<p class="muted">暂无放电循环，无法评估容量可信覆盖。</p>`;
    return;
  }
  const pct = (r) => r === null || r === undefined ? "—" : (r * 100).toFixed(1) + "%";
  const items = [
    ["放电循环", cov.discharge_cycles, ""],
    ["已评估 / 全可信", `${cov.assessed_cycles} / ${cov.fully_trusted_cycles}`, "good"],
    ["含可疑段", cov.suspicious_cycles, cov.suspicious_cycles ? "fair" : "good"],
    ["低覆盖循环", cov.low_coverage_cycles, cov.low_coverage_cycles ? "critical" : "good"],
    ["无法评估", cov.unavailable_cycles, cov.unavailable_cycles ? "fair" : "good"],
    ["最低可信时长占比", pct(cov.min_trusted_duration_ratio),
     cov.min_trusted_duration_ratio !== null && cov.min_trusted_duration_ratio < cov.low_coverage_threshold
       ? "critical" : "good"],
  ];
  let html = `<div class="quality-metrics">${items.map(([k, v, cls]) =>
    `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`).join("")}</div>`;

  const details = cov.suspicious_detail || [];
  if (details.length) {
    html += `<ul class="cov-list">${details.map((d) => `<li class="${d.low_coverage ? "critical" : "warning"}">
      <span class="badge">${d.low_coverage ? "低覆盖" : "含可疑段"}</span>
      日志 #${d.log_id} 放电循环 #${d.cycle_seq}${d.start_iso ? " · " + fmtTime(d.start_iso) : ""}：
      可信时长占比 <b>${pct(d.trusted_duration_ratio)}</b>
      （可信 ${fmt(d.trusted_capacity_ah, 3)}Ah / ${fmtDur(d.trusted_duration_s)}，
      可疑 ${fmt(d.suspicious_capacity_ah, 3)}Ah / ${fmtDur(d.suspicious_duration_s)}）
      <div class="cov-segs">${(d.suspicious_segments || []).map((s) => {
        const range = s.line_start === s.line_end
          ? `第 ${s.line_start} 行` : `第 ${s.line_start}~${s.line_end} 行`;
        return `<span class="cov-seg">${range} · ${(s.reason_labels || []).join("、")}
          （${fmtDur(s.duration_s)}，${fmt(s.capacity_ah, 3)}Ah）</span>`;
      }).join("")}</div>
    </li>`).join("")}</ul>`;
  }
  const unavail = cov.unavailable_detail || [];
  if (unavail.length) {
    html += `<ul class="cov-list">${unavail.map((d) => `<li class="warning">
      <span class="badge">无法评估</span>
      日志 #${d.log_id} 放电循环 #${d.cycle_seq}${d.start_iso ? " · " + fmtTime(d.start_iso) : ""}：
      旧版导入无质量审计记录，无法评估可信覆盖，<b>不按全可信处理</b>。</li>`).join("")}</ul>`;
  }
  html += `<p class="muted">低覆盖阈值：可信时长占比 &lt; ${(cov.low_coverage_threshold * 100).toFixed(0)}%。
    ${cov.note || ""}原有容量、SOH 与健康等级均基于完整积分，未受本评估影响。</p>`;
  el.innerHTML = html;
}

function renderQualitySummary(dq, logs) {
  const el = $("#quality-summary");
  if (!dq || !dq.total_logs) { el.innerHTML = ""; return; }
  const t = dq.counts_by_type || {};
  const items = [
    ["审计日志", `${dq.logs_with_issues}/${dq.total_logs} 份含问题`,
     dq.logs_with_issues ? "fair" : "good"],
    ["质量区段", dq.total_segments, dq.total_segments ? "fair" : "good"],
    ["问题项总数", dq.total_issues, dq.total_issues ? "fair" : "good"],
    ["时间戳乱序日志", dq.unordered_logs, dq.unordered_logs ? "critical" : "good"],
    ["稳健离群点", t.outlier || 0, t.outlier ? "fair" : "good"],
    ["重复时间戳", t.duplicate_time || 0, t.duplicate_time ? "fair" : "good"],
    ["采样缺口", t.interval_gap || 0, t.interval_gap ? "fair" : "good"],
    ["过密采样", t.interval_short || 0, t.interval_short ? "fair" : "good"],
    ["无法解析跳过行", dq.skipped_rows, dq.skipped_rows ? "fair" : "good"],
  ];
  const affected = (dq.affected_fields || []).map((f) => Q_FIELD_LABELS[f] || f);
  el.innerHTML = `<div class="quality-metrics">${items.map(([k, v, cls]) =>
    `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`
  ).join("")}</div>
  <p class="muted">受影响字段：${affected.length ? affected.join("、") : "无"}。
  质量问题仅作为证据保留，不阻断任何可解析行的循环分析。
  完整逐区段证据见各日志详情与导出 JSON（<code>data_quality</code> / <code>logs[].quality_audit</code>）。</p>`;
}

/* ---------------- 日志详情与图表 ---------------- */

async function selectLog(logId, rowEl) {
  state.currentLogId = logId;
  document.querySelectorAll("#logs-table tbody tr").forEach((tr) =>
    tr.classList.toggle("selected", tr === rowEl));
  try {
    const d = await api(`/api/logs/${logId}`);
    state.logDetail = d;
    $("#log-detail").hidden = false;
    $("#log-title").textContent =
      `日志 #${logId}：${d.log.filename}（${d.log.row_count} 行样本）`;
    renderCyclesTable(d.cycles);
    renderQualityPanel(d.log.quality_audit);
    drawLogChart(d.samples, d.cycles, d.log.quality_audit);
    $("#log-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) { toast(err.message, true); }
}

function renderCyclesTable(cycles) {
  const tbody = $("#cycles-table tbody");
  tbody.innerHTML = "";
  for (const c of cycles) {
    const tr = document.createElement("tr");
    const kindTag = c.kind === "discharge"
      ? `<span class="tag discharge">放电</span>`
      : `<span class="tag charge">充电</span>`;
    const stateTag = c.closed
      ? `<span class="tag closed">闭合</span>`
      : `<span class="tag open">未闭合</span>`;
    const flagTxt = c.flags.length
      ? ` ⚠ ${c.flags.map((f) => f.code).join(", ")}` : "";
    const cellTxt = c.cell_count
      ? `${c.cell_count} 节 · ${fmt(c.cell_max_diff_v, 3)}V` +
        (c.cell_sustain_s > 0 ? ` · ${fmt(c.cell_sustain_s, 0)}s` : "")
      : "—";
    tr.innerHTML = `<td>${c.seq}</td><td>${kindTag}</td>
      <td>${c.start_iso ? fmtTime(c.start_iso) : fmt(c.start_t, 0) + "s"}</td>
      <td>${fmtDur(c.duration_s)}</td><td>${fmt(c.capacity_ah, 3)}</td>
      <td class="covcell">${coverageCellHtml(c)}</td>
      <td>${fmt(c.energy_wh, 1)}</td><td>${fmt(c.avg_current_a, 2)}</td>
      <td>${fmt(c.min_voltage)} ~ ${fmt(c.max_voltage)}</td>
      <td>${fmt(c.max_temp_c, 1)}</td><td>${fmt(c.temp_rise_c, 1)}</td>
      <td>${c.ir_mohm !== null ? fmt(c.ir_mohm, 1) : "—"}</td>
      <td>${cellTxt}</td>
      <td>${stateTag}${flagTxt}</td>`;
    tbody.appendChild(tr);
  }
  if (!cycles.length) {
    tbody.innerHTML = `<tr><td colspan="14" class="muted">未识别到完整充放电循环。</td></tr>`;
  }
}

/* ---------------- 放电容量可信覆盖 ---------------- */

const COV_REASON_LABELS = {
  current_outlier: "电流离群点相邻区间",
  interval_gap: "采样缺口区间",
};

function covReasonLabel(r) { return COV_REASON_LABELS[r] || r; }

function coverageCellHtml(c) {
  if (c.kind !== "discharge") return `<span class="muted">—</span>`;
  const cov = c.coverage;
  if (!cov) return `<span class="muted">—</span>`;
  if (cov.status === "unavailable") {
    return `<span class="qbadge fair" title="${cov.message || "缺少导入质量审计记录"}">无法评估</span>`;
  }
  const ratio = cov.trusted_duration_ratio;
  const segs = cov.suspicious_segments || [];
  if (!segs.length) {
    return `<span class="qbadge good" title="全部梯形积分区间均无电流离群点相邻或采样缺口">✓ 全可信</span>`;
  }
  const low = ratio !== null && ratio < 0.95;
  const tip = `可信 ${fmt(cov.trusted_capacity_ah, 3)}Ah / ${fmtDur(cov.trusted_duration_s)}；` +
    `可疑 ${fmt(cov.suspicious_capacity_ah, 3)}Ah / ${fmtDur(cov.suspicious_duration_s)}。` +
    `可信容量仅为可信时段的部分积分，不代表真实总容量。`;
  const segHtml = segs.map((s) => {
    const range = s.line_start === s.line_end
      ? `第 ${s.line_start} 行` : `第 ${s.line_start}~${s.line_end} 行`;
    return `<div class="cov-seg" title="可疑容量 ${fmt(s.capacity_ah, 3)}Ah / ${fmtDur(s.duration_s)}">` +
      `${range} · ${(s.reason_labels || []).join("、") || s.reasons.map(covReasonLabel).join("、")}` +
      `</div>`;
  }).join("");
  return `<div class="cov-wrap" title="${tip}">` +
    `<span class="qbadge ${low ? "critical" : "fair"}">可信 ${(ratio * 100).toFixed(1)}%</span>` +
    `<span class="muted cov-sub">${segs.length} 个可疑段</span>${segHtml}</div>`;
}

/* ---------------- 日志质量审计面板 ---------------- */

function renderQualityPanel(qa) {
  const panel = $("#quality-panel");
  if (!qa) { panel.hidden = true; return; }
  panel.hidden = false;
  $("#quality-panel-sub").textContent =
    `共 ${qa.parsed_rows} 行可解析${qa.skipped_rows ? `，${qa.skipped_rows} 行无法解析已跳过` : ""}` +
    ` · 典型采样间隔 ${qa.interval.sample_interval_s !== null ? qa.interval.sample_interval_s + "s" : "—"}`;

  const cs = qa.counts_by_severity || {};
  const metrics = [
    ["质量区段", qa.segment_count, qa.segment_count ? "fair" : "good"],
    ["问题项", qa.issue_count, qa.issue_count ? "fair" : "good"],
    ["严重", cs.critical || 0, cs.critical ? "critical" : "good"],
    ["警告", cs.warning || 0, cs.warning ? "fair" : "good"],
    ["提示", cs.info || 0, cs.info ? "fair" : "good"],
    ["乱序", qa.time_ordered ? 0 : 1, qa.time_ordered ? "good" : "critical"],
  ];
  $("#quality-metrics").innerHTML = metrics.map(([k, v, cls]) =>
    `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`).join("");

  const filterable = ["all", ...Object.keys(Q_TYPE_LABELS)];
  const counts = qa.counts_by_type;
  $("#quality-filters").innerHTML = filterable.map((typ) => {
    const n = typ === "all" ? qa.segment_count : (counts[typ] || 0);
    const active = state.qaFilter === typ ? " active" : "";
    const label = typ === "all" ? "全部" : qTypeLabel(typ);
    return `<button class="qfilter${active}" data-qfilter="${typ}">${label} (${n})</button>`;
  }).join("");
  document.querySelectorAll("#quality-filters .qfilter").forEach((btn) => {
    btn.onclick = () => {
      state.qaFilter = btn.dataset.qfilter;
      renderQualityPanel(qa);
      drawLogChart(state.logDetail.samples, state.logDetail.cycles,
                   state.logDetail.log.quality_audit);
    };
  });

  renderQualitySegments(qa);
}

function renderQualitySegments(qa) {
  const ul = $("#quality-segments");
  let segs = qa.segments || [];
  if (state.qaFilter !== "all") segs = segs.filter((s) => s.type === state.qaFilter);
  if (!segs.length) {
    ul.innerHTML = qa.segment_count === 0
      ? `<li class="qitem good"><span class="qbadge good">✓ 通过</span>未发现时间戳或电压/电流/温度数据质量问题。</li>`
      : `<li class="muted">该筛选下暂无区段。</li>`;
    return;
  }
  ul.innerHTML = segs.map((s) => {
    const when = s.time_start
      ? `${fmtTime(s.time_start)} ~ ${fmtTime(s.time_end)}`
      : `t = ${s.rel_start_s}s ~ ${s.rel_end_s}s`;
    return `<li class="qitem ${qSevClass(s.severity)}">
      <div class="qhead">
        <span class="badge">${Q_SEV_LABELS[s.severity] || s.severity}</span>
        <span class="qtype" style="color:${Q_TYPE_COLORS[s.type]}">${s.type_label}</span>
        <span class="muted">第 ${s.line_start}~${s.line_end} 行 · ${when} · 涉 ${s.sample_count} 行</span>
      </div>
      <div class="qmsg">${s.message}</div>
      <div class="qfields">${qFieldChips(s.fields)}</div>
      <pre class="qevidence">${JSON.stringify(s.evidence, null, 1)}</pre>
      ${s.details_truncated ? `<div class="muted">逐条证据较多，仅展示前 ${s.details.length} / ${s.issue_count} 项；完整统计见上方汇总。</div>` : ""}
    </li>`;
  }).join("");
}

/* ---------------- Canvas 绘图 ---------------- */

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = Number(canvas.getAttribute("height"));
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  return [ctx, w, h];
}

function drawLogChart(samples, cycles, qa) {
  const canvas = $("#log-chart");
  const [ctx, W, H] = setupCanvas(canvas);
  let cellPresent = false, cellMaxN = 0;
  for (const p of samples) {
    if (p[4]) { cellPresent = true; cellMaxN = Math.max(cellMaxN, p[4].length); }
  }
  const padL = 46, padR = cellPresent ? 44 : 10, padT = 12, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  if (!samples.length) return;

  const t0 = samples[0][0], t1 = samples[samples.length - 1][0];
  const span = Math.max(t1 - t0, 1e-6);
  const x = (t) => padL + ((t - t0) / span) * iw;

  const series = [
    { name: "电压 V", idx: 1, color: "#4da3ff" },
    { name: "电流 A", idx: 2, color: "#f0b429" },
    { name: "温度 °C", idx: 3, color: "#ef5350" },
  ];
  for (const s of series) {
    let mn = Infinity, mx = -Infinity;
    for (const p of samples) {
      const v = p[s.idx];
      if (v < mn) mn = v;
      if (v > mx) mx = v;
    }
    if (mx - mn < 1e-6) { mx += 0.5; mn -= 0.5; }
    s.min = mn; s.max = mx;
    s.y = (v) => padT + ih - ((v - mn) / (mx - mn)) * ih;
  }

  // 循环区间底色
  for (const c of cycles) {
    ctx.fillStyle = c.kind === "discharge"
      ? "rgba(77,163,255,0.08)" : "rgba(52,201,142,0.08)";
    ctx.fillRect(x(c.start_t), padT, Math.max(x(c.end_t) - x(c.start_t), 1), ih);
  }
  // 单体失衡持续超差区间高亮（红色竖带）
  let hasAlarmBand = false;
  for (const c of cycles) {
    for (const iv of (c.cell_intervals || [])) {
      hasAlarmBand = true;
      ctx.fillStyle = "rgba(239,83,80,0.18)";
      ctx.fillRect(x(iv.start_t), padT,
        Math.max(x(iv.end_t) - x(iv.start_t), 1), ih);
    }
  }
  // 网格与电压刻度
  ctx.strokeStyle = "#2a3550";
  ctx.fillStyle = "#8a97b5";
  ctx.font = "10px monospace";
  ctx.lineWidth = 1;
  const vS = series[0];
  for (let g = 0; g <= 4; g++) {
    const gy = padT + (ih * g) / 4;
    ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy); ctx.stroke();
    const vv = vS.max - ((vS.max - vS.min) * g) / 4;
    ctx.fillText(vv.toFixed(2) + "V", 4, gy + 3);
  }
  // 时间刻度
  for (let g = 0; g <= 6; g++) {
    const tt = t0 + (span * g) / 6;
    const mm = Math.floor(tt / 60), ss = Math.round(tt % 60);
    ctx.fillText(`${mm}:${String(ss).padStart(2, "0")}`, x(tt) - 12, H - 8);
  }
  // 曲线
  for (const s of series) {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    samples.forEach((p, k) => {
      const px = x(p[0]), py = s.y(p[s.idx]);
      k === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    });
    ctx.stroke();
  }

  // 单体电压曲线（共用一套单体电压刻度；缺值行断开，不跨点连线）
  const CELL_COLORS = ["#b388ff", "#80cbc4", "#ffb74d", "#f06292",
    "#aed581", "#4dd0e1", "#ff8a65", "#9fa8da", "#c5e1a5", "#ffd54f",
    "#ce93d8", "#81d4fa"];
  const cellN = cellMaxN;
  let cellRange = null;
  if (cellN) {
    let mn = Infinity, mx = -Infinity;
    for (const p of samples) {
      if (!p[4]) continue;
      for (const cv of p[4]) {
        if (cv < mn) mn = cv;
        if (cv > mx) mx = cv;
      }
    }
    if (mx - mn < 1e-6) { mx += 0.5; mn -= 0.5; }
    const pad = (mx - mn) * 0.15;
    mn -= pad; mx += pad;
    const yCell = (cv) => padT + ih - ((cv - mn) / (mx - mn)) * ih;
    // 右轴：单体电压刻度
    ctx.fillStyle = "#b388ff";
    for (let g = 0; g <= 4; g++) {
      const gy = padT + (ih * g) / 4;
      const cv = mx - ((mx - mn) * g) / 4;
      ctx.fillText(cv.toFixed(2) + "V", W - padR + 4, gy + 3);
    }
    ctx.fillStyle = "#8a97b5";
    for (let k = 0; k < cellN; k++) {
      ctx.strokeStyle = CELL_COLORS[k % CELL_COLORS.length];
      ctx.lineWidth = 0.9;
      ctx.beginPath();
      let pen = false;
      for (const p of samples) {
        if (!p[4] || p[4].length !== cellN || p[4][k] === null) { pen = false; continue; }
        const px = x(p[0]), py = yCell(p[4][k]);
        pen ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
        pen = true;
      }
      ctx.stroke();
    }
    cellRange = { min: mn, max: mx, n: cellN };
  }

  // 数据质量审计标记：按筛选后的区段，在样本点上以菱形标记定位（含原始行号）
  const qMarkers = [];
  if (qa && qa.segments && samples.length) {
    const byLine = new Map();
    for (const p of samples) {
      if (p[5] !== null && p[5] !== undefined) byLine.set(p[5], p);
    }
    for (const seg of qa.segments) {
      if (state.qaFilter !== "all" && seg.type !== state.qaFilter) continue;
      const color = Q_TYPE_COLORS[seg.type] || "#fff";
      if (seg.type === "interval_gap" || seg.type === "interval_short") {
        // 区间型：半透明竖带 + 起止行三角
        ctx.fillStyle = color + "22";
        ctx.fillRect(x(seg.rel_start_s), padT,
          Math.max(x(seg.rel_end_s) - x(seg.rel_start_s), 2), ih);
      }
      for (const ln of Array.from(new Set(seg.lines || [seg.line_start, seg.line_end]))) {
        const p = byLine.get(ln);
        if (p) qMarkers.push({ p, color, seg });
      }
    }
    for (const m of qMarkers) {
      const px = x(m.p[0]);
      const py = series[0].y(m.p[1]); // 标记锚定在电压曲线
      ctx.fillStyle = m.color;
      ctx.beginPath();
      ctx.moveTo(px, py - 5); ctx.lineTo(px + 5, py);
      ctx.lineTo(px, py + 5); ctx.lineTo(px - 5, py);
      ctx.closePath(); ctx.fill();
    }
  }
  $("#chart-legend").innerHTML =
    series.map((s) =>
      `<span><span class="dot" style="background:${s.color}"></span>${s.name}
       （${s.min.toFixed(1)} ~ ${s.max.toFixed(1)}）</span>`).join("") +
    `<span>底色：<span class="dot" style="background:rgba(77,163,255,.4)"></span>放电循环
     <span class="dot" style="background:rgba(52,201,142,.4)"></span>充电循环</span>` +
    (hasAlarmBand
      ? `<span><span class="dot" style="background:rgba(239,83,80,.55)"></span>单体压差持续超差区间（&gt;0.10V 且 ≥5s）</span>`
      : "") +
    (cellRange
      ? `<span>单体电压 ${cellRange.n} 节（${cellRange.min.toFixed(2)} ~ ${cellRange.max.toFixed(2)}V，刻度见右轴）：` +
        Array.from({ length: cellRange.n }, (_, k) =>
          `<span class="dot" style="background:${CELL_COLORS[k % CELL_COLORS.length]}"></span>#${k + 1}`).join(" ") +
        "</span>"
      : "") +
    (qa && qa.segments && qa.segments.length
      ? `<span class="qlegend">质量审计：` +
        Object.entries(Q_TYPE_LABELS).map(([t, lab]) =>
          qa.counts_by_type[t]
            ? `<span class="dot" style="background:${Q_TYPE_COLORS[t]}"></span>${lab} ${qa.counts_by_type[t]}`
            : "").join(" ") +
        `（筛选：${state.qaFilter === "all" ? "全部" : qTypeLabel(state.qaFilter)}；菱形按原始行号锚定）</span>`
      : "");
}

function drawTrend(trend, ratedAh) {
  const canvas = $("#trend-chart");
  const [ctx, W, H] = setupCanvas(canvas);
  const padL = 46, padR = 10, padT = 12, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  ctx.clearRect(0, 0, W, H);
  if (!trend.length) return;

  const maxV = Math.max(...trend.map((p) => p.capacity_ah), ratedAh) * 1.15;
  const y = (v) => padT + ih - (v / maxV) * ih;
  const bw = Math.min(40, (iw / trend.length) * 0.6);

  ctx.strokeStyle = "#2a3550";
  ctx.fillStyle = "#8a97b5";
  ctx.font = "10px monospace";
  for (let g = 0; g <= 4; g++) {
    const gy = padT + (ih * g) / 4;
    ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy); ctx.stroke();
    ctx.fillText(((maxV * (4 - g)) / 4).toFixed(2), 4, gy + 3);
  }
  trend.forEach((p, k) => {
    const cx = padL + ((k + 0.5) / trend.length) * iw;
    ctx.fillStyle = "#4da3ff";
    ctx.fillRect(cx - bw / 2, y(p.capacity_ah), bw, ih - (y(p.capacity_ah) - padT));
    ctx.fillStyle = "#8a97b5";
    ctx.fillText(`#${p.n}`, cx - 8, H - 8);
    ctx.fillText(p.capacity_ah.toFixed(2), cx - 14, y(p.capacity_ah) - 4);
  });
  // 额定容量参考线
  ctx.strokeStyle = "#f0b429";
  ctx.setLineDash([5, 4]);
  ctx.beginPath(); ctx.moveTo(padL, y(ratedAh)); ctx.lineTo(W - padR, y(ratedAh)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#f0b429";
  ctx.fillText(`额定 ${ratedAh}Ah`, W - padR - 76, y(ratedAh) - 5);
}

/* ---------------- 启动 ---------------- */

window.addEventListener("resize", () => {
  if (state.logDetail && !$("#log-detail").hidden) {
    drawLogChart(state.logDetail.samples, state.logDetail.cycles,
                 state.logDetail.log.quality_audit);
  }
});
loadBatteries();
