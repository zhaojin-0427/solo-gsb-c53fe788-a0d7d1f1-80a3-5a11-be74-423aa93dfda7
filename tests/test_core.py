"""核心逻辑测试: 直接调用 simulate/Stock, 覆盖需求各条款。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.gcode import simulate, GCodeError   # noqa: E402
from app.material import Stock               # noqa: E402

W, L, T = 100.0, 80.0, 20.0
TOOL_R = 3.0
SAFE = 5.0


def run(gcode):
    stock = Stock(W, L, T, TOOL_R)
    return simulate(gcode, stock, SAFE), stock


def expect_error(gcode, needle):
    try:
        run(gcode)
    except GCodeError as e:
        assert needle in e.message, f"错误信息不符: {e.message}"
        return e
    raise AssertionError(f"应抛出错误({needle})但未抛出")


# 1. 安全: 切槽后在槽内退刀、安全高度快移
res, _ = run("""\
G21 G90 G17
G0 X10 Y10
G1 Z-3 F200
G1 X60 Y10
G1 X60 Y40
G1 X10 Y40
G1 X10 Y10
G0 Z5
G0 X0 Y0
""")
assert res["collision"] is None, "应为安全"

# 2. 碰料: 快移穿入未切余料, 报告行号与位置
res, _ = run("""\
G21 G90 G17
G0 X10 Y10
G1 Z-2
G1 X40 Y10
G0 Z-1
G0 X90 Y10
""")
hit = res["collision"]
assert hit is not None and hit["line"] == 6, hit
assert abs(hit["z"] - (-1.0)) < 1e-6
# 刀盘与槽等宽, 越过槽端(40)立刻扫到槽口外余料
assert 40.0 < hit["x"] < 44.0, hit["x"]
assert hit["depth"] == 0.0

# 3. 切线接触算碰料: 快移降到 Z=0 与未切毛坯顶面相切
res, _ = run("G0 Z0\nG0 X50 Y40\n")
hit = res["collision"]
assert hit is not None and hit["line"] == 1, "Z=0 擦顶面应判碰料"

# 4. 快移可利用已切空腔: 槽底 -3, 快移 Z=-2 通过安全; Z=-3.5 碰料
res, _ = run("""\
G0 X10 Y40
G1 Z-3
G1 X90 Y40
G0 Z-2
G0 X10 Y40
""")
assert res["collision"] is None, "Z=-2 在 -3 槽内应安全"

res, _ = run("""\
G0 X10 Y40
G1 Z-3
G1 X90 Y40
G0 Z-3.5
G0 X10 Y40
""")
hit = res["collision"]
assert hit is not None and hit["line"] == 4, "下降穿过槽底 -3 即应碰料"
assert abs(hit["z"] - (-3.0)) < 1e-6

# 5. 贴着已切槽底 (Z=-3 == 槽深) 不算碰料(零间隙利用空腔)
res, _ = run("""\
G0 X10 Y40
G1 Z-3
G1 X90 Y40
G0 Z-3
G0 X10 Y40
""")
assert res["collision"] is None, "贴已切槽底应安全"

# 6. 斜坡槽: 各处切深分别保留, 快移在浅端碰料
res, _ = run("""\
G0 X10 Y40
G1 Z-1
G1 X90 Y40 Z-4
G0 Z-2.5
G0 X10 Y40
""")
hit = res["collision"]
assert hit is not None and hit["line"] == 5
# 槽深 = -1 -3*(x-10)/80, 深 -2.5 处在 x=50
assert 45.0 < hit["x"] < 55.0, hit["x"]

# 7. 螺旋槽: G2 整圆 + Z 线性变化, 加工安全
res, _ = run("""\
G0 X50 Y40
G1 Z0
G2 X50 Y40 I-8 J0 Z-2
G2 X50 Y40 I-8 J0 Z-4
G0 Z5
""")
assert res["collision"] is None, "螺旋槽加工后原路退刀应安全"

# 8. 螺旋槽浅端保护: 槽深沿转角 -2..-4 变化, 在 CW 90° 处(深约 -2.5)下降应碰料
res, _ = run("""\
G0 X50 Y40
G1 Z0
G2 X50 Y40 I-8 J0 Z-2
G2 X50 Y40 I-8 J0 Z-4
G0 Z5
G0 X42 Y32
G0 Z-3
""")
hit = res["collision"]
assert hit is not None and hit["line"] == 7, hit
assert -2.8 < hit["depth"] < -2.3, hit["depth"]

# 9. 非法圆弧: 终点不在圆上
e = expect_error("G0 X10 Y10\nG1 Z-2\nG2 X30 Y25 I10 J0\n", "非法圆弧")
assert e.line_no == 3

# 10. 非法圆弧: 缺 I/J
expect_error("G0 X10 Y10\nG2 X20 Y10\n", "非法圆弧")

# 11. 不支持的指令
e = expect_error("G0 X10 Y10\nG28 X0\n", "不支持的 G 代码")
assert e.line_no == 2
expect_error("G0 X10 Y10\nM03 S1000\n", "不支持的指令")

# 12. 切削穿底
e = expect_error("G0 X10 Y10\nG1 Z-25\n", "切削穿底")
assert e.line_no == 2

# 13. G20 英寸 + G91 增量
res, stock = run("""\
G20 G90
G0 X0.3937 Y1.5748
G1 Z-0.07874
G91
G1 X1.5748
G90 G21
G0 Z5
""")
assert res["collision"] is None
# 0.3937in=10mm, 1.5748in=40mm, Z=-0.07874in=-2mm, 增量 1.5748in=40mm
# 槽应从 (10,40) 切到 (50,40), 深 -2
from shapely.geometry import Point
assert any(abs(d + 2.0) < 0.01 and g.covers(Point(50, 40))
           for g, d in stock.regions), "英寸/增量换算错误"
assert any(abs(d + 2.0) < 0.01 and g.covers(Point(10, 40))
           for g, d in stock.regions), "英寸/增量换算错误"

# 14. I/J 始终相对段起点(G91 下也一样)
res, _ = run("""\
G91
G0 X20 Y20
G1 Z-2
G2 X0 Y0 I5 J0
G90
G0 Z5
""")
assert res["collision"] is None

# 15. 模态运动: 无 G 字行沿用上一运动模态
res, _ = run("""\
G0 X10 Y10
G1 Z-2
X50
G0 Z5
""")
assert res["collision"] is None

# 16. 穿底边界: 恰好切到底面 Z=-T 允许
res, _ = run("G0 X10 Y10\nG1 Z-20\nG0 Z5\n")
assert res["collision"] is None

# 17. 注释/空行/%/N 字
res, _ = run("""\
%
(程序注释)
N10 G21 G90 ; 行尾注释
N20 G0 X10 Y10
N30 G1 Z-2 F100
N40 G0 Z5
%
""")
assert res["collision"] is None

# 18. 台阶交界处快移: 深区 -5 浅区 -2, 快移 Z=-3 只在深区安全
res, _ = run("""\
G0 X30 Y40
G1 Z-5
G1 X40 Y40
G0 Z5
G0 X60 Y40
G1 Z-2
G1 X70 Y40
G0 Z5
G0 X35 Y40
G0 Z-3
G0 Z5
""")
assert res["collision"] is None, "Z=-3 在深槽(-5)上方应安全"

res, _ = run("""\
G0 X30 Y40
G1 Z-5
G1 X40 Y40
G0 Z5
G0 X60 Y40
G1 Z-2
G1 X70 Y40
G0 Z5
G0 X65 Y40
G0 Z-3
""")
hit = res["collision"]
assert hit is not None and hit["line"] == 10, "Z=-3 进入浅槽(-2)应碰料"
assert abs(hit["depth"] - (-2.0)) < 1e-6

print("全部测试通过 ✔")
