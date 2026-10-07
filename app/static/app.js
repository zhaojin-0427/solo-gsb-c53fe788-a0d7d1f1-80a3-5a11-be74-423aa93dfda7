"use strict";

const EXAMPLES = {
  safe: `( 安全示例: 铣型腔后快移利用已切空腔 )
G21 G90 G17
G0 X10 Y10
G1 Z-3 F200
G1 X60 Y10
G1 X60 Y40
G1 X10 Y40
G1 X10 Y10
G3 X10 Y10 I25 J15 Z-3
G0 Z5
G0 X0 Y0`,
  collision: `( 碰料示例: 快移 Z 未抬够, 穿入未切余料 )
G21 G90 G17
G0 X10 Y10
G1 Z-2 F200
G1 X40 Y10
G0 Z-1
G0 X90 Y10
G0 Z5`,
  ramp: `( 斜坡槽示例: 槽深各处不同, 快移在浅端碰料 )
G21 G90 G17
G0 X10 Y40
G1 Z-1 F200
G1 X90 Y40 Z-4
G0 Z-2.5
G0 X10 Y40
G0 Z5`,
  helical: `( 螺旋槽示例: G2 整圆 + Z 线性下切 )
G21 G90 G17
G0 X50 Y40
G1 Z0 F200
G2 X50 Y40 I-8 J0 Z-2
G2 X50 Y40 I-8 J0 Z-4
G0 Z5
G0 X0 Y0`,
  error: `( 错误示例: 终点不在圆弧上 )
G21 G90 G17
G0 X10 Y10
G1 Z-2 F100
G2 X30 Y25 I10 J0
G0 Z5`,
};

const $ = (id) => document.getElementById(id);

$("exampleSelect").addEventListener("change", (e) => {
  const key = e.target.value;
  if (key && EXAMPLES[key]) $("gcode").value = EXAMPLES[key];
  e.target.value = "";
});

$("fileInput").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => { $("gcode").value = reader.result; };
  reader.readAsText(f);
});

$("checkBtn").addEventListener("click", runCheck);

async function runCheck() {
  const nums = ["stockWidth", "stockLength", "stockThickness",
                "toolDiameter", "safeHeight"];
  const payload = { gcode: $("gcode").value };
  for (const id of nums) {
    const v = parseFloat($(id).value);
    if (!Number.isFinite(v) || v <= 0) {
      alert("参数必须为正数，请检查输入");
      $(id).focus();
      return;
    }
    payload[id.replace(/[A-Z]/g, (c) => "_" + c.toLowerCase())] = v;
  }
  const btn = $("checkBtn");
  btn.disabled = true;
  btn.textContent = "检查中…";
  try {
    const res = await fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      showError(data.detail ? JSON.stringify(data.detail) : "请求失败");
      return;
    }
    render(data);
  } catch (err) {
    showError("网络或服务错误: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "开始检查";
  }
}

function showError(msg) {
  $("resultPanel").hidden = false;
  const banner = $("statusBanner");
  banner.className = "error";
  banner.textContent = "请求失败";
  $("resultDetail").innerHTML = escapeHtml(msg);
}

function render(data) {
  $("resultPanel").hidden = false;
  const banner = $("statusBanner");
  const detail = $("resultDetail");
  banner.className = data.status;

  if (data.status === "safe") {
    banner.textContent = "✔ 安全: 全部快移均未触及当前余料";
    detail.innerHTML = "无碰料。下图为加工结束时的余料深度图与刀具轨迹。" +
      (data.path_truncated ? "<br>(轨迹过长, 图中仅显示部分)" : "");
  } else if (data.status === "collision") {
    banner.textContent = "✘ 首次碰料!";
    const p = data.position;
    detail.innerHTML =
      `行号: <b>${data.line}</b>&nbsp;&nbsp;` +
      `指令: <code>${escapeHtml(data.line_text)}</code><br>` +
      `碰料位置(刀尖中心): X=${p.x} Y=${p.y} Z=${p.z} mm<br>` +
      `该处余料顶面深度: Z=${data.material_top} mm` +
      `<br>下图为碰料发生时的余料深度图。`;
  } else {
    banner.textContent = "⚠ 程序错误, 已停止";
    detail.innerHTML = `行号: <b>${data.line}</b><br>` +
      `原因: ${escapeHtml(data.message)}`;
  }

  if (data.depth_map) drawScene(data);
  $("resultPanel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// 深度 -> 颜色: 0 为浅灰(未切), 越深越蓝
function depthColor(depth, minDepth) {
  if (depth >= 0) return "#f2f2f2";
  const t = Math.min(1, depth / minDepth); // 0..1
  const light = 88 - 58 * t;               // 88% -> 30%
  return `hsl(209, 75%, ${light}%)`;
}

function drawScene(data) {
  const { width: W, length: L, thickness: T } = data.stock;
  const canvas = $("mapCanvas");
  const margin = { l: 56, r: 16, t: 16, b: 40 };
  const plotW = Math.min(860, Math.max(360, canvas.parentElement.clientWidth - 220));
  const scale = plotW / W;
  const plotH = L * scale;
  canvas.width = margin.l + plotW + margin.r;
  canvas.height = margin.t + plotH + margin.b;
  const ctx = canvas.getContext("2d");

  const toX = (x) => margin.l + x * scale;
  const toY = (y) => margin.t + plotH - y * scale;

  // 1) 余料深度图
  const { nx, ny, grid } = data.depth_map;
  const cw = plotW / nx, ch = plotH / ny;
  for (let j = 0; j < ny; j++) {
    for (let i = 0; i < nx; i++) {
      ctx.fillStyle = depthColor(grid[j][i], -T);
      ctx.fillRect(margin.l + i * cw, margin.t + plotH - (j + 1) * ch,
                   cw + 0.5, ch + 0.5);
    }
  }

  // 2) 毛坯边框与坐标轴
  ctx.strokeStyle = "#37474f";
  ctx.lineWidth = 1.2;
  ctx.strokeRect(margin.l, margin.t, plotW, plotH);
  ctx.fillStyle = "#37474f";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "center";
  const ticks = 5;
  for (let k = 0; k <= ticks; k++) {
    const x = (W * k) / ticks, y = (L * k) / ticks;
    ctx.fillText(x.toFixed(0), toX(x), margin.t + plotH + 14);
    ctx.textAlign = "right";
    ctx.fillText(y.toFixed(0), margin.l - 6, toY(y) + 4);
    ctx.textAlign = "center";
  }
  ctx.fillText("X / mm", margin.l + plotW / 2, margin.t + plotH + 30);
  ctx.save();
  ctx.translate(14, margin.t + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("Y / mm", 0, 0);
  ctx.restore();

  // 3) 刀具轨迹
  for (const mv of data.path || []) {
    ctx.beginPath();
    mv.pts.forEach((p, i) => {
      if (i === 0) ctx.moveTo(toX(p[0]), toY(p[1]));
      else ctx.lineTo(toX(p[0]), toY(p[1]));
    });
    if (mv.kind === "rapid") {
      ctx.strokeStyle = "#ef6c00";
      ctx.lineWidth = 1.2;
      ctx.setLineDash([5, 4]);
    } else {
      ctx.strokeStyle = "#1565c0";
      ctx.lineWidth = 1.6;
      ctx.setLineDash([]);
    }
    ctx.stroke();
  }
  ctx.setLineDash([]);

  // 4) 起点与碰料点
  ctx.fillStyle = "#2e7d32";
  ctx.beginPath();
  ctx.arc(toX(0), toY(0), 4, 0, 2 * Math.PI);
  ctx.fill();

  if (data.status === "collision" && data.position) {
    const { x, y } = data.position;
    ctx.strokeStyle = "#d50000";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(toX(x), toY(y), 7, 0, 2 * Math.PI);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(toX(x) - 10, toY(y) - 10);
    ctx.lineTo(toX(x) + 10, toY(y) + 10);
    ctx.moveTo(toX(x) + 10, toY(y) - 10);
    ctx.lineTo(toX(x) - 10, toY(y) + 10);
    ctx.stroke();
  }

  // 5) 图例色条
  const lg = $("legendCanvas").getContext("2d");
  const grad = lg.createLinearGradient(0, 0, 0, 180);
  grad.addColorStop(0, depthColor(-1e-9, -T));
  grad.addColorStop(1, depthColor(-T, -T));
  lg.clearRect(0, 0, 24, 180);
  lg.fillStyle = grad;
  lg.fillRect(0, 0, 24, 180);
  lg.strokeStyle = "#90a4ae";
  lg.strokeRect(0, 0, 24, 180);
  $("legendTop").textContent = "0";
  $("legendBottom").textContent = (-T).toFixed(1);
}
