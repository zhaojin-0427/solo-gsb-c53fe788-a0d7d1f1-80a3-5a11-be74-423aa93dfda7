"""FastAPI 入口: 提供碰料检查 API 与前端静态页面。"""
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.gcode import GCodeError, simulate
from app.material import Stock

app = FastAPI(title="数控铣削快移碰料检查")

# 深度图栅格分辨率
GRID_NX = 220
GRID_MAX_NY = 400


class CheckRequest(BaseModel):
    gcode: str
    stock_width: float = Field(gt=0, description="毛坯宽度 X (mm)")
    stock_length: float = Field(gt=0, description="毛坯长度 Y (mm)")
    stock_thickness: float = Field(gt=0, description="毛坯厚度 (mm)")
    tool_diameter: float = Field(gt=0, description="平底刀直径 (mm)")
    safe_height: float = Field(gt=0, description="安全高度 (mm, 正数)")


@app.post("/api/check")
def check(req: CheckRequest):
    stock = Stock(req.stock_width, req.stock_length, req.stock_thickness,
                  req.tool_diameter / 2.0)
    base = {
        "stock": {
            "width": req.stock_width,
            "length": req.stock_length,
            "thickness": req.stock_thickness,
        },
        "tool_diameter": req.tool_diameter,
        "safe_height": req.safe_height,
    }
    try:
        result = simulate(req.gcode, stock, req.safe_height)
    except GCodeError as e:
        return {**base, "status": "error", "line": e.line_no,
                "message": e.message}

    nx = GRID_NX
    ny = max(1, min(GRID_MAX_NY, round(nx * req.stock_length
                                       / req.stock_width)))
    resp = {
        **base,
        "path": result["moves"],
        "path_truncated": result["truncated"],
        "depth_map": {"nx": nx, "ny": ny, "grid": stock.depth_grid(nx, ny)},
    }
    hit = result["collision"]
    if hit is not None:
        resp.update({
            "status": "collision",
            "line": hit["line"],
            "line_text": hit["line_text"],
            "position": {"x": round(hit["x"], 4), "y": round(hit["y"], 4),
                         "z": round(hit["z"], 4)},
            "material_top": round(hit["depth"], 4),
        })
    else:
        resp["status"] = "safe"
    return resp


static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
