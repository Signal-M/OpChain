# -*- coding: utf-8 -*-
"""生成 OpChain 的 Mock 模式演示动图 docs/demo.gif。

用真实引擎在 Mock 环境跑一遍 探索→合成→重放，把真实产生的
探索轨迹 / 合成链路 / 重放数据 渲染成 GIF（手机画面 + 右侧分阶段面板）。
仅开发期使用，依赖 pillow（pip install pillow）。

用法：
    python tools/make_demo_gif.py
"""
import os
import sys
import queue as _q

import PIL
from PIL import Image, ImageDraw, ImageFont

# ----------------------------------------------------------------------------
# 1) 用真实引擎取 Mock 模式数据
# ----------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.runtime import Bus, Control
from engine.devices import MockDevice
from engine.vision import MockVision
from engine.brain import MockBrain
from engine.agent import Agent, SYNTH_PATH

bus = Bus()
control = Control()
control.speed = 0.0
vision = MockVision()
device = MockDevice(total=6, vision=vision)
brain = MockBrain()
agent = Agent(device, vision, brain, bus, control)

trace = agent.explore("抓取列表中每个场地的名称、地址和订场链接")
chain = agent.synth_chain
assert chain, "合成失败"

device2 = MockDevice(total=6, vision=vision)
agent.device = device2
agent.vision = vision
captured = []
q = bus.subscribe()
agent.replay(SYNTH_PATH)
while True:
    try:
        ev = q.get_nowait()
    except _q.Empty:
        break
    if ev.get("type") == "data":
        captured.append(ev["record"])

records = [
    (r.get("venue", ""), r.get("address", ""), r.get("link", ""))
    for r in captured
]
if not records:
    records = [("(示例)灵动网球", "朝阳区东三环北路27号", "https://example.com/a"),
               ("(示例)方恒网球", "海淀区中关村南大街", "https://example.com/b")]

steps = [s["action"] for s in chain["steps"]]
trace_actions = [
    (h["action"].get("action", ""), h["action"].get("target", ""), h["action"].get("reason", ""))
    for h in trace
]

# ----------------------------------------------------------------------------
# 2) 字体
# ----------------------------------------------------------------------------
FONT_PATH = "/System/Library/Fonts/STHeiti Light.ttc"
try:
    def font(sz):
        return ImageFont.truetype(FONT_PATH, sz)
except Exception:
    def font(sz):
        return ImageFont.load_default()

F_TITLE = font(32)
F_H = font(20)
F_BODY = font(15)
F_SMALL = font(12)


def wrap(text, fnt, max_w):
    lines, cur = [], ""
    for ch in text:
        test = cur + ch
        if fnt.getlength(test) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


# ----------------------------------------------------------------------------
# 3) 画布与配色（浅色主题）
# ----------------------------------------------------------------------------
W, H = 800, 470
BG = (247, 248, 250)
PANEL = (255, 255, 255)
INK = (31, 35, 40)
SUB = (110, 119, 130)
ACCENT = (46, 158, 91)      # OpChain 绿
ACCENT2 = (47, 111, 235)    # 蓝
LINE = (225, 228, 232)
CARD = (240, 244, 241)


def rr(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def new_canvas():
    img = Image.new("RGB", (W, H), BG)
    return img, ImageDraw.Draw(img)


def draw_phone(d, highlight_idx=None, read_idx=None):
    px, py, pw, ph = 40, 40, 250, 390
    rr(d, (px, py, px + pw, py + ph), 22, fill=PANEL, outline=LINE, width=2)
    d.rounded_rectangle((px + pw // 2 - 34, py + 10, px + pw // 2 + 34, py + 22), radius=8, fill=LINE)
    d.text((px + 16, py + 30), "微信小程序", font=F_SMALL, fill=SUB)
    card_y, card_h, gap = py + 52, 64, 10
    names = ["场地卡片 A", "场地卡片 B", "场地卡片 C", "场地卡片 D"]
    for i, name in enumerate(names):
        cy = card_y + i * (card_h + gap)
        if cy + card_h > py + ph - 14:
            break
        fill, outline = CARD, LINE
        if read_idx is not None and i <= read_idx:
            fill, outline = (220, 240, 228), ACCENT
        elif highlight_idx == i:
            fill, outline = (224, 234, 252), ACCENT2
        rr(d, (px + 12, cy, px + pw - 12, cy + card_h), 10, fill=fill, outline=outline, width=2)
        d.text((px + 24, cy + 12), name, font=F_BODY, fill=INK)
        d.text((px + 24, cy + 36), "名称 / 地址 / 链接", font=F_SMALL, fill=SUB)


def draw_right(d, phase, arg=0):
    """右侧单个分阶段面板。phase: explore / synth / replay。"""
    rx, ry, rw, rh = 320, 40, W - 350, 390
    rr(d, (rx, ry, rx + rw, ry + rh), 14, fill=PANEL, outline=LINE, width=1)

    if phase == "explore":
        d.text((rx + 18, ry + 16), "① 探索（MockBrain 实时决策）", font=F_H, fill=ACCENT)
        y = ry + 58
        for i, (act, tgt, reason) in enumerate(trace_actions[:8]):
            done = i < arg
            mark = "✓" if done else ("●" if i == arg else "○")
            col = ACCENT if done else (ACCENT2 if i == arg else SUB)
            d.text((rx + 18, y), mark, font=F_BODY, fill=col)
            label = f"{act}" + (f" → {tgt}" if tgt else "")
            for ln in wrap(label, F_BODY, rw - 70):
                d.text((rx + 46, y), ln, font=F_BODY, fill=INK if done or i == arg else SUB)
                y += 22
            y += 6

    elif phase == "synth":
        d.text((rx + 18, ry + 16), "② 合成确定性链路", font=F_H, fill=ACCENT)
        d.text((rx + 18, ry + 44), "Agent 生成后可零模型调用重放", font=F_SMALL, fill=SUB)
        y = ry + 78
        for s in steps[:9]:
            d.text((rx + 26, y), f"• {s}", font=F_BODY, fill=INK)
            y += 30

    elif phase == "replay":
        d.text((rx + 18, ry + 16), "③ 重放产出数据（emit）", font=F_H, fill=ACCENT2)
        hy = ry + 50
        d.text((rx + 18, hy), "场地", font=F_SMALL, fill=SUB)
        d.text((rx + 210, hy), "地址", font=F_SMALL, fill=SUB)
        d.text((rx + 420, hy), "链接", font=F_SMALL, fill=SUB)
        d.line((rx + 14, hy + 18, rx + rw - 14, hy + 18), fill=LINE, width=1)
        ry2 = hy + 30
        for venue, addr, link in records[:arg]:
            d.text((rx + 18, ry2), wrap(venue, F_SMALL, 180)[0], font=F_SMALL, fill=INK)
            d.text((rx + 210, ry2), wrap(addr, F_SMALL, 200)[0], font=F_SMALL, fill=INK)
            d.text((rx + 420, ry2), wrap(link, F_SMALL, 120)[0], font=F_SMALL, fill=ACCENT2)
            ry2 += 30


# ----------------------------------------------------------------------------
# 4) 组装帧
# ----------------------------------------------------------------------------
frames = []


def frame(phone_kwargs, phase, arg=0, title=False):
    img, d = new_canvas()
    if title:
        d.text((W // 2, 196), "OpChain", font=F_TITLE, fill=ACCENT, anchor="mm")
        d.text((W // 2, 244), "本地多模态 GUI Agent 自动化引擎", font=F_H, fill=INK, anchor="mm")
        d.text((W // 2, 292), "探索 → 合成确定性链路 → 零模型调用重放", font=F_BODY, fill=SUB, anchor="mm")
    draw_phone(d, **phone_kwargs)
    if not title:
        draw_right(d, phase, arg)
    frames.append(img)


# F0 标题
frame({}, None, title=True)

# 探索阶段：逐条揭示轨迹
for upto in range(0, min(len(trace_actions), 6) + 1):
    frame({"highlight_idx": min(upto, 3)}, "explore", upto)

# 合成阶段
frame({"read_idx": 0}, "synth", 0)

# 重放阶段：逐行填充数据
for k in range(0, len(records) + 1):
    frame({"read_idx": min(k - 1, 3)}, "replay", k)

# 结尾
frame({"read_idx": 3}, "replay", len(records))

# ----------------------------------------------------------------------------
# 5) 保存
# ----------------------------------------------------------------------------
out = os.path.join(ROOT, "docs", "demo.gif")
os.makedirs(os.path.dirname(out), exist_ok=True)
frames[0].save(
    out, save_all=True, append_images=frames[1:],
    duration=700, loop=0, optimize=True,
)
print(f"✅ 已生成 {out}（{len(frames)} 帧，{os.path.getsize(out)//1024} KB）")
