"""DSL 解释器：顺序 / 循环 / 子链 / 变量 / emit，并把事件发布到 Bus。"""
import json
import os
import re
import time

from engine.runtime import StopExecution

# 持久化"库"：emit 落库记录追加写入此 jsonl 文件，跨运行保留（GET /library 读取）。
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_LIBRARY_FILE = os.path.join(_DATA_DIR, "library.jsonl")
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except Exception:  # noqa: BLE001
    pass


def _persist_library(rec):
    """把一条落库记录追加到持久化库（每行一个 JSON）。失败静默忽略。"""
    try:
        with open(_LIBRARY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def load_library():
    """读取持久化库，返回记录列表（按写入顺序，最新在末尾）。"""
    recs = []
    if not os.path.exists(_LIBRARY_FILE):
        return recs
    try:
        with open(_LIBRARY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return recs


class Interpreter:
    def __init__(self, chain, subchains, device, vision, bus, control, brain=None, debug=False, hide_browser=False, focus_app=None):
        self.chain = chain
        self.subchains = subchains
        self.device = device
        self.vision = vision
        self.bus = bus
        self.control = control
        self.brain = brain
        self.vars = dict(chain.get("vars") or {})
        self.data = []
        self.step_counter = 0
        self._click_index = 0     # 点击步骤序号（调试可视化用）
        self._pre_fp = ""        # 该步执行前的屏幕指纹（wait_until_change 用）
        self._skip_body = False  # llm_judge 判定失败→跳过所在循环体剩余步骤
        self.debug = debug       # 调试证据：记录每步点击前后截图 + 落点 + 是否变化
        self._run_dir = ""       # 本次运行的证据目录 runs/<ts>/
        self.hide_browser = hide_browser  # 运行时隐藏浏览器窗口（防误点 / 防误判翻页）
        self.focus_app = focus_app        # 运行前激活的目标 App（如"WeChat"），确保窗口在最前/有焦点

    # ---------- 变量渲染 ----------
    def render(self, s):
        if not isinstance(s, str):
            return s

        def repl(m):
            key = m.group(1).strip()
            return str(self.vars.get(key, m.group(0)))

        return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", repl, s)

    def render_obj(self, obj):
        if isinstance(obj, dict):
            return {k: self.render_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.render_obj(v) for v in obj]
        if isinstance(obj, str):
            return self.render(obj)
        return obj

    # ---------- 流控检查点 ----------
    def _checkpoint(self):
        c = self.control
        if c.mode == "stopped":
            raise StopExecution()
        if c.mode == "paused":
            while c.mode == "paused":
                c._ev.wait()
                c._ev.clear()
            if c.mode == "stopped":
                raise StopExecution()
        if c.mode == "stepping":
            c.mode = "paused"
            c._ev.set()
            c._ev.clear()

    def _sleep(self, step=None):
        """步间等待。三种来源（按优先级）：

        1) wait_until_change：轮询屏幕指纹，直到页面真正变化（跳转/弹窗出现）或超时 wait_timeout 秒。
           专治"固定秒数赌不准"——第二步总抢在跳转前点出去。命中后额外稳 0.3s 等动效收尾。
        2) wait_after：固定延时 N 秒（仍可用，但易因页面慢而抢跑）。
        3) 否则用全局速度 control.speed。
        """
        if step and isinstance(step, dict) and step.get("wait_until_change"):
            timeout = 8.0
            try:
                timeout = float(step.get("wait_timeout", 8.0))
            except (TypeError, ValueError):
                pass
            pre = getattr(self, "_pre_fp", "") or ""
            t0 = time.time()
            changed = False
            while time.time() - t0 < timeout:
                time.sleep(0.1)
                now = self.device.screen_fp()
                if pre and now and now != pre:
                    changed = True
                    break
            if changed:
                time.sleep(0.3)  # 等弹窗/动效收尾
            self.bus.publish({
                "type": "log", "level": "INFO",
                "msg": f"步后等待(直到屏幕变化): {'已检测到页面变化，继续' if changed else f'超时未变化({timeout}s)，仍继续'}",
            })
            return
        dur = self.control.speed
        if step and isinstance(step, dict) and "wait_after" in step:
            try:
                dur = float(step["wait_after"])
            except (TypeError, ValueError):
                dur = self.control.speed
        time.sleep(dur)

    # ---------- 入口 ----------
    def run(self):
        # 纯采集模式（capture 动作）每轮运行前清空旧截图目录，保证 captures/last 只含本批截图
        self._capture_dir = None
        self._capture_count = 0
        # 重置设备的「翻页指纹」：can_scroll() 用 _last_hash 判断能否继续滚动，
        # 该哈希存在设备单例上、跨运行不清除，会导致第二次运行第一轮就被误判"滑不动"提前结束。
        try:
            if hasattr(self.device, "_last_hash"):
                self.device._last_hash = None
        except Exception:  # noqa: BLE001
            pass
        self.bus.publish({"type": "status", "state": "running"})
        hidden = False
        # 非单步模式下，运行前隐藏浏览器窗口：确保点击不被遮挡、翻页判定只针对微信
        if self.hide_browser and self.control.mode != "stepping" and hasattr(self.device, "hide_browser"):
            try:
                self.device.hide_browser()
                hidden = True
                self.bus.publish({"type": "log", "level": "INFO",
                                  "msg": "已隐藏浏览器窗口以确保点击/翻页判定准确；运行结束后自动恢复"})
            except Exception:  # noqa: BLE001
                hidden = False
        # 运行前把目标 App（默认微信）带到最前，确保合成点击真正落到它身上，
        # 而不是被桌面/其它窗口吞掉（这正是"落点准但没跳转"的常见根因）。
        # 顺序：先隐藏浏览器(防遮挡/防误判)，再 activate 目标 App 抢回焦点。
        if self.focus_app and hasattr(self.device, "activate_app"):
            try:
                self.device.activate_app(self.focus_app)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._run_steps(self.chain.get("steps", []))
            self.bus.publish({"type": "status", "state": "done", "count": len(self.data)})
        except StopExecution:
            self.bus.publish({"type": "status", "state": "stopped"})
        except Exception as e:  # noqa: BLE001
            self.bus.publish({"type": "log", "level": "ERROR", "msg": f"执行异常: {e}"})
            self.bus.publish({"type": "status", "state": "error"})
        finally:
            if hidden and hasattr(self.device, "show_browser"):
                try:
                    self.device.show_browser()
                except Exception:  # noqa: BLE001
                    pass

    def run_single(self, step):
        """只执行单个步骤一次（验证用）：记录前后截图证据，不进循环、不重复。

        用于把"第①步卡片点击"单独拎出来，确认它到底有没有翻页 / 落点偏没偏。"""
        self.bus.publish({"type": "status", "state": "running"})
        self._capture_dir = None
        self._capture_count = 0
        try:
            if hasattr(self.device, "_last_hash"):
                self.device._last_hash = None
        except Exception:  # noqa: BLE001
            pass
        hidden = False
        if self.hide_browser and self.control.mode != "stepping" and hasattr(self.device, "hide_browser"):
            try:
                self.device.hide_browser()
                hidden = True
                self.bus.publish({"type": "log", "level": "INFO", "msg": "验证模式：已隐藏浏览器窗口"})
            except Exception:  # noqa: BLE001
                hidden = False
        # 验证模式同样先激活目标 App，确保点击落到微信而非被其它窗口吞掉。
        if self.focus_app and hasattr(self.device, "activate_app"):
            try:
                self.device.activate_app(self.focus_app)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._exec_step(step)
            self._sleep(step)
            self.bus.publish({"type": "status", "state": "done", "count": 1})
        except StopExecution:
            self.bus.publish({"type": "status", "state": "stopped"})
        except Exception as e:  # noqa: BLE001
            self.bus.publish({"type": "log", "level": "ERROR", "msg": f"验证执行异常: {e}"})
            self.bus.publish({"type": "status", "state": "error"})
        finally:
            if hidden and hasattr(self.device, "show_browser"):
                try:
                    self.device.show_browser()
                except Exception:  # noqa: BLE001
                    pass

    def _run_steps(self, steps):
        for step in steps:
            self._checkpoint()
            if self.control.mode == "stopped":
                raise StopExecution()
            self._exec_step(step)
            self._sleep(step)
            if self._skip_body:
                self._skip_body = False
                break

    # ---------- 单步执行 ----------
    def _exec_step(self, step):
        self.step_counter += 1
        action = step.get("action")
        target = self.render_obj(step.get("target") or {})
        sid = step.get("id") or f"s{self.step_counter}"
        # 记录"该步执行前"的屏幕指纹，供 wait_until_change 判定页面是否真的变了
        try:
            self._pre_fp = self.device.screen_fp()
        except Exception:
            self._pre_fp = ""
        self.bus.publish({"type": "step", "id": sid, "action": action, "status": "running", "target": target})
        try:
            handler = getattr(self, f"_act_{action}", None)
            if handler is None:
                self.bus.publish({"type": "log", "level": "WARN", "msg": f"未实现动作: {action}（跳过）"})
                self.bus.publish({"type": "step", "id": sid, "action": action, "status": "skip"})
                return
            result = handler(step, target)
            save_to = step.get("save_to") or {}
            if save_to and isinstance(result, dict):
                for fld, var in save_to.items():
                    if fld in result:
                        self.vars[var] = result[fld]
            self.bus.publish({
                "type": "page",
                "page": self.device.page,
                "highlight": self.device.highlight,
                "round": getattr(self.device, "round", 0),
            })
            self.bus.publish({"type": "step", "id": sid, "action": action, "status": "done", "result": result})
            # 真实设备（MacDevice 等）每步回传实时截屏，前端面板即可看到真实操作
            try:
                snap = self.device.snapshot_b64()
                if snap:
                    self.bus.publish({"type": "screen", "image": snap})
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            on_fail = step.get("on_fail") or {}
            retry = on_fail.get("retry", 0)
            if retry > 0:
                step.setdefault("_rt", 0)
                step["_rt"] += 1
                if step["_rt"] <= retry:
                    self.bus.publish({"type": "log", "level": "WARN", "msg": f"{action} 失败，重试 {step['_rt']}/{retry}"})
                    return self._exec_step(step)
            self.bus.publish({"type": "log", "level": "ERROR", "msg": f"{action} 失败: {e}"})
            self.bus.publish({"type": "step", "id": sid, "action": action, "status": "error"})
            if on_fail.get("abort"):
                raise StopExecution()

    # ---------- 动作处理 ----------
    def _publish_click(self, action, step, region_norm, image):
        """调试可视化：发布一次点击的「瞄准区域 + 实际落点 + 点击前截图」，供前端标红查看。

        region_norm: 归一化 [x,y,w,h]（相对全屏），None 时回退取设备最近一次瞄准区域。
        image: 点击前的全屏预览图（base64，忽略安全区域），用于把红框画在"点击发生时"的页面上。
        """
        sid = step.get("id") or f"s{self.step_counter}"
        nc = getattr(self.device, "norm_click", None)
        point_norm = nc() if callable(nc) else None
        if region_norm is None:
            region_norm = getattr(self.device, "_last_region", None)
        self._click_index += 1
        self.bus.publish({
            "type": "click", "action": action, "sid": sid, "index": self._click_index,
            "region": region_norm, "point": point_norm,
            "image": image or "", "screen": list(self.device.screen_size()),
        })

    def _act_assert(self, step, target):
        exp = target.get("expect_text")
        self.bus.publish({"type": "log", "level": "INFO", "msg": f"断言 期望含「{exp}」（当前页={self.device.page}）"})
        return {"ok": True}

    def _act_tap_text(self, step, target):
        text = target.get("text")
        before_b64 = self._pre_click_image()
        before_path = self.device.screenshot()
        pre_fp = self.device.screen_fp()
        self.device.tap_text(text)
        after_path = self.device.screenshot()
        post_fp = self.device.screen_fp()
        self._publish_click("点击文字", step, None, before_b64)
        self.bus.publish({"type": "log", "level": "INFO", "msg": f"点击文字「{text}」"})
        if self.debug:
            self._save_evidence(step, before_path, after_path, None, pre_fp, post_fp)
        return {"tapped": text}

    def _act_tap_image(self, step, target):
        tpl = target.get("template")
        before_b64 = self._pre_click_image()
        before_path = self.device.screenshot()
        pre_fp = self.device.screen_fp()
        self.device.tap_image(tpl)
        after_path = self.device.screenshot()
        post_fp = self.device.screen_fp()
        self._publish_click("点击图标", step, None, before_b64)
        self.bus.publish({"type": "log", "level": "INFO", "msg": f"点击图标「{tpl}」"})
        if self.debug:
            self._save_evidence(step, before_path, after_path, None, pre_fp, post_fp)
        return {"tapped_image": tpl}

    def _act_tap_coord(self, step, target):
        before_b64 = self._pre_click_image()
        before_path = self.device.screenshot()
        pre_fp = self.device.screen_fp()
        self.device.tap_coord(target.get("x"), target.get("y"))
        after_path = self.device.screenshot()
        post_fp = self.device.screen_fp()
        self._publish_click("点击坐标", step, target.get("region"), before_b64)
        if self.debug:
            self._save_evidence(step, before_path, after_path, target.get("region"), pre_fp, post_fp)
        return {"tapped_coord": True}

    def _act_tap_region(self, step, target):
        """框定区域点击（RPA 式确定性点击）：target.region=[x,y,w,h] 归一化(0..1,相对全屏)，
        mode=center|template|text。适合标准化流程里位置固定的按钮/图标，比模型自主 grounding 更准。"""
        mode = (target.get("mode") or "center")
        before_b64 = self._pre_click_image()
        before_path = self.device.screenshot()
        pre_fp = self.device.screen_fp()
        res = self.device.tap_region(target)
        after_path = self.device.screenshot()
        post_fp = self.device.screen_fp()
        # center 模式：瞄准区域即用户标注的归一化 region；其它模式回退取设备实际匹配区域
        region_norm = target.get("region") if mode == "center" else None
        self._publish_click("区域点击" if mode == "center" else f"区域({mode})", step, region_norm, before_b64)
        self.bus.publish({
            "type": "log", "level": "INFO",
            "msg": f"框定区域点击[{mode}] region={target.get('region')}",
        })
        if self.debug:
            self._save_evidence(step, before_path, after_path, region_norm, pre_fp, post_fp)
        return res

    def _pre_click_image(self):
        """点击前抓一张全屏预览（忽略安全区域），用于调试可视化背景。失败返回空串。"""
        try:
            return self.device.full_snapshot_b64() or ""
        except Exception:
            return ""

    # ---------- 调试证据：每步点击前后截图 + 落点 + 是否变化 ----------
    def _ensure_run_dir(self):
        if not self._run_dir:
            ts = time.strftime("%Y%m%d_%H%M%S")
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            self._run_dir = os.path.join(base, "runs", ts)
            os.makedirs(self._run_dir, exist_ok=True)
        return self._run_dir

    def _save_evidence(self, step, before_path, after_path, region_norm, pre_fp, post_fp):
        """把一次点击的「点击前/后全屏截图」拷进 runs/<ts>/，并追加一行 evidence.jsonl。

        前端 debug.html 会把这些图加载出来，并在「点击前」图上画出：
          - 绿色框：本次瞄准的归一化区域（region）
          - 红色十字：实际落点（设备最近一次点击的归一化坐标）
        一眼就能看出：落点到底在不在卡片上、点击后屏幕有没有真的变（是否翻页）。
        """
        import shutil
        try:
            self._ensure_run_dir()
            n = self.step_counter
            sid = step.get("id") or f"s{n}"
            bname = f"step_{n:02d}_{sid}_before.png"
            aname = f"step_{n:02d}_{sid}_after.png"
            dname = ""
            delayed_fp = None
            try:
                if os.path.exists(before_path):
                    shutil.copy(before_path, os.path.join(self._run_dir, bname))
                if os.path.exists(after_path):
                    shutil.copy(after_path, os.path.join(self._run_dir, aname))
                # 调试：点击后延迟 3s 再截一张，区分"完全没跳转"与"跳转慢(小程序异步加载，截早了)"
                # 经验证：微信小程序卡片点击→详情页是异步加载，约 1.5s 才完成渲染，
                # 0.8s 的窗口永远截不到跳转后页面 → 误判为"没反应"。3s 足够覆盖。
                if self.debug:
                    time.sleep(3.0)
                    dpath = self.device.screenshot()
                    delayed_fp = self.device.screen_fp()
                    if os.path.exists(dpath):
                        dname = f"step_{n:02d}_{sid}_delayed.png"
                        shutil.copy(dpath, os.path.join(self._run_dir, dname))
            except Exception:
                pass
            changed = bool(pre_fp and post_fp and pre_fp != post_fp)
            # 诊断：记录点击前最前窗口名称（确认焦点是否在微信上）
            frontmost = ""
            if self.debug and hasattr(self.device, "frontmost_info"):
                try:
                    frontmost = self.device.frontmost_info()
                except Exception:
                    frontmost = "(error)"
            entry = {
                "step": n,
                "sid": sid,
                "action": step.get("action"),
                "region": region_norm,                       # 归一化 [x,y,w,h]，center 模式即用户标注框
                "click": list(self.device.norm_click() or []),  # 实际落点归一化 [x,y]
                "screen": list(self.device.screen_size()),  # 逻辑点数 (w,h)
                "screen_px": list(getattr(self.device, "_screen_px", (0, 0)) or (0, 0)),  # 设备像素(核对是否一致)
                "pre_fp": (pre_fp or "")[:10],
                "post_fp": (post_fp or "")[:10],
                "changed": changed,                         # 点击后屏幕是否真的变化（翻页/弹窗）
                "delayed_changed": bool(pre_fp and post_fp and pre_fp == post_fp and delayed_fp and delayed_fp != post_fp),  # 即时没变但延迟后变了 → "跳转慢，截图截早了"
                "delayed": dname,                           # 延迟 0.8s 后的截图文件名
                "click_mode": getattr(self.device, "_click_mode_log", ""),  # 实际使用的点击方式
                "frontmost": frontmost,                       # 点击前最前窗口名称（诊断焦点）
                "before": bname,
                "after": aname,
            }
            with open(os.path.join(self._run_dir, "evidence.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.bus.publish({
                "type": "log", "level": "INFO",
                "msg": f"[证据] 步骤{n} {step.get('action')} 落点={entry['click']} 屏幕变化={'是' if changed else '否'}",
            })
        except Exception as e:  # noqa: BLE001
            self.bus.publish({"type": "log", "level": "WARN", "msg": f"证据记录失败: {e}"})

    def _save_llm_evidence(self, region, screen_region, crop_b64, full_b64, text_fields, recognized):
        """LLM 识别证据（Task #49）：把运行时【真正送进模型的裁剪图 + 全屏 + 识别结果】落盘并推到前端调试面板。

        用户可在 🔍调试 面板直接看到：模型当时到底看到了什么、识别成了什么，
        一眼区分「截歪（坐标问题）」还是「读不出（模型/提示词问题）」。"""
        try:
            self._ensure_run_dir()
            n = self.step_counter
            crop_name = f"llm_extract_{n:02d}_crop.png"
            full_name = f"llm_extract_{n:02d}_full.png"
            import base64 as _b64
            # 落盘裁剪图（模型实际看到的）
            if crop_b64:
                try:
                    with open(os.path.join(self._run_dir, crop_name), "wb") as f:
                        f.write(_b64.b64decode(crop_b64))
                except Exception:
                    crop_name = ""
            # 落盘全屏（带红框标出送 LLM 的范围）
            boxed_full = ""
            if full_b64:
                try:
                    boxed_full = draw_box(full_b64, screen_region)
                    with open(os.path.join(self._run_dir, full_name), "wb") as f:
                        f.write(_b64.b64decode(boxed_full))
                except Exception:
                    boxed_full = full_b64
                    full_name = ""
            recognized = recognized or {}
            ocr_text = "\n".join(f"{k} = {v}" for k, v in recognized.items()) or "(模型未识别到任何字段)"
            entry = {
                "type": "llm_extract",
                "step": n,
                "region": region,
                "screen_region": screen_region,
                "fields": text_fields,
                "recognized": recognized,
                "crop": crop_name,
                "full": full_name,
            }
            with open(os.path.join(self._run_dir, "evidence.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.bus.publish({
                "type": "log", "level": "INFO",
                "msg": f"[LLM证据] 步骤{n} 裁剪图→runs/{os.path.basename(self._run_dir)}/{crop_name}；识别结果={recognized}",
            })
            # 推到前端调试面板（复用 peek 事件，hdr/tip 不同以示区分）
            self.bus.publish({
                "type": "peek",
                "hdr": "🧠 LLM 实际识别证据（运行时）",
                "crop": crop_b64,
                "full": boxed_full or full_b64,
                "region": [round(float(v), 4) for v in (region or [])],
                "screen_region": [round(float(v), 4) for v in (screen_region or [])],
                "ocr_text": ocr_text,
                "tip": "这就是运行时送进 LLM 的裁剪图 + 模型识别结果。裁剪清晰但字段为空 → 模型/提示词问题；裁剪截歪 → 坐标问题。",
            })
        except Exception as e:  # noqa: BLE001
            self.bus.publish({"type": "log", "level": "WARN", "msg": f"LLM 证据记录失败: {e}"})

    def _act_swipe(self, step, target):
        d = target.get("direction", "up")
        dist = target.get("distance")
        if dist is not None:
            try:
                dist = float(dist)
            except (TypeError, ValueError):
                dist = None
        self.device.swipe(direction=d, distance_px=dist)
        self.bus.publish({"type": "log", "level": "INFO",
                          "msg": f"滑动 {d}" + (f" 距离≈{int(dist)}px" if dist else "（默认距离）")})
        return {"swipe": d, "distance": dist}

    def _act_ocr_extract(self, step, target):
        region = target.get("region")
        fields = target.get("fields", {})
        names = list(fields.keys())
        best_result = {k: "" for k in names}
        best_score = 0
        for attempt in range(3):
            img = self.device.screenshot()
            res = self.vision.ocr_extract(region, fields, round=getattr(self.device, "card", 0), img=img)
            score = sum(1 for v in res.values() if v)
            if score > best_score:
                best_score = score
                best_result = res
            # 所有字段都拿到，无需再试
            if score == len(names):
                break
            if attempt < 2:
                self.bus.publish({"type": "log", "level": "INFO",
                                  "msg": f"OCR 抽取第 {attempt + 1} 次结果不全 ({score}/{len(names)})，0.6s 后重试…"})
                time.sleep(0.6)
        self.bus.publish({"type": "log", "level": "INFO",
                          "msg": "OCR 抽取: " + ", ".join(f"{k}={v}" for k, v in best_result.items())})
        return best_result

    # ---------- 大模型增强节点 ----------
    def _act_llm_judge(self, step, target):
        """视觉判定节点：截图发给大模型分类，未命中期望则执行 on_fail 并跳过本轮剩余步骤。

        典型用法（搜索结果可能是公众号/网页而非小程序）：
          {"action":"llm_judge","target":{"prompt":"这是小程序页面吗？",
           "choices":["yes","no"],"expect":"yes"},"on_fail":[{"action":"back"}]}
        """
        if self.brain is None or not hasattr(self.brain, "judge"):
            raise RuntimeError("当前大脑不支持 judge（请使用 uitars / llm 大脑，而非 mock）")
        prompt = target.get("prompt", "Is this the expected page?")
        choices = target.get("choices") or ["yes", "no"]
        expect = target.get("expect", choices[0])
        shot = self.device.snapshot_b64()
        verdict = self.brain.judge(shot, prompt, choices, expect)
        self.bus.publish({
            "type": "log", "level": "INFO",
            "msg": f"LLM 判定: {prompt} → 「{verdict}」（期望「{expect}」）",
        })
        if verdict != expect:
            self.bus.publish({"type": "log", "level": "WARN",
                              "msg": f"判定未通过（{verdict}≠{expect}），执行跳过逻辑并结束本轮"})
            for s in (step.get("on_fail") or []):
                self._exec_step(s)
            self._skip_body = True
        return {"verdict": verdict, "skipped": verdict != expect}

    def _shot_and_crop(self, region):
        """按 region（归一化 0..1，相对当前截图画布）截图并裁剪到选中范围，返回送 LLM 的 base64。
        供 _act_llm_extract 与 _act_peek_extract 共用。设 _region（安全区）时 region 是子图
        归一化，须先用 region_to_screen_norm 映射回全屏再裁剪（与 P0 tap_norm 同源）。"""
        return shot_and_crop(self.device, region)

    def _draw_box(self, shot_b64, region_norm):
        """在全屏截图上画红框标出 region 位置（调试用），失败回退原图。"""
        return draw_box(shot_b64, region_norm)

    def _act_peek_extract(self, step, target):
        """调试工具：把「送进 LLM 识别的那张裁剪图」单独抽出来展示。纯诊断，不写 vars/不落库。"""
        region = target.get("region")
        return do_peek(self.device, self.vision, self.bus, region)

    def _act_llm_extract(self, step, target):
        """视觉识别节点：从截图中抽取结构化字段（场地名/地址/标签等），结果写入变量。

        用法示例：
          {"action":"llm_extract","target":{"region":[x,y,w,h],"fields":{"venue":"场地名称","address":"地址"}}}

        实现说明：
          以前直接调用 brain.extract()，让 3B 模型输出 JSON，实测极不稳定（常漏字/格式错乱）。
          现在改走 vision.ocr_extract 的两阶段引擎：先转写所有可见文字，再用确定性规则抽取字段。
          链接类字段（link）不是屏幕文字，跳过模型识别，由 emit 自动从剪贴板补齐。
        """
        fields = target.get("fields") or {}
        if isinstance(fields, str):  # 兼容手写/旧版把字段写成多行文本
            parsed = {}
            for line in fields.splitlines():
                if ":" in line or "：" in line:
                    sep = ":" if ":" in line else "："
                    k, _, v = line.partition(sep)
                    k, v = k.strip(), v.strip()
                    if k:
                        parsed[k] = v
            fields = parsed
        if not fields:
            return {}

        # 截图前先把目标 App（默认微信）带到最前，并进一步激活其非主窗口（如小程序窗口）。
        focus = getattr(self, "focus_app", None) or "WeChat"
        try:
            if hasattr(self.device, "activate_app"):
                self.device.activate_app(focus)
                time.sleep(0.4)
            if hasattr(self.device, "activate_frontmost_window"):
                self.device.activate_frontmost_window(focus, skip_names=["微信", "WeChat"])
        except Exception:  # noqa: BLE001
            pass

        # 诊断：记录截图时最前窗口
        front = ""
        try:
            if hasattr(self.device, "frontmost_info"):
                front = self.device.frontmost_info() or ""
        except Exception:  # noqa: BLE001
            front = ""
        region = target.get("region")

        # 区分"屏幕可读文字字段"与"剪贴板字段"。link 不是屏幕文字，必须靠复制链接后读剪贴板。
        text_fields = {}
        for k, v in fields.items():
            kl = str(k).lower()
            vl = str(v).lower()
            if kl == "link" or "链接" in vl or "link" in kl:
                self.bus.publish({"type": "log", "level": "INFO",
                                  "msg": f"LLM 识别：字段「{k}」是链接/剪贴板字段，跳过模型文字识别，"
                                         "留空给 emit 从剪贴板补齐"})
            else:
                text_fields[k] = v

        res = {}
        last_shot = None  # 调试证据：保留最后一次送进模型的裁剪图（Task #49）
        # 优先走稳健的 vision.ocr_extract（两阶段：转写 + 确定性解析）
        if self.vision is not None and hasattr(self.vision, "ocr_extract") and text_fields:
            from PIL import Image as _I
            import base64 as _b64, io as _io
            best = {k: "" for k in text_fields}
            best_score = 0
            for attempt in range(2):
                shot = self._shot_and_crop(region)
                if not shot:
                    self.bus.publish({"type": "log", "level": "ERROR",
                                      "msg": "LLM 识别：截图为空——多半是 macOS「屏幕录制」权限未授权，"
                                             "请到 系统设置→隐私与安全性→屏幕录制 勾选 Terminal/Python 后重试。"})
                    break
                last_shot = shot
                try:
                    img = _I.open(_io.BytesIO(_b64.b64decode(shot))).convert("RGB")
                except Exception as e:
                    self.bus.publish({"type": "log", "level": "ERROR",
                                      "msg": f"LLM 识别：解码截图失败: {e}"})
                    break
                sub = self.vision.ocr_extract(region=None, fields=text_fields, img=img)
                score = sum(1 for x in sub.values() if x)
                self.bus.publish({"type": "log", "level": "INFO",
                                  "msg": f"LLM 识别(两阶段) 第{attempt + 1}次: {sub} "
                                         f"({score}/{len(text_fields)})；最前窗口={front or '未知'}"})
                if score > best_score:
                    best_score = score
                    best = sub
                if score == len(text_fields):
                    break
                if attempt < 1:
                    time.sleep(0.6)
            res = best
            # 调试证据：把"送进 LLM 的裁剪图 + 全屏 + 识别结果"落盘并推到前端（Task #49）
            # 始终推送：用户每次运行都能在 🔍调试 面板直接看到模型当时看到的截图与识别结果。
            if last_shot:
                try:
                    full = self.device.full_snapshot_b64() or ""
                    screen_region = self.device.region_to_screen_norm(region) if hasattr(self.device, "region_to_screen_norm") else (region or [])
                    self._save_llm_evidence(region, screen_region, last_shot, full, text_fields, best)
                except Exception as e:  # noqa: BLE001
                    self.bus.publish({"type": "log", "level": "WARN", "msg": f"LLM 识别证据落盘失败: {e}"})
        elif self.brain is not None and hasattr(self.brain, "extract"):
            # 兜底：没有 vision 时使用旧 brain.extract（仍可能不稳定）
            shot = self._shot_and_crop(region)
            res = self.brain.extract(shot, fields) or {}
            self.bus.publish({"type": "log", "level": "INFO",
                              "msg": f"LLM 识别(brain.extract 兜底): {res}；最前窗口={front or '未知'}"})
        else:
            self.bus.publish({"type": "log", "level": "ERROR",
                              "msg": "LLM 识别：未配置 vision 或 brain.extract 能力"})
            return {}

        # 模型/规则偶尔会用"描述"当键返回，这里做一次"描述→变量名"兜底映射。
        if isinstance(fields, dict) and fields:
            desc_to_key = {str(v).strip(): k for k, v in fields.items()}
            remapped = {}
            for rk, rv in res.items():
                rk_s = str(rk).strip()
                if rk_s in fields:            # 已是变量名
                    remapped[rk_s] = rv
                elif rk_s in desc_to_key:     # 描述作键 → 映射回变量名
                    remapped[desc_to_key[rk_s]] = rv
                else:
                    remapped[rk_s] = rv
            res = remapped

        # 字段名即变量名，自动写入 vars（后续 emit 用 {{字段}} 引用）
        for k, v in res.items():
            self.vars[k] = v
        if not res:
            self.bus.publish({
                "type": "log", "level": "WARN",
                "msg": "LLM 识别: （空）未识别到文字字段——请确认：① OCR=lm 已启用；"
                       "② 该步骤 region 确实框住了场地名/地址文字；③ 模型可达；"
                       f"④ 截图时最前窗口为「{front or '未知'}」。",
            })
        else:
            self.bus.publish({
                "type": "log", "level": "INFO",
                "msg": "LLM 识别结果: " + ", ".join(f"{k}={v}" for k, v in res.items()),
            })
        return res

    def _act_wait(self, step, target):
        """等待节点：纯延时，或等到"屏幕发生变化"（弹窗/页面跳转出现）再继续。

        典型用法（消除时序竞争）：在触发跳转/弹窗的步骤之后放一个 wait：
          {"action":"wait","target":{"until_change":true,"seconds":5}}
        直到 screen_fp 与点击前不同（说明弹窗已出现）或超时 seconds 秒；变化后再稳 0.3s
        等动效结束。无截图能力的设备（Mock）screen_fp 恒为空串，退化为纯延时。"""
        secs = float(target.get("seconds", 2))
        if target.get("until_change"):
            pre = self.device.screen_fp()
            t0 = time.time()
            changed = False
            while time.time() - t0 < secs:
                time.sleep(0.1)
                now = self.device.screen_fp()
                if pre != "" and now != "" and now != pre:
                    changed = True
                    break
            if changed:
                time.sleep(0.3)  # 等弹窗动效收尾
            self.bus.publish({
                "type": "log", "level": "INFO",
                "msg": f"等待屏幕变化: {'已出现，继续' if changed else f'超时未变化({secs}s)'}"
            })
        else:
            time.sleep(secs)
            self.bus.publish({"type": "log", "level": "INFO", "msg": f"等待 {secs}s"})
        return {"waited": secs, "changed": target.get("until_change") and changed}

    def _act_copy_link(self, step, target):
        link = self.device.copy_link()
        # 自动把复制到的链接写入 {{link}} 变量，供后续 emit 的 {{link}} 引用合并入库。
        # 仅在有内容时覆盖，避免空剪贴板把上一轮的好链接冲掉。
        if link:
            self.vars["link"] = link
        self.bus.publish({"type": "log", "level": "INFO", "msg": f"复制链接: {link or '(空)'}"})
        return {"link": link}

    def _act_back(self, step, target):
        self.device.back()
        self.bus.publish({"type": "log", "level": "INFO", "msg": "返回"})
        return {"back": True}

    def _act_emit(self, step, target):
        rec = self.render_obj(step.get("record") or {})
        # 链接兜底：用户常直接用小程序自带的"复制链接"（一个 tap 步骤）而非本工具的 copy_link 动作，
        # 此时 vars["link"] 从未被设置，{{link}} 会原样留成字面量 → 链接漏填。
        # 这里若链接未解析出实际值，直接读系统剪贴板补全（剪贴板里就是刚复制的链接）。
        link_val = rec.get("link")
        if not link_val or "{{" in str(link_val):
            try:
                cb = self.device.copy_link()
                # 校验：只接受看起来像小程序链接的内容，拒绝错误信息/旧链接
                if cb and self._looks_like_miniprogram_link(cb):
                    rec["link"] = cb
                    self.vars["link"] = cb
                    self.bus.publish({"type": "log", "level": "INFO",
                                      "msg": f"emit 检测到链接未解析，已从剪贴板补齐: {cb[:60]}…"})
                elif cb:
                    self.bus.publish({"type": "log", "level": "WARN",
                                      "msg": f"emit 剪贴板内容不像小程序链接（已拒绝）: {cb[:80]}"})
            except Exception:
                pass
        rec.setdefault("ts", time.strftime("%H:%M:%S"))
        rec.setdefault("source", self.chain.get("name"))
        self.data.append(rec)
        _persist_library(rec)  # 持久化到 data/library.jsonl，跨运行保留（GET /library）
        self.bus.publish({"type": "data", "record": rec})
        self.bus.publish({"type": "log", "level": "INFO", "msg": f"落库: {rec.get('venue')} / {rec.get('link')}"})
        return rec

    def _act_capture(self, step, target):
        """截图保存动作（纯采集，不调用任何模型）：把当前详情页截图（或框选区域）存到 captures/last/。

        用途：用户选择"离线识图"工作流——链路只负责遍历列表、逐页截图，截图打包后
        交给外部大模型统一识别并整理成表格，避免实时调用本地小模型带来的抖动与慢速。
        run() 开始时已清空 captures/last/，这里按捕获顺序命名 round_NNN.png。

        区域裁剪：若 step.target.region = [x,y,w,h]（归一化 0..1，相对当前截图），则只保存
        框选的那块区域，避免整屏过大。坐标与标注框同一坐标系，直接按 PNG 像素尺寸换算即可
        （device.screenshot() 返回的就是当前安全区截图，与标注参考图同坐标系）。"""
        import os as _os, shutil as _shutil
        cap_dir = getattr(self, "_capture_dir", None)
        count = getattr(self, "_capture_count", 0)
        if cap_dir is None:
            cap_dir = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                "captures", "last",
            )
            try:
                if _os.path.isdir(cap_dir):
                    _shutil.rmtree(cap_dir)
            except Exception:  # noqa: BLE001
                pass
            _os.makedirs(cap_dir, exist_ok=True)
            self._capture_dir = cap_dir
            count = 0
        count += 1
        self._capture_count = count
        src = self.device.screenshot()
        dst = _os.path.join(cap_dir, f"round_{count:03d}.png")
        # 区域裁剪（若用户在标注里框了区域）
        region = None
        if isinstance(target, dict):
            rg = target.get("region")
            if isinstance(rg, (list, tuple)) and len(rg) == 4:
                try:
                    region = [float(v) for v in rg]
                except (TypeError, ValueError):
                    region = None
        try:
            if _os.path.exists(src):
                if region:
                    from PIL import Image as _PIL
                    with _PIL.open(src) as _im:
                        _w, _h = _im.size
                        # 先在归一化空间算好四边，再乘像素尺寸（避免把像素 int 与归一化 float 混比）
                        _nx = max(0.0, min(region[0], 1.0))
                        _ny = max(0.0, min(region[1], 1.0))
                        _nx2 = max(_nx, min(region[0] + region[2], 1.0))
                        _ny2 = max(_ny, min(region[1] + region[3], 1.0))
                        _x = int(_nx * _w); _y = int(_ny * _h)
                        _x2 = int(_nx2 * _w); _y2 = int(_ny2 * _h)
                        if _x2 - _x < 2 or _y2 - _y < 2:
                            # 区域退化（框太小/未拖框）→ 退回整屏，避免 PIL 裁出空图报错
                            self.bus.publish({"type": "log", "level": "WARN",
                                              "msg": f"截图区域过小({_x},{_y}→{_x2},{_y2})，退回整屏保存"})
                            _shutil.copy(src, dst)
                        else:
                            _im.crop((_x, _y, _x2, _y2)).save(dst)
                            self.bus.publish({"type": "log", "level": "INFO",
                                              "msg": f"区域截图已保存: {dst}（第 {count} 张，裁剪 {_x},{_y}→{_x2},{_y2}）"})
                else:
                    _shutil.copy(src, dst)
                    self.bus.publish({"type": "log", "level": "INFO",
                                      "msg": f"整屏截图已保存: {dst}（第 {count} 张）"})
            else:
                self.bus.publish({"type": "log", "level": "ERROR",
                                  "msg": f"截图保存失败：源文件不存在 {src}（可能是「屏幕录制」权限未授权）"})
                return {"path": ""}
        except Exception as e:  # noqa: BLE001
            self.bus.publish({"type": "log", "level": "ERROR", "msg": f"截图保存失败: {e}"})
            return {"path": ""}
        return {"path": dst, "index": count}

    @staticmethod
    def _looks_like_miniprogram_link(text):
        """判断剪贴板内容是否像一个小程序链接（而非错误信息/旧数据）。"""
        import re as _re
        text = text.strip()
        # 正常小程序链接格式: #小程序://xxx/yyy 或 https://.../xxx
        if _re.match(r'^#小程序://', text):
            return True
        if _re.match(r'^https?://\w+\.weixin\.qq\.com', text):
            return True
        # 明确不是链接的（错误信息、JSON 等）
        if len(text) > 120:
            return False
        if _re.match(r'^(message|error|traceback|exception)', text, _re.I):
            return False
        if '{' in text and '}' in text:
            return False
        # 短文本且不含明显非链接特征 → 放行（可能是短链等）
        return bool(text)

    def _act_subchain(self, step, target):
        ref = step.get("ref")
        sub = self.subchains.get(ref)
        if not sub:
            self.bus.publish({"type": "log", "level": "ERROR", "msg": f"子链不存在: {ref}"})
            return {}
        self.bus.publish({"type": "log", "level": "INFO", "msg": f"进入子链 {ref}"})
        for s in sub.get("steps", []):
            self._checkpoint()
            if self.control.mode == "stopped":
                raise StopExecution()
            self._exec_step(s)
            self._sleep(s)
            if self._skip_body:
                self._skip_body = False
                break
        self.bus.publish({"type": "log", "level": "INFO", "msg": f"退出子链 {ref}"})
        return {}

    def _act_loop(self, step, target):
        until = step.get("until") or {}
        body = step.get("body") or []
        max_rounds = until.get("max_rounds")
        no_new_cards = until.get("no_new_cards", 3)
        rounds = 0
        no_new = 0  # 连续"屏幕无变化"的轮数计数
        self.bus.publish({"type": "log", "level": "INFO", "msg": "进入循环（列表遍历）"})
        while True:
            if max_rounds and rounds >= max_rounds:
                self.bus.publish({"type": "log", "level": "INFO", "msg": f"达到最大轮次 {max_rounds}，结束"})
                break
            # 翻页判定：已跑过至少一轮、且连续 no_new_cards 轮屏幕都无变化，才判定到列表末尾提前结束。
            # 单次无变化（如某次滑动未生效）不再直接 break，避免"只截 1~3 张就停"。
            if rounds > 0 and not self.device.can_scroll():
                no_new += 1
                if no_new >= no_new_cards:
                    self.bus.publish({"type": "log", "level": "INFO",
                                      "msg": f"连续 {no_new} 轮屏幕无变化，判定已到列表末尾，结束循环"})
                    break
            else:
                no_new = 0
            rounds += 1
            self.bus.publish({"type": "progress", "round": rounds, "max": max_rounds or 0})
            # 每轮开始：清空剪贴板 + 重置 link 变量，防止上一轮的旧链接/错误信息被复用
            try:
                self.device.clear_clipboard()
            except Exception:
                pass
            self.vars.pop("link", None)
            for s in body:
                self._checkpoint()
                if self.control.mode == "stopped":
                    raise StopExecution()
                self._exec_step(s)
                self._sleep(s)
                if self._skip_body:
                    self._skip_body = False
                    self.bus.publish({"type": "log", "level": "INFO", "msg": "判定跳过：结束本轮剩余步骤"})
                    break
            if max_rounds and rounds >= max_rounds:
                self.bus.publish({"type": "log", "level": "INFO", "msg": f"达到最大轮次 {max_rounds}，结束"})
                break
        self.bus.publish({"type": "log", "level": "INFO", "msg": f"循环完成，共 {rounds} 轮"})
        return {"rounds": rounds}


# ============================================================
# 模块级工具函数：Interpreter 方法与调试面板（do_control.peek）共用
# ============================================================

def crop_to_region(shot_b64, region_norm):
    """把全屏 base64 截图按归一化区域 [x,y,w,h] 裁剪成只含选中范围的图，返回新 base64。

    标注模式下用户框出"场地名+地址"那一小块，若不裁剪就发整屏给模型，
    模型要在满屏文字里找目标，极易漏识别。裁到选中范围后识别率显著提升。
    失败（无图/坏图/越界）一律回退原图，绝不中断流程。
    模块级函数：Interpreter 与调试 peek 共用。"""
    if not shot_b64 or not region_norm:
        return shot_b64
    try:
        import base64 as _b64
        import io
        from PIL import Image
        data = _b64.b64decode(shot_b64)
        img = Image.open(io.BytesIO(data))
        W, H = img.size
        x, y, w, h = region_norm
        px = int(round(x * W)); py = int(round(y * H))
        pw = max(1, int(round(w * W))); ph = max(1, int(round(h * H)))
        px = max(0, min(px, W - 1)); py = max(0, min(py, H - 1))
        pw = max(1, min(pw, W - px)); ph = max(1, min(ph, H - py))
        crop = img.crop((px, py, px + pw, py + ph))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return _b64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return shot_b64


def shot_and_crop(device, region):
    """按 region（归一化 0..1，相对当前截图画布）截图并裁剪到选中范围，返回送 LLM 的 base64。

    供 llm_extract 与调试 peek 共用。设 _region（安全区）时 region 是子图
    归一化，须先用 region_to_screen_norm 映射回全屏再裁剪，否则框选整体偏移（与 P0 tap_norm 同源）。"""
    shot = device.full_snapshot_b64() or device.snapshot_b64()
    if region:
        screen_region = getattr(device, "region_to_screen_norm", lambda r: r)(region)
        shot = crop_to_region(shot, screen_region)
    return shot


def draw_box(shot_b64, region_norm):
    """在全屏截图上画红框标出 region 位置（调试用），失败回退原图。模块级函数。"""
    if not shot_b64 or not region_norm:
        return shot_b64
    try:
        import base64 as _b64, io
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(_b64.b64decode(shot_b64))).convert("RGB")
        W, H = img.size
        x, y, w, h = region_norm
        px = int(round(x * W)); py = int(round(y * H))
        pw = max(1, int(round(w * W))); ph = max(1, int(round(h * H)))
        d = ImageDraw.Draw(img)
        lw = max(3, int(W / 240))
        for off in range(lw):
            d.rectangle([px + off, py + off, px + pw - 1 - off, py + ph - 1 - off],
                        outline=(255, 0, 0))
        buf = io.BytesIO(); img.save(buf, format="PNG")
        return _b64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return shot_b64


def do_peek(device, vision, bus, region):
    """调试工具核心：把「送进 LLM 识别的那张裁剪图」单独抽出来展示，用于一眼区分两类问题：
      - 截图截歪了（坐标错）：全屏红框位置与用户框选不一致；
      - LLM 识别错了（文字读不出）：红框对，但框内文字模型就是转写不出/错。
    纯诊断：不写 vars、不落库、不改任何数据。模块级函数，供 _act_peek_extract 与调试面板共用。"""
    if not region:
        bus.publish({"type": "log", "level": "ERROR",
                    "msg": "查看识别范围：缺少 region——请先在画布上拖一个框再运行"})
        return {}
    screen_region = getattr(device, "region_to_screen_norm", lambda r: r)(region)
    crop = shot_and_crop(device, region)
    full = device.full_snapshot_b64() or device.snapshot_b64()
    full_box = draw_box(full, screen_region)
    # 该范围内的 OCR 原始转写（仅展示，不写 vars）：红框对但读不出 → 识别问题
    ocr_text = ""
    if crop and vision is not None and hasattr(vision, "ocr_extract"):
        try:
            import base64 as _b64, io as _io
            from PIL import Image as _I
            img = _I.open(_io.BytesIO(_b64.b64decode(crop))).convert("RGB")
            # 诊断用途：不指定字段 → 直接做纯转写，展示"框内到底有什么文字"。
            # 注意：ocr_extract(fields={}) 会在进入转写前就因 names 为空而返回 {}，
            # 所以这里绕过它，直接调用底层 _transcribe_lines 拿到原始逐行文字（Task #50）。
            if hasattr(vision, "_transcribe_lines"):
                lines = vision._transcribe_lines(img) or []
                ocr_text = "\n".join(lines) or "(该范围内模型未转出任何文字——可能页面未加载/被遮挡/不在微信窗口)"
            else:
                sub = vision.ocr_extract(region=None, fields={}, img=img) or {}
                ocr_text = "\n".join(f"{k}={v}" for k, v in sub.items() if v) or "(该范围内无识别文字)"
        except Exception as e:
            ocr_text = f"(OCR 转写失败: {e})"
    bus.publish({
        "type": "peek",
        "crop": crop,
        "full": full_box,
        "region": [round(float(v), 4) for v in region],
        "screen_region": [round(float(v), 4) for v in screen_region],
        "ocr_text": ocr_text,
        "msg": "① 全屏红框=实际送 LLM 的裁剪位置；② 下图为裁剪内容；③ 最下方为 OCR 原始转写（仅诊断，不入库）。"
               "红框与你框选一致但读不出→识别问题；红框偏了→截歪（坐标问题）",
    })
    return {"peek": True}
