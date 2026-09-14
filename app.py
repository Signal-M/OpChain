"""操作链路自动化引擎 · 后端（Python 标准库，零第三方依赖）。

提供：
  GET  /                 静态界面
  GET  /chains           可用链路列表
  GET  /state            当前状态快照
  GET  /data             已抽取数据
  GET  /stream           SSE 事件流（步骤/日志/页面/数据/进度/状态）
  POST /control          {action: start|pause|step|stop|reset|save_chain|delete_chain|..., speed?}
"""
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---- 日志：同时写文件 + 控制台（文件路径与启动脚本一致）----
_LOG_FILE = "/tmp/opchain_ui.log"
import logging as _logging
_logging.basicConfig(
    level=_logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        _logging.FileHandler(_LOG_FILE, mode="a", encoding="utf-8"),
        _logging.StreamHandler(sys.stdout),
    ],
)
# 让 print() 也写入日志文件（tee 模式）
class _TeeStream:
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            s.write(data)
            try:
                s.flush()
            except Exception:
                pass
    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass
_log_fh = open(_LOG_FILE, "a", encoding="utf-8")
sys.stdout = _TeeStream(sys.stdout, _log_fh)
sys.stderr = _TeeStream(sys.stderr, _log_fh)

from engine.runtime import Bus, Control
from engine.devices import MockDevice
from engine.vision import MockVision
from engine.loader import load_chain, load_subchains
from engine.interpreter import Interpreter, load_library, do_peek
from engine.brain import build_brain
from engine.agent import Agent, load_synth_chain

BASE = os.path.dirname(os.path.abspath(__file__))
CHAIN_PATH = os.path.join(BASE, "chains", "tennis_booking.json")
CHAINS_DIR = os.path.join(BASE, "chains")
STATIC_DIR = os.path.join(BASE, "static")

# 全局运行时状态
bus = Bus()
control = Control()
DEVICE = os.environ.get("DEVICE", "mock").lower()
# mac 本机已部署本地多模态模型（ui-tars:7b + qwen2.5vl:3b），默认用 lm 做文字定位；
# 其它设备无模型，默认 mock（text/template 模式不可用，需显式 OCR=real 并装 paddleocr）。
OCR = os.environ.get("OCR", "lm" if DEVICE == "mac" else "mock").lower()
DEBUG_EVIDENCE = True    # 调试证据：开启后每步点击记录前后截图 + 落点，供 debug.html 排查
HIDE_BROWSER = True    # 运行时默认隐藏浏览器窗口（防误点 / 防误判翻页）；单步模式不隐藏


def build_vision():
    if OCR == "real":
        from engine.vision import RealVision

        return RealVision()
    if OCR == "lm":
        # 本地多模态模型做文字定位：免装 PaddleOCR 重依赖。
        # 实测 qwen2.5vl:3b「读中文」可靠但空间定位系统性失准；改由 ui-tars:7b（7B GUI 专精，
        # 原生直出点击坐标，整图上像素级精准）做 grounding，qwen2.5vl:3b 做中文抽取/验证。
        from engine.vision import LMVision

        return LMVision()
    from engine.vision import MockVision

    return MockVision()


def capture_target_b64():
    """标注截屏：暂时隐藏最前的本网页浏览器窗口，截取其后方的真实目标（微信）界面，再恢复。

    根因修复：原 /capture 截的是全屏，而标注模态打开时浏览器必在最前 → 截到的是网页而非微信，
    导致归一化坐标相对网页定位，运行时点击落点错位（"点得离标注非常远"）。
    本函数把浏览器移开再截，确保截到的是用户真正想点的界面。
    依赖：macOS 辅助功能权限（osascript）+ 屏幕录制权限（screencapture）。
    """
    import subprocess
    import time

    name = None
    try:
        name = subprocess.check_output(
            ["osascript", "-e",
             'tell application "System Events" to get name of first process whose frontmost is true'],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", "ignore").strip()
        # 隐藏最前窗口（即本网页浏览器），让其后方的微信露出来
        if name and name.lower() not in ("finder", "loginwindow"):
            try:
                subprocess.call(
                    ["osascript", "-e",
                     f'tell application "System Events" to set visible of process "{name}" to false'],
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.7)
            except Exception:
                pass
    except Exception:
        name = None
    try:
        # 关键修复：标注截屏必须与运行时 _act_capture 使用同一坐标系——都走 screenshot()
        # （尊重 _region 的安全区子图）。原先 full_snapshot_b64() 截的是全屏，而运行时若设了
        # 安全区域则返回安全区子图，两者坐标系不一致会导致「框选范围 vs 实际裁剪」错位/偏小。
        _cap_path = g["device"].screenshot()
        import base64 as _b64
        if _cap_path:
            with open(_cap_path, "rb") as _f:
                b64 = _b64.b64encode(_f.read()).decode("ascii")
        else:
            # Mock 模式或 screenshot 未实现：回退到全屏截屏，至少让标注模式有图可画
            b64 = g["device"].full_snapshot_b64() or None
    except Exception as e:  # noqa: BLE001
        b64 = None
        print("[capture_target] 截图失败:", e)
    # 无论如何都要恢复最前窗口的可见性，避免浏览器被永久隐藏导致"点了标注模式没反应/窗口消失"
    if name:
        try:
            subprocess.call(
                ["osascript", "-e",
                 f'tell application "System Events" to set visible of process "{name}" to true'],
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        try:
            subprocess.call(
                ["osascript", "-e", f'tell application "{name}" to activate'],
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    return b64


def build_device(vis):
    if DEVICE == "adb":
        from engine.devices import ADBDevice

        return ADBDevice(vision=vis, serial=os.environ.get("ADB_SERIAL"))
    if DEVICE == "windows":
        from engine.devices import WindowsUIDevice

        return WindowsUIDevice(vision=vis)
    if DEVICE == "mac":
        from engine.devices import MacDevice

        return MacDevice(vision=vis)
    from engine.devices import MockDevice

    return MockDevice(total=6, vision=vis)


vision = build_vision()
device = build_device(vision)
chain = load_chain(CHAIN_PATH)
subchains = load_subchains(CHAINS_DIR)
brain = build_brain()
agent = Agent(device, vision, brain, bus, control)

g = {
    "device": device,
    "vision": vision,
    "brain": brain,
    "bus": bus,
    "control": control,
    "chain": chain,
    "subchains": subchains,
    "interp": None,
    "thread": None,
    "agent": agent,
    "agent_thread": None,
    "data": [],
    "status": "idle",
    "page": {"page": "list", "highlight": None, "round": 0},
    "progress": {"round": 0, "max": 0},
}

# 状态消费者：把事件汇总到全局状态（SSE 另有独立队列）
state_q = bus.subscribe()


def consumer():
    while True:
        ev = state_q.get()
        if ev is None:
            continue
        if ev["type"] == "data":
            g["data"].append(ev["record"])
        elif ev["type"] == "page":
            g["page"] = ev
        elif ev["type"] == "status":
            g["status"] = ev["state"]
        elif ev["type"] == "progress":
            g["progress"] = ev


threading.Thread(target=consumer, daemon=True).start()


# ---------------- 控制 ----------------
def heal_stale_state():
    """自愈：浏览器刷新不会停止后台线程，旧线程可能已死但状态仍停在
    running/exploring/replaying，导致「开始」按钮被 disabled 且 do_start 被
    「已在运行中」拦截。清理僵尸线程并复位孤儿状态，避免每次重开界面都点不了开始。"""
    if g["thread"] and not g["thread"].is_alive():
        g["thread"] = None
    if g["agent_thread"] and not g["agent_thread"].is_alive():
        g["agent_thread"] = None
    if g["status"] in ("running", "exploring", "replaying") and g["thread"] is None and g["agent_thread"] is None:
        g["status"] = "idle"
        bus.publish({"type": "status", "state": "idle"})


def do_start(speed=None, chain_file=None, step=False, focus_app="WeChat"):
    if speed is not None:
        try:
            control.speed = float(speed)
        except (TypeError, ValueError):
            pass
    heal_stale_state()  # 复位孤儿状态（浏览器刷新后旧线程可能已死但状态仍停在 running）
    if g["thread"] and g["thread"].is_alive():
        return {"ok": False, "msg": "已有任务在后台运行，请先点「停止」再开始"}
    # 可选指定链路文件（标注 UI 保存的自定义链路）；默认用主链路
    ch = chain
    if chain_file:
        cpath = os.path.join(CHAINS_DIR, os.path.basename(chain_file))
        if os.path.exists(cpath):
            try:
                ch = load_chain(cpath)
            except Exception:
                ch = chain
    g["interp"] = Interpreter(ch, subchains, g["device"], g["vision"], bus, control, g["brain"],
                             debug=DEBUG_EVIDENCE, hide_browser=HIDE_BROWSER, focus_app=focus_app)
    control.mode = "stepping" if step else "running"
    t = threading.Thread(target=g["interp"].run, daemon=True)
    t.start()
    g["thread"] = t
    return {"ok": True, "msg": "已开始"}


# ---------------- 链路保存 / 删除（标注 UI） ----------------
def do_save_chain(name, chain_obj):
    if not isinstance(chain_obj, dict) or not isinstance(chain_obj.get("steps"), list) or not chain_obj["steps"]:
        return {"ok": False, "msg": "链路格式不正确（需包含非空 steps 数组）"}
    raw = (name or chain_obj.get("name") or "custom_chain")
    safe = re.sub(r"[^A-Za-z0-9_\u4e00-\u9fa5-]", "_", str(raw)).strip("_") or "custom_chain"
    fname = safe if safe.endswith(".json") else safe + ".json"
    fpath = os.path.join(CHAINS_DIR, fname)
    out = dict(chain_obj)
    out["name"] = out.get("name") or str(raw)
    out.setdefault("description", "由标注 UI 生成的链路")
    out.setdefault("vars", {})
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"写入失败: {e}"}
    return {"ok": True, "file": fname, "path": fpath}


def do_delete_chain(file):
    if not file:
        return {"ok": False, "msg": "缺少 file"}
    fn = os.path.basename(str(file))
    if fn == os.path.basename(CHAIN_PATH) or fn.endswith("_subchain.json"):
        return {"ok": False, "msg": "主链路 / 子链不可删除"}
    fpath = os.path.join(CHAINS_DIR, fn)
    if not os.path.exists(fpath):
        return {"ok": False, "msg": "文件不存在"}
    try:
        os.remove(fpath)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"删除失败: {e}"}
    return {"ok": True, "file": fn}


def do_new_chain(name):
    """新建一个空的独立任务（链路文件）。标注互不干扰：每个任务有自己独立的
    标注数据（前端按「链路文件名」隔离存储），切换任务不会互相覆盖。"""
    raw = (name or "新任务").strip() or "新任务"
    safe = re.sub(r"[^A-Za-z0-9_\u4e00-\u9fa5-]", "_", raw).strip("_") or "new_task"
    fname = safe + ".json"
    fpath = os.path.join(CHAINS_DIR, fname)
    # 避免覆盖已有同名任务：追加序号
    n = 1
    while os.path.exists(fpath):
        fname = f"{safe}_{n}.json"
        fpath = os.path.join(CHAINS_DIR, fname)
        n += 1
    out = {"name": raw, "description": "新建任务（尚未标注）", "vars": {}, "steps": []}
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"写入失败: {e}"}
    return {"ok": True, "file": fname, "name": raw}


def do_verify(step_index=0, ref_image=None, chain_file=None, focus_app="WeChat"):
    """单独验证链路里的某一步（默认第 1 步）：只执行这一次点击并记录前后截图证据，
    不进循环。配合前端 ref_image（标注时画框用的截图）对比，可区分三类问题：
      (a) 落点偏了 / 微信挪了 → 标注参考图与运行时 before 图对不上；
      (b) 落点准但没翻页 → 红十字在卡片上但 changed=否；
      (c) 隐藏浏览器后正常翻页 → 根因是浏览器误判（已默认开启隐藏）。"""
    ch = chain
    if chain_file:
        cpath = os.path.join(CHAINS_DIR, os.path.basename(str(chain_file)))
        if os.path.exists(cpath):
            try:
                ch = load_chain(cpath)
            except Exception:
                ch = chain
    flat = []

    def collect(steps):
        for s in (steps or []):
            if s.get("action") == "loop":
                collect(s.get("body") or [])
            else:
                flat.append(s)

    collect(ch.get("steps", []))
    if not flat:
        return {"ok": False, "msg": "链路里没有可执行步骤"}
    idx = max(0, min(int(step_index), len(flat) - 1))
    step = flat[idx]
    interp = Interpreter(ch, subchains, g["device"], g["vision"], bus, control, g["brain"],
                         debug=True, hide_browser=HIDE_BROWSER, focus_app=focus_app)
    rd = interp._ensure_run_dir()
    meta = {}
    if ref_image and rd:
        try:
            import base64 as _b64
            raw = re.sub(r"^data:image/\w+;base64,", "", ref_image)
            with open(os.path.join(rd, "ref.png"), "wb") as f:
                f.write(_b64.b64decode(raw))
            meta["ref"] = "ref.png"
        except Exception:
            pass
    if meta:
        try:
            with open(os.path.join(rd, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f)
        except Exception:
            pass
    if g["thread"] and g["thread"].is_alive():
        return {"ok": False, "msg": "已有任务在运行，请先停止"}
    t = threading.Thread(target=interp.run_single, args=(step,), daemon=True)
    t.start()
    g["thread"] = t
    return {"ok": True, "msg": f"已启动验证第{idx + 1}步({step.get('action')})",
            "run": os.path.basename(rd)}


def do_reset():
    control.mode = "idle"
    g["device"] = build_device(g["vision"])
    g["agent"].device = g["device"]
    g["agent"].vision = g["vision"]
    g["data"] = []
    g["status"] = "idle"
    g["page"] = {"page": "list", "highlight": None, "round": 0}
    g["progress"] = {"round": 0, "max": 0}
    g["agent"].trace = []
    g["agent"].synth_chain = None
    g["agent"].phase = "idle"
    bus.publish({"type": "reset"})
    bus.publish({"type": "status", "state": "idle"})
    return {"ok": True, "msg": "已重置"}


# ---------------- Agent 控制 ----------------
def do_explore(goal=None):
    if g["agent_thread"] and not g["agent_thread"].is_alive():
        g["agent_thread"] = None
    if g["agent_thread"] and g["agent_thread"].is_alive():
        return {"ok": False, "msg": "Agent 探索进行中"}
    if not goal:
        goal = "遍历列表，抓取每个场地的名称、地址和订场链接"
    control.mode = "running"
    t = threading.Thread(target=g["agent"].explore, args=(goal,), daemon=True)
    t.start()
    g["agent_thread"] = t
    return {"ok": True, "msg": "Agent 探索已启动"}


def do_replay():
    if g["agent_thread"] and not g["agent_thread"].is_alive():
        g["agent_thread"] = None
    if g["agent_thread"] and g["agent_thread"].is_alive():
        return {"ok": False, "msg": "Agent 探索进行中，请先等待"}
    if not g["agent"].synth_chain and not load_synth_chain():
        return {"ok": False, "msg": "尚无合成链路，请先「探索并合成」"}
    # 重放前重置设备，保证从干净状态确定性跑完整列表
    control.mode = "idle"
    g["device"] = build_device(g["vision"])
    g["agent"].device = g["device"]
    g["agent"].vision = g["vision"]
    g["data"] = []
    bus.publish({"type": "reset"})
    control.mode = "running"
    t = threading.Thread(target=g["agent"].replay, daemon=True)
    t.start()
    g["agent_thread"] = t
    return {"ok": True, "msg": "合成链路重放已启动"}


def do_agent_reset():
    control.mode = "idle"
    g["device"] = build_device(g["vision"])
    g["agent"].device = g["device"]
    g["agent"].vision = g["vision"]
    g["agent"].trace = []
    g["agent"].synth_chain = None
    g["agent"].phase = "idle"
    g["data"] = []
    g["status"] = "idle"
    g["page"] = {"page": "list", "highlight": None, "round": 0}
    g["progress"] = {"round": 0, "max": 0}
    bus.publish({"type": "reset"})
    bus.publish({"type": "status", "state": "idle"})
    return {"ok": True, "msg": "Agent 已重置"}


def do_control(body):
    action = (body or {}).get("action")
    if action == "start":
        return do_start(body.get("speed"), body.get("chain"), body.get("step"), body.get("focus_app"))
    if action == "pause":
        control.signal("paused")
        return {"ok": True, "msg": "已暂停"}
    if action == "step":
        control.signal("stepping")
        return {"ok": True, "msg": "单步执行中"}
    if action == "stop":
        control.signal("stopped")
        return {"ok": True, "msg": "已停止"}
    if action == "reset":
        return do_reset()
    if action == "explore":
        return do_explore(body.get("goal"))
    if action == "replay":
        return do_replay()
    if action == "agent_reset":
        return do_agent_reset()
    if action == "agent_stop":
        control.signal("stopped")
        return {"ok": True, "msg": "已停止"}
    if action == "set_region":
        reg = g["device"].set_region(
            body.get("x", 0), body.get("y", 0),
            body.get("w", 0), body.get("h", 0),
        )
        return {"ok": reg is not None, "region": reg}
    if action == "clear_region":
        g["device"].clear_region()
        return {"ok": True, "region": None}
    if action == "set_click_offset":
        off = g["device"].set_click_offset(body.get("dx", 0), body.get("dy", 0))
        return {"ok": True, "offset": off}
    if action == "set_tap_retry":
        rt = g["device"].set_tap_retry(body.get("max_retry", 0), body.get("jitter", 0))
        return {"ok": True, "tap_retry": rt}
    if action == "set_click_mode":
        cm = g["device"].set_click_mode(body.get("mode", "cgevent"))
        return {"ok": True, "click_mode": cm}
    if action == "new_chain":
        return do_new_chain(body.get("name"))
    if action == "save_chain":
        return do_save_chain(body.get("name"), body.get("chain"))
    if action == "delete_chain":
        return do_delete_chain(body.get("file"))
    if action == "debug":
        global DEBUG_EVIDENCE
        DEBUG_EVIDENCE = bool(body.get("on", False))
        return {"ok": True, "debug": DEBUG_EVIDENCE}
    if action == "peek":
        # 调试工具「查看识别范围」：独立于标注链，直接在调试面板拖框查看「全屏红框+裁剪+OCR」，
        # 一眼区分截歪（坐标错） vs 识别错（文字读不出）。使用模块级 do_peek，无需 Interpreter 实例。
        region = body.get("region")
        if not region:
            return {"ok": False, "msg": "查看识别范围：缺少 region——请先在画布上拖一个框"}
        dev = g.get("device")
        if dev is None:
            return {"ok": False, "msg": "尚未初始化设备，请先启动一次运行或确认设备已连接"}
        do_peek(dev, g.get("vision"), bus, region)
        return {"ok": True, "msg": "已推送识别范围预览（见手机预览上的「识别范围预览」面板）"}
    if action == "hide_browser":
        global HIDE_BROWSER
        HIDE_BROWSER = bool(body.get("on", False))
        return {"ok": True, "hide_browser": HIDE_BROWSER}
    if action == "verify":
        return do_verify(body.get("step", 0), body.get("ref_image"), body.get("chain"), body.get("focus_app"))
    if action == "set_brain":
        return set_brain_runtime(body.get("brain"), body.get("base_url"), body.get("model"), body.get("api_key"))
    return {"ok": False, "msg": f"未知动作: {action}"}


def set_brain_runtime(brain, base_url=None, model=None, api_key=None):
    """界面「识别模型」下拉框在运行时切换大脑/模型，无需重启或改环境变量。

    同时更新 g["brain"]（确定性链路 llm_extract/llm_judge 用）与 g["agent"].brain（GUI Agent 模式用）。"""
    try:
        new_brain = build_brain(brain=brain, base_url=base_url, model=model, api_key=api_key)
    except Exception as e:
        return {"ok": False, "msg": f"构建大脑失败: {e}"}
    g["brain"] = new_brain
    # GUI Agent 模式复用同一大脑实例
    if g.get("agent") is not None:
        try:
            g["agent"].brain = new_brain
        except Exception:
            pass
    return {"ok": True, "brain": type(new_brain).__name__, "model": getattr(new_brain, "model", "?"),
            "base_url": getattr(new_brain, "base_url", "?")}


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ctype(self, path):
        if path.endswith(".html"):
            return "text/html; charset=utf-8"
        if path.endswith(".css"):
            return "text/css; charset=utf-8"
        if path.endswith(".js"):
            return "application/javascript; charset=utf-8"
        if path.endswith(".json"):
            return "application/json; charset=utf-8"
        if path.endswith(".png"):
            return "image/png"
        if path.endswith(".svg"):
            return "image/svg+xml"
        return "application/octet-stream"

    def _send_csv(self, rows, fname):
        import csv
        import io

        buf = io.StringIO()
        # UTF-8-SIG：让 Excel 正确识别中文表头，不乱码
        buf.write("\ufeff")
        fields = []
        for r in rows:
            for k in r.keys():
                if k not in fields:
                    fields.append(k)
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
        data = buf.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        elif p == "/chains":
            chains = []
            if os.path.isdir(CHAINS_DIR):
                for fn in sorted(os.listdir(CHAINS_DIR)):
                    if not fn.endswith(".json") or fn.endswith("_subchain.json"):
                        continue
                    try:
                        with open(os.path.join(CHAINS_DIR, fn), encoding="utf-8") as f:
                            c = json.load(f)
                        chains.append({
                            "name": c.get("name", fn),
                            "file": fn,
                            "description": c.get("description", ""),
                        })
                    except Exception:
                        continue
            self._send_json({"chains": chains})
        elif p == "/state":
            dev = g["device"]
            # 防御：MockDevice/未连接设备可能缺部分方法，逐个用 hasattr 兜底，
            # 避免 /state 抛异常导致前端初始化（loadChainList、区域恢复）整段中断。
            screen = {"w": 0, "h": 0}
            if hasattr(dev, "screen_size"):
                try:
                    sw, sh = dev.screen_size()
                    screen = {"w": sw, "h": sh}
                except Exception:
                    pass
            self._send_json({
                "status": g["status"], "page": g["page"],
                "progress": g["progress"], "device": DEVICE,
                "ocr": OCR,
                "brain": type(g["brain"]).__name__,
                "build": "20260803.19",  # 三处修复：①标注按任务持久化（CHAIN_SEL_KEY 记住所选链路，刷新后 annoKey 对齐，不再"恢复原样"）；②LLM 识别截图日志（_act_llm_extract 每次把送进模型的裁剪图+全屏+识别结果落盘 runs/<ts>/ 并推送 🧠识别证据到调试面板）；③修复 do_peek 诊断 OCR 空问题（fields={} 时 ocr_extract 提前 return {} 不转写；改为直接调 _transcribe_lines 取原始逐行文字）。
                "screen": screen,
                "region": dev.region() if hasattr(dev, "region") else None,
                "click_offset": dev.click_offset() if hasattr(dev, "click_offset") else None,
                "tap_retry": dev.tap_retry() if hasattr(dev, "tap_retry") else None,
                "click_mode": dev.click_mode() if hasattr(dev, "click_mode") else "",
                "agent": {
                    "phase": g["agent"].phase,
                    "brain": type(g["agent"].brain).__name__,
                    "has_synth": g["agent"].synth_chain is not None,
                    "trace_len": len(g["agent"].trace),
                },
            })
        elif p == "/capture":
            # 全屏截屏（忽略安全区域），供前端「框选范围」时预览绘制
            self._send_json({"image": g["device"].full_snapshot_b64()})
        elif p == "/capture_target":
            # 标注专用：隐藏最前的本网页浏览器窗口，截取其后方的真实目标(微信)界面，再恢复。
            # 解决"标注截的是浏览器而非微信→全屏归一化坐标错位→点击飞到别处"的根因。
            self._send_json({"image": capture_target_b64()})
        elif p == "/debug_latest":
            # 返回最近一次运行的点击证据（每步 before/after 文件名 + 落点 + 是否变化），供 debug.html 排查
            runs_dir = os.path.join(BASE, "runs")
            steps = []
            latest = None
            ref = None
            try:
                if os.path.isdir(runs_dir):
                    subs = sorted(d for d in os.listdir(runs_dir)
                                  if os.path.isdir(os.path.join(runs_dir, d)))
                    if subs:
                        latest = subs[-1]
                        evfile = os.path.join(runs_dir, latest, "evidence.jsonl")
                        if os.path.exists(evfile):
                            with open(evfile, encoding="utf-8") as f:
                                for line in f:
                                    line = line.strip()
                                    if line:
                                        steps.append(json.loads(line))
                        metaf = os.path.join(runs_dir, latest, "meta.json")
                        if os.path.exists(metaf):
                            try:
                                ref = json.load(open(metaf, encoding="utf-8")).get("ref")
                            except Exception:
                                ref = None
            except Exception:
                pass
            self._send_json({"run": latest, "steps": steps, "ref": ref})
        elif p.startswith("/debug_img"):
            # 返回某次运行里的某张证据截图（?run=<ts>&file=<name>）
            q = parse_qs(urlparse(self.path).query)
            run = (q.get("run") or [""])[0]
            fname = (q.get("file") or [""])[0]
            fpath = os.path.join(BASE, "runs", os.path.basename(run), os.path.basename(fname))
            if run and fname and os.path.exists(fpath):
                self._send_file(fpath, "image/png")
            else:
                self.send_error(404)
        elif p == "/agent_chain":
            self._send_json({"chain": load_synth_chain() or {}})
        elif p == "/data":
            self._send_json({"data": g["data"]})
        elif p == "/library":
            # 持久化库（data/library.jsonl）：跨运行保留的全部落库记录
            self._send_json({"data": load_library()})
        elif p == "/library.csv":
            # 导出持久化库为 CSV（按出现的字段取并集，UTF-8-SIG 防 Excel 乱码）
            self._send_csv(load_library(), "library.csv")
        elif p == "/stream":
            self._serve_sse()
        else:
            # 兜底：安全服务 static/ 目录下存在的静态文件（如 debug.html），防目录穿越
            rel = p.lstrip("/")
            fp = os.path.normpath(os.path.join(STATIC_DIR, rel))
            base = os.path.normpath(STATIC_DIR)
            if rel and not rel.startswith(".") and fp.startswith(base + os.sep) and os.path.isfile(fp):
                self._send_file(fp, self._ctype(fp))
            else:
                self.send_error(404)

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = bus.subscribe()
        # 前端重连/刷新时先自愈孤儿状态，保证初始快照里的 status 已是干净的 idle，
        # 否则「开始」按钮会被上一会话残留的 running 状态禁用。
        heal_stale_state()
        # 初始快照
        snap = {"type": "snapshot", "status": g["status"], "page": g["page"],
                "progress": g["progress"], "data": g["data"]}
        self.wfile.write(("data: " + json.dumps(snap, ensure_ascii=False) + "\n\n").encode("utf-8"))
        self.wfile.flush()
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                except Exception:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {}
        if p == "/control":
            self._send_json(do_control(body))
        else:
            self.send_error(404)


def main():
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"操作链路自动化引擎已启动: http://127.0.0.1:{port}")
    # 终端键盘安全开关：s 紧急停止 / p 暂停·继续（在运行本进程的终端按键）
    try:
        from engine.hotkey import HotkeyMonitor

        hk = HotkeyMonitor(control)
        if hk.start():
            print("键盘安全开关已启用：在运行终端按 [s] 紧急停止，[p] 暂停/继续（或 Ctrl+C 终止进程）")
        else:
            print("键盘安全开关未启用（stdin 非终端）；紧急情况可按 Ctrl+C 终止进程")
    except Exception as e:  # noqa: BLE001
        print(f"键盘安全开关初始化失败：{e}（可用 Ctrl+C 终止）")

    # 退出前还原终端属性（restore 在 SIGINT/SIGTERM 时也会调用，避免终端无回显）
    def _on_exit(signum, frame):
        try:
            hk.restore()
        except Exception:
            pass
        import os as _os
        _os._exit(0)

    try:
        import signal as _signal

        _signal.signal(_signal.SIGINT, _on_exit)
        _signal.signal(_signal.SIGTERM, _on_exit)
    except Exception:
        pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
