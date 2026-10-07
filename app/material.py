"""余料模型与快移碰料检测。

毛坯为长方体: XY 范围 [0, W] x [0, L], 顶面 Z=0, 底面 Z=-T。
刀具为平底立铣刀, 半径 r。

余料用一组互不重迭(仅边界相接)的区域表示, 每个区域记录材料顶面深度
(depth <= 0)。未切削区域 depth = 0, 不显式存储, 需要时由毛坯矩形减去
已切区域得到。

碰料语义:
  - 切削去除的是刀具扫过的闭区域, 区域边界视为已被加工(属于空腔一侧),
    因此刀具可以贴着此前自己切出的空腔壁/底快移(零间隙配合不算碰料);
  - 刀具压入材料内部(材料顶面高于刀尖)必然碰料;
  - 与"未加工表面"(depth == 0)相切也算碰料(切线接触算碰料)。
"""
from shapely.geometry import Point, LineString, box, MultiPolygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

# 数值容差
EPS_Z = 1e-9        # Z 向比较容差 (mm)

# 区域数量超过该阈值时, 按深度合并以控制计算量
MERGE_THRESHOLD = 400

# 刀盘/扫掠域缓冲的圆弧离散精度
QUAD_SEGS = 16


def _poly_parts(geom):
    """展开为 Polygon 部件列表(忽略退化的线/点)。"""
    gt = geom.geom_type
    if gt == "Polygon":
        return [geom]
    if gt in ("MultiPolygon", "GeometryCollection"):
        out = []
        for g in geom.geoms:
            out.extend(_poly_parts(g))
        return out
    return []


def _rebuild(parts):
    """由部件列表重建几何(部件互不重迭, 直接组装)。"""
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return MultiPolygon(parts)


class Stock:
    """毛坯余料模型。"""

    def __init__(self, width, length, thickness, tool_radius):
        self.width = float(width)
        self.length = float(length)
        self.thickness = float(thickness)
        self.r = float(tool_radius)
        # 边界归属容差: 已切区域(含更深区域)向外吸收该宽度的边界带,
        # 使离散近似产生的细刺/边界归属于空腔一侧, 避免误判。
        # 取值覆盖缓冲多边形近似误差(~0.0012r)与圆弧离散弦差(0.01mm)。
        self.eps_shrink = 0.02 + 0.003 * self.r
        self.rect = box(0.0, 0.0, self.width, self.length)
        # regions: list of [geometry, depth], depth 为该区域材料顶面 Z (<=0)
        self.regions = []
        self._tree = None         # 区域空间索引(惰性重建)
        # 未切区域 = 毛坯减去已切并集(已切并集外扩 eps_shrink, 使边界归空腔侧)。
        # 覆盖域只增不减, 因此增量维护; 切削先登记到待扣列表,
        # 快移检查前再一次性批量扣除, 避免频繁对复杂几何求差。
        self._uncut_eff = self.rect
        self._pending_uncut = []

    def _candidate_indices(self, geom):
        """返回与 geom 相交的区域下标(STRtree 加速)。"""
        if not self.regions:
            return []
        if self._tree is None:
            self._tree = STRtree([g for g, _ in self.regions])
        return self._tree.query(geom, predicate="intersects").tolist()

    # ------------------------------------------------------------------ #
    # 切削: 把刀具扫过的多边形按给定深度从余料中扣除
    # ------------------------------------------------------------------ #
    def add_cut(self, poly, depth):
        """从余料中扣除多边形 poly 内、顶面深度为 depth 的材料。"""
        if depth >= 0.0:
            return  # 刀尖不在材料内部, 不去除材料
        poly = poly.intersection(self.rect)
        if poly.is_empty:
            return
        self._pending_uncut.append(poly)
        idx = self._candidate_indices(poly)
        if not idx:
            self.regions.append([poly, depth])
            self._invalidate_cache()
            return
        cand_set = set(idx)
        new_regions = [r for i, r in enumerate(self.regions)
                       if i not in cand_set]
        splits = []
        for i in idx:
            geom, d = self.regions[i]
            if d <= depth:
                # 已有区域切得更深(或相同), 本次切削不影响它
                new_regions.append([geom, d])
                continue
            # 部件级局部化: 只有与 poly 相交的子多边形参与布尔运算,
            # 避免对大型合并区域整体求交/求差
            rem_parts, inter_parts = [], []
            for p in _poly_parts(geom):
                if not p.intersects(poly):
                    rem_parts.append(p)
                    continue
                inter = p.intersection(poly)
                if inter.is_empty:
                    rem_parts.append(p)
                    continue
                inter_parts.extend(_poly_parts(inter))
                rem_parts.extend(_poly_parts(p.difference(poly)))
            splits.append((rem_parts, d, inter_parts))
        for rem_parts, d, inter_parts in splits:
            g = _rebuild(rem_parts)
            if g is not None:
                new_regions.append([g, d])
            g = _rebuild(inter_parts)
            if g is not None:
                new_regions.append([g, depth])
        # poly 中未被任何已有区域覆盖的部分
        leftover = poly
        for i in idx:
            if leftover.is_empty:
                break
            leftover = leftover.difference(self.regions[i][0])
        if not leftover.is_empty:
            new_regions.append([leftover, depth])
        self.regions = new_regions
        self._invalidate_cache()
        if len(self.regions) > MERGE_THRESHOLD:
            self._merge_same_depth()

    def add_cut_path(self, pts):
        """沿折线 pts=[(x,y,z),...] 切削, 每一小段按该段最低刀尖深度扣除。

        坡铣/螺旋插补时 Z 沿路径变化, 折线各小段分别记录深度,
        因此螺旋槽各处的切深被分别保留。
        """
        r = self.r
        for a, b in zip(pts, pts[1:]):
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            if dx * dx + dy * dy < 1e-18:
                if abs(b[2] - a[2]) < 1e-12:
                    continue  # 零长度段
                seg = Point(a[0], a[1])
            else:
                seg = LineString([(a[0], a[1]), (b[0], b[1])])
            poly = seg.buffer(r, quad_segs=QUAD_SEGS)
            self.add_cut(poly, min(a[2], b[2]))

    def _merge_same_depth(self):
        groups = {}
        for geom, d in self.regions:
            key = round(d, 9)
            groups.setdefault(key, []).append(geom)
        self.regions = [[unary_union(gs), d] for d, gs in groups.items()]

    def _invalidate_cache(self):
        self._tree = None

    def _flush_uncut(self):
        """把待扣的切削扫掠域批量从未切区域中扣除。"""
        if self._pending_uncut:
            cut = unary_union([p.buffer(self.eps_shrink, quad_segs=4)
                               for p in self._pending_uncut])
            self._uncut_eff = self._uncut_eff.difference(cut)
            self._pending_uncut = []

    # ------------------------------------------------------------------ #
    # 快移碰料检测
    # ------------------------------------------------------------------ #
    def check_rapid(self, p0, p1):
        """检查 G0 快移段 p0->p1 (三维) 是否碰到当前余料。

        返回 None 表示安全; 否则返回 dict(t, x, y, z, depth):
        首次接触时刀具中心位置 (x,y,z) 及该处材料顶面深度 depth。
        """
        z0, z1 = p0[2], p1[2]
        zlow = min(z0, z1)
        if zlow > EPS_Z:
            return None  # 全程在毛坯顶面之上, 不可能碰料

        dx = p1[0] - p0[0]
        dy = p1[1] - p0[1]
        len2 = dx * dx + dy * dy
        vertical = len2 < 1e-18
        if vertical:
            capsule = Point(p0[0], p0[1]).buffer(self.r, quad_segs=QUAD_SEGS)
        else:
            capsule = LineString([(p0[0], p0[1]), (p1[0], p1[1])]).buffer(
                self.r, quad_segs=QUAD_SEGS)

        best = None  # (t, x, y, z, depth)

        def consider(ta, tb, depth, is_uncut):
            nonlocal best
            za = z0 + (z1 - z0) * ta
            zb = z0 + (z1 - z0) * tb
            if is_uncut:
                hit = za <= EPS_Z or zb <= EPS_Z
                level = 0.0
            else:
                hit = za < depth - EPS_Z or zb < depth - EPS_Z
                level = depth
            if not hit:
                return
            # 求该区间内首次满足接触条件的 t
            starts_hit = za <= EPS_Z if is_uncut else za < depth - EPS_Z
            if starts_hit:
                t = ta
            elif abs(z1 - z0) > EPS_Z:
                t = (level - z0) / (z1 - z0)
                t = min(max(t, ta), tb)
            else:
                return
            if best is None or t < best[0]:
                best = (t, p0[0] + dx * t, p0[1] + dy * t,
                        z0 + (z1 - z0) * t, depth)

        # 1) 已切区域: 刀尖低于该区域材料顶面(严格)才算碰料;
        #    检查前先把切得更深的区域(含边界)从该区域中剔除,
        #    使边界归属于更深的区域, 避免台阶交界处的误判。
        deeper_cache = {}
        for i in self._candidate_indices(capsule):
            geom, d = self.regions[i]
            if zlow >= d - EPS_Z:
                continue  # 快移全程在该区域材料顶面之上(含相切)
            key = round(d, 9)
            if key not in deeper_cache:
                deeper = [g for g, dd in self.regions if dd < d - EPS_Z]
                deeper_cache[key] = (
                    unary_union(deeper).buffer(self.eps_shrink) if deeper else None)
            eff = geom
            du = deeper_cache[key]
            if du is not None:
                eff = geom.difference(du)
                if eff.is_empty:
                    continue
            inter = eff.intersection(capsule)
            if inter.is_empty:
                continue
            for ta, tb in self._t_intervals(inter, p0, dx, dy, len2, vertical):
                consider(ta, tb, d, False)

        # 2) 未切区域(材料顶面 Z=0): 刀尖达到或低于 0 即碰料(相切也算)
        self._flush_uncut()
        uncut = self._uncut_eff
        if uncut is not None and not uncut.is_empty:
            inter = uncut.intersection(capsule)
            if not inter.is_empty:
                for ta, tb in self._t_intervals(inter, p0, dx, dy, len2,
                                                vertical):
                    consider(ta, tb, 0.0, True)

        if best is None:
            return None
        return {"t": best[0], "x": best[1], "y": best[2], "z": best[3],
                "depth": best[4]}

    @staticmethod
    def _t_intervals(geom, p0, dx, dy, len2, vertical):
        """把(区域 ∩ 刀盘扫掠域)几何映射回路径参数 t 的区间列表。"""
        if vertical:
            return [(0.0, 1.0)]
        inv = 1.0 / len2

        def proj(c):
            t = ((c[0] - p0[0]) * dx + (c[1] - p0[1]) * dy) * inv
            return 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)

        out = []

        def visit(g):
            gt = g.geom_type
            if gt == "Point":
                t = proj((g.x, g.y))
                out.append((t, t))
            elif gt in ("LineString", "LinearRing"):
                ts = [proj(c) for c in g.coords]
                out.append((min(ts), max(ts)))
            elif gt == "Polygon":
                ts = [proj(c) for c in g.exterior.coords]
                out.append((min(ts), max(ts)))
            elif hasattr(g, "geoms"):
                for s in g.geoms:
                    visit(s)

        visit(geom)
        return out

    # ------------------------------------------------------------------ #
    # 余料深度图(用于前端显示)
    # ------------------------------------------------------------------ #
    def depth_grid(self, nx, ny):
        """返回 ny 行 nx 列的材料顶面深度栅格(0 表示未切削)。"""
        import numpy as np
        import shapely

        xs = (np.arange(nx) + 0.5) * (self.width / nx)
        ys = (np.arange(ny) + 0.5) * (self.length / ny)
        X, Y = np.meshgrid(xs, ys)
        depth = np.zeros((ny, nx))
        for geom, d in self.regions:
            mask = shapely.intersects_xy(geom, X, Y)
            np.minimum(depth, d, out=depth, where=mask)
        return depth.tolist()
