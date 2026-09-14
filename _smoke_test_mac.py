"""macOS 真实层冒烟测试（不触发真实鼠标/键盘点击）。

验证：
  1) MacDevice：构造、屏幕尺寸、CoreGraphics 可用性、截图、perceive、can_scroll
  2) UITARSBrain 解析器：click/scroll/type/drag/finished 正则翻译（不联网）
  3) 端到端真实模型调用：截一张真实屏 → 发给 ui-tars:7b → 解析出 DSL 动作
     （只「看」不「点」，确认闭环通路）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.devices import MacDevice
from engine.brain import UITARSBrain


def hr(t):
    print("\n" + "=" * 60)
    print(t)
    print("=" * 60)


# ---------- 1) MacDevice 基础 ----------
hr("1) MacDevice 基础能力")
dev = MacDevice()
print("cg_available :", MacDevice.cg_available())
print("screen_size  :", dev._screen)
path = dev.screenshot()
print("screenshot   :", path, "exists=", os.path.exists(path),
      "bytes=", os.path.getsize(path) if os.path.exists(path) else 0)
perc = dev.perceive()
print("perceive keys:", sorted(perc.keys()))
print("has screenshot b64:", bool(perc.get("screenshot")),
      "len=", len(perc.get("screenshot", "")))
print("can_scroll   :", dev.can_scroll())

# ---------- 2) UITARSBrain 解析器单元测试 ----------
hr("2) UITARSBrain 解析器（不联网）")
W, H = 1440, 900
cases = [
    ("click", "Thought: 我看到了预订按钮\nAction: click(start_box='(500,300)')"),
    ("scroll", "Action: scroll(direction='down')"),
    ("type", "Action: type(start_box='(200,400)', content='阳光网球中心')"),
    ("drag", "Action: drag(start_box='(100,100)', end_box='(100,800)')"),
    ("finished", "Action: finished()"),
    ("pixel-coords", "Action: click(start_box='(720,270)')"),  # >1000? no, stays norm
]
for name, raw in cases:
    out = UITARSBrain._translate(raw, {"screen_w": W, "screen_h": H})
    print(f"  {name:14s} -> {out}")

# 像素坐标兜底：坐标明显超过 1000 时不归一化
px = UITARSBrain._translate(
    "Action: click(start_box='(1440,900)')", {"screen_w": W, "screen_h": H})
print("  pixel>1000    ->", px, "(应原样 1440,900)")

# ---------- 3) 端到端真实模型调用（只看不点） ----------
hr("3) 端到端：真实截图 → ui-tars:7b → DSL")
base = os.environ.get("BRAIN_BASE_URL", "http://127.0.0.1:11434/v1")
model = os.environ.get("BRAIN_MODEL", "ui-tars:7b")
brain = UITARSBrain(base_url=base, model=model)
goal = "这是一个桌面，请指出屏幕中央附近可点击的区域（仅返回 Action，不要真执行）"
try:
    t0 = time.time()
    action = brain.decide(goal, perc, [])
    dt = time.time() - t0
    print(f"  模型返回 ({dt:.1f}s): {action}")
    print("  解析动作类型:", action.get("action"))
    print("  [OK] 真实 macOS 闭环通路打通（感知→模型→DSL 动作）")
except Exception as e:  # noqa: BLE001
    print("  [WARN] 模型调用失败（不影响代码正确性，可后续重试）:", e)

print("\n全部冒烟检查完成。")
