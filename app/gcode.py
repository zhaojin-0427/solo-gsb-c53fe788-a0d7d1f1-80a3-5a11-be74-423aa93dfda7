"""G-code 解析与模态回放。

支持: G0/G1/G2/G3, G17, G20/G21, G90/G91; F/S/N 字被接受但忽略。
默认模态: G17 / G21 / G90, 初始运动模态 G0, 起点 (0, 0, 安全高度)。
I/J 圆心偏移始终相对于圆弧段起点; G2/G3 不指定 X/Y 终点时为整圆,
Z 沿圆弧转角线性变化(螺旋插补)。
"""
import math
import re

WORD_RE = re.compile(
    r"([A-Za-z])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)")

ALLOWED_PARAMS = set("XYZIJFSN")

ARC_CHORD_TOL = 0.01    # 圆弧离散弦高误差 (mm)
RADIUS_TOL_ABS = 0.005  # 圆弧半径允差 (mm)
RADIUS_TOL_REL = 1e-3   # 圆弧半径相对允差
EPS = 1e-9

# 切削段细分: 坡铣时每段长度/落差上限, 保证各小段分别记录切深
RAMP_MAX_LEN = 2.0
RAMP_MAX_DZ = 0.5
RAMP_MAX_SEGS = 500

# 显示用轨迹点总数上限, 超出后只记录段端点
MAX_PATH_POINTS = 40000


class GCodeError(Exception):
    """带原行号的 G-code 错误(非法圆弧/不支持指令/切削穿底等)。"""

    def __init__(self, line_no, message):
        self.line_no = line_no
        self.message = message
        super().__init__(f"第 {line_no} 行: {message}")


def strip_comments(line):
    """去除 (...) 注释和 ; 到行尾的注释。"""
    out = []
    depth = 0
    for c in line:
        if c == "(":
            depth += 1
        elif c == ")":
            if depth:
                depth -= 1
        elif c == ";" and depth == 0:
            break
        elif depth == 0:
            out.append(c)
    return "".join(out)


def parse_line(line, line_no):
    """解析一行, 返回 [(letter, value), ...]; 空行/纯注释/% 返回 []。"""
    text = strip_comments(line).strip()
    if not text or text == "%":
        return []
    words = []
    pos = 0
    for m in WORD_RE.finditer(text):
        gap = text[pos:m.start()].strip()
        if gap:
            raise GCodeError(line_no, f"无法解析的内容: {gap!r}")
        words.append((m.group(1).upper(), float(m.group(2))))
        pos = m.end()
    tail = text[pos:].strip()
    if tail:
        raise GCodeError(line_no, f"无法解析的内容: {tail!r}")
    if not words:
        raise GCodeError(line_no, f"无法解析的行: {text!r}")
    return words


class MachineState:
    def __init__(self, safe_height):
        self.pos = (0.0, 0.0, float(safe_height))  # 起点 (0,0,安全高度)
        self.units = 1.0       # G21 毫米; G20 英寸 -> x25.4
        self.absolute = True   # G90
        self.motion = 0        # 初始运动模态 G0


def arc_points(x0, y0, z0, x1, y1, z1, cx, cy, cw, line_no):
    """生成圆弧(可整圆、可螺旋)离散折线, 末点精确落在 (x1,y1,z1)。"""
    r0 = math.hypot(x0 - cx, y0 - cy)
    if r0 < EPS:
        raise GCodeError(line_no, "非法圆弧: 圆心与起点重合(I/J 全为 0)")
    full = math.hypot(x1 - x0, y1 - y0) < 1e-6
    if not full:
        r1 = math.hypot(x1 - cx, y1 - cy)
        if abs(r1 - r0) > max(RADIUS_TOL_ABS, RADIUS_TOL_REL * r0):
            raise GCodeError(
                line_no,
                f"非法圆弧: 终点不在圆弧上(起点半径 {r0:.4f}, "
                f"终点半径 {r1:.4f})")
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    if cw:
        sweep = (a0 - a1) % (2.0 * math.pi)
    else:
        sweep = (a1 - a0) % (2.0 * math.pi)
    if full or sweep < 1e-12:
        sweep = 2.0 * math.pi
    if r0 > ARC_CHORD_TOL:
        step = 2.0 * math.acos(max(-1.0, 1.0 - ARC_CHORD_TOL / r0))
    else:
        step = math.pi
    n = max(1, min(2000, math.ceil(sweep / step)))
    pts = []
    for k in range(n + 1):
        t = k / n
        a = a0 - sweep * t if cw else a0 + sweep * t
        pts.append((cx + r0 * math.cos(a), cy + r0 * math.sin(a),
                    z0 + (z1 - z0) * t))
    pts[-1] = (x1, y1, z1)
    return pts


def simulate(gcode_text, stock, safe_height):
    """按模态逐行回放, 切削扣除余料, 快移做碰料检查。

    返回 {"moves": [...], "collision": None|dict, "truncated": bool};
    遇到非法圆弧/不支持指令/切削穿底时抛出 GCodeError(含行号)。
    """
    st = MachineState(safe_height)
    moves = []
    truncated = False
    point_count = 0

    def record(kind, pts, line_no):
        nonlocal truncated, point_count
        if point_count > MAX_PATH_POINTS:
            truncated = True
            pts = [pts[0], pts[-1]]
        point_count += len(pts)
        moves.append({"kind": kind,
                      "pts": [[round(p[0], 4), round(p[1], 4), round(p[2], 4)]
                              for p in pts],
                      "line": line_no})

    for line_no, raw in enumerate(gcode_text.splitlines(), 1):
        words = parse_line(raw, line_no)
        if not words:
            continue
        g_codes = []
        params = {}
        for letter, val in words:
            if letter == "G":
                g = int(round(val))
                if abs(val - g) > EPS:
                    raise GCodeError(line_no, f"非法 G 代码: G{val:g}")
                g_codes.append(g)
            elif letter in ALLOWED_PARAMS:
                params[letter] = val
            else:
                raise GCodeError(line_no, f"不支持的指令: {letter}{val:g}")

        explicit_motion = None
        for g in g_codes:
            if g in (0, 1, 2, 3):
                explicit_motion = g
                st.motion = g
            elif g == 17:
                pass
            elif g == 20:
                st.units = 25.4
            elif g == 21:
                st.units = 1.0
            elif g == 90:
                st.absolute = True
            elif g == 91:
                st.absolute = False
            else:
                raise GCodeError(line_no, f"不支持的 G 代码: G{g}")

        has_axis = any(k in params for k in ("X", "Y", "Z"))
        has_ij = "I" in params or "J" in params
        if explicit_motion is None and not has_axis \
                and not (has_ij and st.motion in (2, 3)):
            continue  # 纯模态设置行, 无运动

        motion = st.motion
        u = st.units
        x0, y0, z0 = st.pos
        tgt = [x0, y0, z0]
        for i, ax in enumerate("XYZ"):
            if ax in params:
                v = params[ax] * u
                tgt[i] = v if st.absolute else st.pos[i] + v
        x1, y1, z1 = tgt

        if motion in (0, 1):
            length = math.hypot(x1 - x0, y1 - y0)
            dz = z1 - z0
            if length < EPS and abs(dz) < EPS:
                st.pos = (x1, y1, z1)
                continue  # 零位移, 无检查无切削
            n = 1
            if motion == 1 and abs(dz) > EPS:
                # 坡铣: 细分使各小段分别记录切深
                n = max(1, math.ceil(length / RAMP_MAX_LEN),
                        math.ceil(abs(dz) / RAMP_MAX_DZ))
                n = min(n, RAMP_MAX_SEGS)
            pts = [(x0 + (x1 - x0) * k / n,
                    y0 + (y1 - y0) * k / n,
                    z0 + dz * k / n) for k in range(n + 1)]
        else:
            i_off = params.get("I", 0.0) * u
            j_off = params.get("J", 0.0) * u
            if abs(i_off) < EPS and abs(j_off) < EPS:
                raise GCodeError(
                    line_no, "非法圆弧: 缺少圆心偏移 I/J(不支持 R 编程)")
            pts = arc_points(x0, y0, z0, x1, y1, z1,
                             x0 + i_off, y0 + j_off,
                             motion == 2, line_no)

        if motion == 0:
            hit = stock.check_rapid((x0, y0, z0), (x1, y1, z1))
            record("rapid", pts, line_no)
            if hit is not None:
                hit["line"] = line_no
                hit["line_text"] = raw.strip()
                return {"moves": moves, "collision": hit,
                        "truncated": truncated}
        else:
            zmin = min(p[2] for p in pts)
            if zmin < -stock.thickness - 1e-9:
                raise GCodeError(
                    line_no,
                    f"切削穿底: 刀尖 Z={zmin:.4f} 低于毛坯底面 "
                    f"Z=-{stock.thickness:g}")
            stock.add_cut_path(pts)
            record("cut", pts, line_no)
        st.pos = (x1, y1, z1)

    return {"moves": moves, "collision": None, "truncated": truncated}
