"""GUI Agent：探索态 → 合成确定性链路 → 执行态重放。

闭环（PRD §5）：
  Perceive（设备.perceive 结构树）→ Plan（brain.decide）→ Act（_exec 执行设备动作）
  → Verify（重感知校验）→ Memory（trace 记录）。
首轮探索得到 trace，合成为与 DSL 同构的 chain.json；之后交给原 Interpreter 重放，
实现「Agent 生成 RPA、之后零模型调用免费复用」（PRD 成本杠杆 L1）。
"""
import json
import os
import time

from engine.runtime import StopExecution
from engine.loader import load_chain

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYNTH_PATH = os.path.join(BASE, "chains", "agent_synth.json")


class Agent:
    def __init__(self, device, vision, brain, bus, control):
        self.device = device
        self.vision = vision
        self.brain = brain
        self.bus = bus
        self.control = control
        self.trace = []          # [{perception, action, result}]
        self.synth_chain = None
        self.phase = "idle"
        self.goal = ""
        self._vars = {}          # 探索期变量（ocr→copy_link→emit 用）

    # ============ 探索态 ============
    def explore(self, goal, max_steps=60):
        self.goal = goal
        self.trace = []
        self._vars = {}
        if hasattr(self.brain, "reset"):
            self.brain.reset()
        self.phase = "explore"
        self.bus.publish({"type": "status", "state": "exploring"})
        self.bus.publish({"type": "agent", "phase": "explore",
                          "msg": f"目标：{goal}"})
        self.bus.publish({"type": "agent", "phase": "explore",
                          "msg": "感知→规划→执行→校验 闭环启动（探索态：每步由大脑实时决策）"})
        steps = 0
        try:
            while steps < max_steps:
                if self.control.mode == "stopped":
                    raise StopExecution()
                # 暂停支持：被暂停时阻塞，直到继续或停止
                while self.control.mode == "paused":
                    if self.control.mode == "stopped":
                        raise StopExecution()
                    time.sleep(0.1)
                perception = self.device.perceive()
                # 仅「截屏模式(MacDevice 等)」下，screenshot 缺失才视为权限问题并安全停止；
                # 结构树模式(MockDevice 等)本就无 screenshot 字段，不应因此中断演示/测试。
                perception_modality = perception.get("modality")
                if perception_modality == "screenshot" and not perception.get("screenshot"):
                    self.bus.publish({"type": "log", "level": "ERROR",
                                      "msg": "截屏为空/无效：请检查 macOS「屏幕录制」权限——"
                                             "系统设置→隐私与安全性→屏幕录制，勾选运行 python3 app.py 的「终端」；"
                                             "改完须彻底退出并重启『终端 + python3 app.py』才能生效（权限变更不会热生效）"})
                    self.bus.publish({"type": "agent", "phase": "explore_done",
                                      "msg": "截屏为空，已安全停止探索"})
                    break
                shot = perception.get("screenshot")
                if shot:
                    self.bus.publish({"type": "screen", "image": shot})
                action = self.brain.decide(goal, perception, self.trace)
                if action.get("action") == "stop_explore":
                    self.bus.publish({"type": "agent", "phase": "explore_done",
                                      "msg": action.get("reason", "探索完成")})
                    break
                # 推理播报：大脑为什么这么决策
                self.bus.publish({
                    "type": "agent", "phase": "think",
                    "page": perception.get("page"),
                    "action": action.get("action"),
                    "reason": action.get("reason", ""),
                    "perception": perception,
                })
                # 执行
                result = self._exec(action)
                # 校验：执行后重感知，确认页面符合预期（Verify）
                after = self.device.perceive()
                self.bus.publish({
                    "type": "agent", "phase": "verify",
                    "before": perception.get("page"),
                    "after": after.get("page"),
                    "ok": after.get("page") is not None,
                })
                self.trace.append({"perception": perception, "action": action, "result": result})
                steps += 1
                time.sleep(self.control.speed)
            # 合成确定性链路
            self.synth_chain = self.synthesize()
            self._write_chain(self.synth_chain)
            self.phase = "synthesized"
            if not self.synth_chain.get("steps"):
                self.bus.publish({"type": "log", "level": "WARN",
                                  "msg": "合成链路为空：探索未产生有效步骤——请核查截屏与模型输出"
                                         "（见「Agent 推理」「实时日志」；若画面空白多为屏幕录制权限问题）"})
            self.bus.publish({
                "type": "agent", "phase": "synthesized",
                "msg": f"已合成确定性链路：{len(self.synth_chain.get('steps', []))} 顶级步骤（含循环），"
                      f"写入 {os.path.basename(SYNTH_PATH)}",
                "chain": self.synth_chain,
            })
            self.bus.publish({"type": "status", "state": "synthesized"})
        except StopExecution:
            self.bus.publish({"type": "status", "state": "stopped"})
        except Exception as e:  # noqa: BLE001
            self.bus.publish({"type": "log", "level": "ERROR", "msg": f"探索异常: {e}"})
            self.bus.publish({"type": "status", "state": "error"})
        return self.trace

    # ============ 动作执行（与 Interpreter 同源动作，但由 Agent 逐条驱动）============
    def _exec(self, action):
        a = action.get("action")
        t = action.get("target") or {}
        sid = f"a{len(self.trace)}"
        self.bus.publish({"type": "step", "id": sid, "action": a,
                          "status": "running", "target": t})
        try:
            if a == "tap_text":
                self.device.tap_text(t.get("text"))
                res = {"tapped": t.get("text")}
            elif a == "tap_image":
                self.device.tap_image(t.get("template"))
                res = {"tapped_image": t.get("template")}
            elif a == "tap_coord":
                self.device.tap_coord(t.get("x"), t.get("y"))
                res = {"tapped_coord": True}
            elif a == "swipe":
                self.device.swipe(t.get("direction", "up"))
                res = {"swipe": t.get("direction", "up")}
            elif a == "back":
                self.device.back()
                res = {"back": True}
            elif a == "copy_link":
                link = self.device.copy_link()
                res = {"link": link}
            elif a == "type_text":
                self.device.type_text(t.get("text", ""), x=t.get("x"), y=t.get("y"))
                res = {"typed": t.get("text")}
            elif a == "ocr_extract":
                img = self.device.screenshot()
                res = self.vision.ocr_extract(
                    t.get("region"), t.get("fields", {}),
                    round=getattr(self.device, "card", 0), img=img,
                )
            elif a == "assert":
                res = {"ok": True}
            else:
                res = {}
                self.bus.publish({"type": "log", "level": "WARN", "msg": f"未实现动作: {a}（跳过）"})

            # save_to：写回探索期变量
            save_to = action.get("save_to") or {}
            if save_to and isinstance(res, dict):
                for fld, var in save_to.items():
                    if fld in res:
                        self._vars[var] = res[fld]

            # 页面状态事件（投屏 + 进度）
            self.bus.publish({"type": "page", "page": self.device.page,
                              "highlight": self.device.highlight,
                              "round": getattr(self.device, "round", 0)})
            self.bus.publish({"type": "step", "id": sid, "action": a,
                              "status": "done", "result": res})

            # 复制链接即落库一条（演示探索期也在产出数据）
            # 字段动态取自探索期捕获的变量 self._vars，不再写死 venue/address/link
            if a == "copy_link" and "link" in res:
                rec = {"link": res["link"], "ts": time.strftime("%H:%M:%S"),
                       "source": "Agent探索"}
                for k, v in self._vars.items():
                    if k not in rec:
                        rec[k] = v
                self.bus.publish({"type": "data", "record": rec})
                self.bus.publish({"type": "log", "level": "INFO",
                                  "msg": f"落库: {rec.get('link')}"})
            return res
        except Exception as e:  # noqa: BLE001
            self.bus.publish({"type": "log", "level": "ERROR", "msg": f"{a} 失败: {e}"})
            self.bus.publish({"type": "step", "id": sid, "action": a, "status": "error"})
            return {}

    # ============ 合成确定性链路 ============
    def synthesize(self):
        """把一轮探索轨迹泛化为可重放的 chain（Agent 生成 RPA）。

        不再写死「球场列表 / venue / address / link」等网球语义：
        - 链路名称与描述取自本次自然语言目标 self.goal；
        - 仅当轨迹真的呈现「滑动 + 抓取」特征时才套 loop，否则线性回放；
        - emit 字段名由探索期实际捕获的变量 self._vars 动态推导（占位符 {{var}}）；
        - 首页断言仅当存在语义化首页（MockDevice 等）才加，截图模式(Mac)无语义页则跳过。
        """
        body = []
        for h in self.trace:
            a = h["action"]
            if a.get("action") == "stop_explore":
                continue
            step = {"action": a.get("action")}
            if a.get("target"):
                step["target"] = a["target"]
            if a.get("save_to"):
                step["save_to"] = a["save_to"]
            body.append(step)

        acts = [b["action"] for b in body]
        swipe_count = acts.count("swipe")
        link_count = acts.count("copy_link")
        is_list = swipe_count >= 1 and link_count >= 1

        # 动态字段：探索期捕获的变量 → emit 占位符；无变量则不出 emit 步
        emit_fields = {k: "{{" + k + "}}" for k in self._vars.keys()}

        steps = []
        if is_list:
            # 列表循环：每轮收尾 = 落库一行 + 滑到下一张
            tail = []
            if emit_fields:
                tail.append({"action": "emit", "record": emit_fields})
            tail.append({"action": "swipe", "target": {"direction": "up"}})
            steps = [
                {"action": "loop",
                 "until": {"type": "scroll_until_end", "max_rounds": 12, "no_new_cards": 3},
                 "body": body + tail},
            ]
        else:
            # 一次性任务：直接线性回放探索轨迹
            if emit_fields:
                body.append({"action": "emit", "record": emit_fields})
            steps = body

        # 首页断言：仅当存在语义化首页（MockDevice 等）才加；
        # 截图模式(Mac)首页恒为 "screen"，无意义，跳过
        home = self.trace[0]["perception"].get("page") if self.trace else None
        if home and home not in ("screen", "unknown", None, ""):
            steps.insert(0, {"action": "assert", "target": {"expect_text": home}})

        return {
            "name": f"Agent 合成链路 · {self.goal or '未命名目标'}",
            "description": "由 GUI Agent 探索态按真实轨迹自动合成（可确定性重放，零模型调用）",
            "vars": {k: "" for k in self._vars.keys()},
            "steps": steps,
        }

    def _write_chain(self, chain):
        import tempfile
        d = os.path.dirname(SYNTH_PATH)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(chain, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SYNTH_PATH)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # ============ 执行态重放 ============
    def replay(self, chain_path=SYNTH_PATH):
        """把合成的链路交给原 Interpreter 重放：确定性、0 模型调用、秒级。"""
        chain = load_chain(chain_path)
        from engine.interpreter import Interpreter
        self.phase = "replay"
        self.bus.publish({"type": "status", "state": "replaying"})
        self.bus.publish({"type": "agent", "phase": "replay",
                          "msg": "执行态重放：原解释器按合成链路确定性执行（0 模型调用 / 0 成本）"})
        interp = Interpreter(chain, {}, self.device, self.vision, self.bus, self.control, self.brain)
        interp.run()
        self.phase = "done"
        return chain


def load_synth_chain():
    if os.path.exists(SYNTH_PATH):
        return load_chain(SYNTH_PATH)
    return None
