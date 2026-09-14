"""v0.2 冒烟测试：探索→合成→重放（Mock 环境，零依赖）。"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.runtime import Bus, Control
from engine.devices import MockDevice
from engine.vision import MockVision
from engine.brain import MockBrain
from engine.agent import Agent, SYNTH_PATH

bus = Bus()
control = Control()
control.speed = 0.0  # 测试不等待
vision = MockVision()
device = MockDevice(total=6, vision=vision)
brain = MockBrain()
agent = Agent(device, vision, brain, bus, control)

print("=== 1) 探索态（每步由 MockBrain 实时决策）===")
trace = agent.explore("抓取列表中每个场地的名称、地址和订场链接")
print(f"探索轨迹步数: {len(trace)}")
for i, h in enumerate(trace):
    a = h["action"]
    print(f"  {i+1}. {a.get('action')} -> target={a.get('target')} | reason={a.get('reason','')[:30]}")

print("\n=== 2) 合成确定性链路 ===")
chain = agent.synth_chain
assert chain, "合成失败"
print("链路名:", chain["name"])
print("顶级步骤:", [s["action"] for s in chain["steps"]])
loop = next(s for s in chain["steps"] if s["action"] == "loop")
print("循环体步数:", len(loop["body"]))
print("循环体内动作:", [b["action"] for b in loop["body"]])
assert os.path.exists(SYNTH_PATH), "agent_synth.json 未写出"
with open(SYNTH_PATH, encoding="utf-8") as f:
    raw = f.read()
assert raw.strip(), "agent_synth.json 写入为空！"
print("写入文件:", SYNTH_PATH, f"（{len(raw)} 字节）")

print("\n=== 3) 执行态重放（原 Interpreter，0 模型调用）===")
# 重放前重置设备，模拟 app.do_replay 行为
device = MockDevice(total=6, vision=vision)
agent.device = device
agent.vision = vision
# 捕获 data 事件
captured = []
q = bus.subscribe()
control.mode = "idle"
agent.replay(SYNTH_PATH)
# 读取 bus 中 replay 产生的 data 事件
import queue as _q
while True:
    try:
        ev = q.get_nowait()
    except _q.Empty:
        break
    if ev.get("type") == "data":
        captured.append(ev["record"])
print(f"重放产出数据条数: {len(captured)}")
for r in captured:
    print("  ", r.get("venue"), "|", r.get("address"), "|", r.get("link"))

assert len(captured) >= 1, "重放未产出数据"
print("\n✅ v0.2 冒烟测试通过：探索→合成→重放 全链路在 Mock 环境跑通。")
