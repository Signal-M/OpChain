"""GUI Agent 大脑（模型无关接口）。

设计要点（见《GUI Agent 自动化产品架构设计与 PRD》）：
- 大脑只负责「规划」：输入 目标 + 屏幕感知 + 历史轨迹，输出一个与 DSL step 同构的 action。
- 模型无关：默认 MockBrain（零依赖状态机，离线可跑）；可切换 LLMBrain（云端/本地开源多模态模型）。
- 真实链路：感知(Perceive)→规划(Plan/本文件)→执行(Act/Agent)→校验(Verify/Agent)→记忆(Memory/Agent.trace)。

约定输出格式：
    {"action": "tap_text", "target": {"text": "预订"}, "reason": "进入场地详情", "save_to": {...}}
  或 {"action": "stop_explore", "reason": "..."} 表示一轮探索完成，可以合成链路。
"""
import json
import os
import re
import urllib.request
import urllib.error


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------
class BaseBrain:
    def decide(self, goal, perception, history):
        """返回下一步 action（dict）或 stop_explore。"""
        raise NotImplementedError

    def reset(self):
        """新一轮探索前清理内部状态（无状态大脑可留空）。"""
        pass

    # ---- 大模型视觉理解（供 DSL 的 llm_judge / llm_extract 节点调用）----
    # 默认实现基于多模态 _vqa；各大脑只需实现 _vqa（把图+问题发给模型取文本）。
    # MockBrain 直接离线返回，不调用模型。
    def _vqa(self, question, image_b64):
        raise NotImplementedError

    def judge(self, screenshot, prompt, choices=None, expect=None):
        """视觉判定：把截图 + 问题发给模型，返回 choices 中的一个标签（小写）。"""
        choices = choices or ["yes", "no"]
        q = (
            f"Look at this screenshot. {prompt}. "
            f"Respond with exactly one word from this list: {', '.join(choices)}. "
            f"Output only the word."
        )
        ans = (self._vqa(q, screenshot) or "").strip().lower()
        for c in choices:
            if c.lower() in ans:
                return c
        return ans

    def extract(self, screenshot, fields):
        """视觉识别：把截图 + 字段说明发给模型，返回 {字段名: 值} 的 JSON 字典。"""
        desc = "; ".join(f"{k}: {v}" for k, v in (fields or {}).items())
        q = (
            f"Extract the following fields from this screenshot. "
            f"Respond with a SINGLE JSON object only (no markdown, no explanation). "
            f"The JSON keys MUST be exactly the field names on the LEFT (e.g. venue, address), "
            f"NOT their descriptions on the RIGHT. Fields to extract: {desc}."
        )
        txt = self._vqa(q, screenshot) or ""
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


# --------------------------------------------------------------------------
# Mock：网球约球流程状态机（零依赖、可离线演示）
# --------------------------------------------------------------------------
class MockBrain(BaseBrain):
    """基于当前页 + 历史轨迹推断下一步；历史上出现 copy_link 且回到 list 即视为一轮完成。"""

    def decide(self, goal, perception, history):
        page = (perception or {}).get("page")

        # 一轮完成的标志：曾复制过链接，且当前已回到列表
        if page == "list" and any(
            h.get("action", {}).get("action") == "copy_link" for h in history
        ):
            return {"action": "stop_explore", "reason": "已完成一轮「订场→复制链接」，可合成确定性链路"}

        if page == "list":
            return self._a("tap_text", {"text": "预订"}, "列表页：点击卡片预订，进入详情")

        if page == "detail":
            cyc = self._current_cycle(history)
            if not self._has(cyc, "ocr_extract"):
                return self._a(
                    "ocr_extract",
                    {"region": [0.05, 0.1, 0.9, 0.3],
                     "fields": {"venue": "场地[:：]?\\s*(.+)", "address": "地址[:：]?\\s*(.+)"}},
                    "详情页：OCR 抽取场地名/地址",
                    save_to={"venue": "venue", "address": "address"},
                )
            return self._a("tap_text", {"text": "立即订场"}, "详情页：点击立即订场")

        if page == "confirm":
            return self._a("tap_text", {"text": "确定"}, "确认页：点击确定下单")

        if page == "new_miniapp":
            # 用「上一步动作」精确推进子流程：
            #   确定 → 新小程序：点更多打开菜单
            #   菜单点了复制链接 → 回到新小程序：执行复制
            #   已复制过 → 返回列表
            last = history[-1]["action"] if history else None
            if last and last.get("action") == "copy_link":
                return self._a("back", {}, "已复制链接：返回列表")
            if last and last.get("action") == "tap_text" and last.get("target", {}).get("text") == "复制链接":
                return self._a("copy_link", {}, "复制小程序链接到剪贴板", save_to={"link": "link"})
            return self._a("tap_image", {"template": "templates/more_dots.png", "threshold": 0.85},
                           "新小程序页：点击右上角更多")

        if page == "menu":
            return self._a("tap_text", {"text": "复制链接"}, "菜单：点击复制链接")

        return {"action": "stop_explore", "reason": f"未知页面 {page}，终止探索"}

    # ---- 工具 ----
    @staticmethod
    def _current_cycle(history):
        last = None
        for i, h in enumerate(history):
            a = h.get("action", {})
            if a.get("action") == "tap_text" and a.get("target", {}).get("text") == "预订":
                last = i
        return history[last + 1:] if last is not None else history

    @staticmethod
    def _has(actions, name):
        return any(h.get("action", {}).get("action") == name for h in actions)

    @staticmethod
    def _a(action, target, reason, save_to=None):
        d = {"action": action, "target": target, "reason": reason}
        if save_to:
            d["save_to"] = save_to
        return d

    # ---- 离线视觉理解（演示/调试用，不调用模型）----
    def judge(self, screenshot, prompt, choices=None, expect=None):
        # 默认判定为「通过」：返回期望项（或 choices 首项），便于离线跑通主流程。
        # 传入 expect 时原样返回，方便单测模拟「判定不通过」分支。
        if expect is not None:
            return expect
        if choices:
            return choices[0]
        return "yes"

    def extract(self, screenshot, fields):
        out = {}
        for k in (fields or {}):
            if k == "venue":
                out[k] = "示例网球中心"
            elif k == "address":
                out[k] = "示例市示范区1号"
            else:
                out[k] = f"示例{k}"
        return out


# --------------------------------------------------------------------------
# 模型驱动大脑（云端 / 本地开源，模型无关）
# 默认不启用；设置环境变量 BRAIN=cloud|local 并配置密钥后生效。
# 云端首选阿里云通义千问（DashScope OpenAI 兼容接口），兼容本地 vLLM / Ollama。
# --------------------------------------------------------------------------
class LLMBrain(BaseBrain):
    # 动作白名单（写进 prompt，约束模型输出）
    ACTIONS = ["tap_text", "tap_image", "tap_coord", "type_text", "swipe", "back", "copy_link",
               "ocr_extract", "assert", "stop_explore"]

    def __init__(self, provider="cloud", api_key="", base_url="", model=""):
        self.provider = provider
        # 云端默认：阿里云 DashScope OpenAI 兼容端点
        if provider == "cloud":
            self.base_url = base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            self.model = model or "qwen-plus"
        else:  # 本地开源：UI-TARS / Qwen2.5-VL 经 vLLM 暴露的 OpenAI 兼容接口
            self.base_url = base_url or "http://127.0.0.1:8000/v1"
            self.model = model or "local-model"
        self.api_key = api_key
        # 本地开源模型（Ollama/vLLM）通常不需要密钥；云端（DashScope 等）才需要。
        if provider != "local" and not self.api_key:
            raise RuntimeError(
                "LLMBrain 未配置密钥：云端需设置 BRAIN_API_KEY（及 BRAIN_BASE_URL/BRAIN_MODEL），"
                "本地(Ollama)可留空；或改用 BRAIN=mock 离线运行"
            )
        # 本地模型允许空密钥，统一补一个占位值，避免 Authorization 头缺失
        if not self.api_key:
            self.api_key = "ollama"

    def decide(self, goal, perception, history):
        system = (
            "你是手机 GUI 自动化 Agent 的规划大脑。根据「目标 + 当前屏幕感知 + 历史轨迹」，"
            "决定下一步操作。只输出一个 JSON 对象，格式："
            '{"action": <动作名>, "target": <参数对象>, "reason": <一句话理由>, "save_to": <可选>};'
            f"可用动作：{', '.join(self.ACTIONS)}。"
            "当目标已通过一轮完整流程达成（如已复制链接并回到列表），输出 "
            '{"action":"stop_explore","reason":"..."}。'
            "target 示例：tap_text→{\"text\":\"预订\"}；tap_image→{\"template\":\"...\",\"threshold\":0.85}；"
            "swipe→{\"direction\":\"up\"}；ocr_extract→{\"region\":[x,y,w,h],\"fields\":{...}}。"
        )
        user = self._build_user_message(goal, perception, history)
        try:
            content = self._chat(system, user)
            action = self._parse(content)
            action = self._normalize(action)
        except Exception as e:  # noqa: BLE001
            # 模型不可用时不阻塞原型：回退为安全停止，由前端提示
            return {"action": "stop_explore", "reason": f"模型调用失败: {e}"}
        if action.get("action") not in self.ACTIONS:
            return {"action": "stop_explore", "reason": f"模型返回未知动作: {action.get('action')}"}
        return action

    # 动作同义词归一化：本地小模型（如 qwen2.5vl:3b）常输出 click/tap/scroll 等
    # 非标准动作名，这里映射到 DSL 白名单动作，提升小模型的可用性。
    ACTION_SYNONYMS = {
        "tap_text": "tap_text", "tap": "tap_text", "click": "tap_text",
        "click_text": "tap_text", "tap_on": "tap_text", "press": "tap_text",
        "tap_image": "tap_image", "click_image": "tap_image", "tap_img": "tap_image",
        "tap_coord": "tap_coord", "click_coord": "tap_coord", "click_point": "tap_coord",
        "type_text": "type_text", "type": "type_text", "input": "type_text", "enter": "type_text",
        "swipe": "swipe", "swipe_up": "swipe", "swipe_down": "swipe",
        "scroll": "swipe", "scroll_up": "swipe", "scroll_down": "swipe",
        "back": "back", "go_back": "back", "return": "back", "nav_back": "back",
        "copy_link": "copy_link", "copy": "copy_link", "copy_link_to_clipboard": "copy_link",
        "ocr_extract": "ocr_extract", "ocr": "ocr_extract", "extract": "ocr_extract",
        "assert": "assert", "verify": "assert",
        "stop_explore": "stop_explore", "stop": "stop_explore",
        "finish": "stop_explore", "done": "stop_explore", "end": "stop_explore",
    }

    @staticmethod
    def _normalize(action):
        """把模型输出归一化到白名单动作；无法识别则原样返回（交由 decide 安全停止）。"""
        if not isinstance(action, dict):
            return action
        raw = (action.get("action") or "").strip().lower()
        norm = LLMBrain.ACTION_SYNONYMS.get(raw, raw)
        action["action"] = norm
        # 小模型常漏写 target；对需要 target 的动作补空对象，避免下游 KeyError
        if norm in ("tap_text", "tap_image", "type_text", "ocr_extract", "assert") and not action.get("target"):
            action["target"] = {}
        return action

    @staticmethod
    def _build_user_message(goal, perception, history):
        """构造 user 消息：若感知带截图（真实设备/小程序 webview 场景），
        走多模态（图 + 文本）；否则纯文本（MockDevice 结构化树场景）。"""
        shot = (perception or {}).get("screenshot")
        text = json.dumps(
            {"goal": goal, "perception": perception, "history": history[-12:]},
            ensure_ascii=False,
        )
        if shot:
            url = shot if shot.startswith("data:") else f"data:image/png;base64,{shot}"
            return [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": url}},
            ]
        return text

    def _chat(self, system, user):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    # ---- 视觉理解（VQA）：把图 + 问题发给模型，返回纯文本答案 ----
    def _vqa(self, question, image_b64):
        if not image_b64:
            raise RuntimeError("未提供截图：设备截屏为空")
        url = image_b64 if image_b64.startswith("data:") else f"data:image/png;base64,{image_b64}"
        messages = [
            {"role": "system", "content": "You are a GUI understanding assistant. Answer strictly as requested, no extra text."},
            {"role": "user", "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": url}},
            ]},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 512,
        }
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _parse(content):
        # 容错：优先整段解析，失败则截取第一个 {...}
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            import re
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                return json.loads(m.group(0))
            raise


# --------------------------------------------------------------------------
# UI-TARS 原生协议大脑（本地开源 GUI 模型，Ollama 部署）
# 模型输出 Thought/Action 范式（click(start_box='(x,y)') / scroll / type / finished …），
# 本类负责解析并翻译为引擎 DSL（tap_coord / swipe / type_text / back / copy_link / stop_explore）。
# --------------------------------------------------------------------------
class UITARSBrain(BaseBrain):
    SYSTEM = (
        "You are a GUI automation agent. Given a screenshot and a goal, decide the next "
        "single action to progress toward the goal. Output exactly one action using the format:\n"
        "Thought: <brief reasoning about what you see and what to do next>\n"
        "Action: <action>\n\n"
        "Action space (coordinates are in the range [0,1000] as '(x,y)'):\n"
        "- click(start_box='(x,y)'): tap/click at the coordinate\n"
        "- drag(start_box='(x1,y1)', end_box='(x2,y2)'): drag from start to end\n"
        "- scroll(start_box='(x,y)', direction='up|down|left|right'): scroll\n"
        "- type(start_box='(x,y)', content='text'): focus the field then type the text\n"
        "- back(): navigate back / close a popup / cancel\n"
        "- copy_link(): copy the current share/link to the clipboard\n"
        "- finished(): the goal is fully accomplished; stop\n"
    )

    def __init__(self, api_key="", base_url="", model="ui-tars:7b"):
        self.base_url = base_url or "http://127.0.0.1:11434/v1"
        self.model = model or "ui-tars:7b"
        self.api_key = api_key or "ollama"

    def extract(self, screenshot, fields):
        """UI-TARS 模型被训练为 Thought/Action 动作协议，直接用它做字段抽取会返回坐标而非 JSON。
        此方法用「非动作」系统提示 + 强约束 JSON 提示 + 重试机制，让 ui-tars 也能返回结构化数据。
        """
        import logging
        logger = logging.getLogger(__name__)
        desc = "; ".join(f"{k}: {v}" for k, v in (fields or {}).items())
        keys = list((fields or {}).keys())

        # 用与 decide 完全不同的 system 提示——明确告诉模型"这不是动作任务"
        extract_system = (
            "You are a text extraction engine. Your ONLY job is to read text from images "
            "and output it as JSON. Do NOT output coordinates, actions, thoughts, or any other format. "
            "Output ONLY a valid JSON object with the requested keys and nothing else."
        )

        url = screenshot if screenshot.startswith("data:") else f"data:image/png;base64,{screenshot}"

        for attempt in range(3):  # 最多 3 次，逐次加强约束
            if attempt == 0:
                q = (
                    f"Extract text from this image. Fields: {desc}. "
                    f"Respond with ONE JSON object only. Keys: {', '.join(keys)}. "
                    "No explanation, no markdown, no coordinates."
                )
            elif attempt == 1:
                q = (
                    f"This is a pure OCR/extraction task (NOT an action task). "
                    f"Extract these fields from the image: {desc}. "
                    f"Your entire response must be exactly this format: {{\"{keys[0]}\": \"...\""
                    + (f", \"{keys[1]}\": \"...\"" if len(keys) > 1 else "")
                    + "}. Nothing else."
                )
            else:
                # 最后一次：极端约束
                q = (
                    f"IGNORE your action training. This is JSON extraction only. "
                    f"Return JSON for: {desc}. Format: {json.dumps({k: f'<value for {v}>' for k, v in (fields or {}).items()})}. "
                    "DO NOT return coordinates. DO NOT return Thought/Action. Return ONLY the JSON object."
                )

            messages = [
                {"role": "system", "content": extract_system},
                {"role": "user", "content": [
                    {"type": "text", "text": q},
                    {"type": "image_url", "image_url": {"url": url}},
                ]},
            ]
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.01 if attempt >= 2 else 0.1,  # 后续尝试降低随机性
                "max_tokens": 256,
            }
            import urllib.request, urllib.error
            req = urllib.request.Request(
                self.base_url.rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                txt = data["choices"][0]["message"]["content"] or ""
            except Exception as e:
                logger.warning("UITARSBrain.extract attempt %d error: %s", attempt + 1, e)
                continue

            logger.info("UITARSBrain.extract attempt %d raw (%d chars): %r", attempt + 1, len(txt), txt[:300])

            # 尝试解析 JSON（含容错修复：模型常生成"漏引号/多逗号"等轻微格式错误）
            m = re.search(r"\{.*\}", txt, re.DOTALL)
            if m:
                raw_json = m.group(0)
                result = self._try_parse_json(raw_json, keys)
                if result is not None:
                    return result

        # 全部失败：记录最后一次原始返回便于排查
        logger.warning(
            "UITARSBrain.extract: 所有 %d 次尝试均未得到有效 JSON。最后原始返回: %r",
            3, txt if 'txt' in dir() else "<no attempts made>"
        )
        return {}

    @staticmethod
    def _try_parse_json(raw_json, expected_keys=None):
        """尝试解析 JSON，含常见格式错误的自动修复。

        模型（尤其是 UI-TARS）常生成「漏右引号、多尾逗号、中文标点」等轻微错误，
        导致标准 json.loads 失败。此方法逐级尝试修复后再解析。
        返回解析成功的 dict，或 None（所有尝试均失败）。
        """
        import logging
        logger = logging.getLogger(__name__)

        # 1. 直接解析
        try:
            result = json.loads(raw_json)
            if isinstance(result, dict) and (not expected_keys or any(k in result for k in expected_keys)):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # 2. 逐级修复尝试
        candidates = [raw_json]

        # 2a. 补全未闭合的字符串引号（模型最常见错误： "value → "value"）
        repaired = re.sub(r'"\s*$', '"', raw_json)       # 行尾缺 "
        repaired = re.sub(r'(?<!\\)"([^"]*?)([,\}\]])', r'"\1"\2', repaired)  # 值缺右引号
        candidates.append(repaired)

        # 2b. 删除 }/] 前的尾逗号
        no_trailing = re.sub(r',\s*([}\]])', r'\1', repaired)
        candidates.append(no_trailing)

        # 2c. 中文冒号/分号/括号替换为 ASCII
        ascii_fixed = raw_json.replace("：", ":").replace("；", ";").replace("【", "[").replace("】", "]").replace("\"", "'")
        candidates.append(ascii_fixed)

        for i, cand in enumerate(candidates, 1):
            try:
                result = json.loads(cand)
                if isinstance(result, dict) and (not expected_keys or any(k in result for k in expected_keys)):
                    logger.info("JSON repair: attempt %d succeeded", i)
                    return result
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug("JSON repair: attempt %d failed: %s (text=%r)", i, e, cand[:200])
                continue

        return None

    def decide(self, goal, perception, history):
        shot = (perception or {}).get("screenshot")
        if not shot:
            return {"action": "stop_explore", "reason": "UI-TARS 需要截图感知，但设备未提供 screenshot"}
        try:
            raw = self._chat(goal, perception, history)
            action = self._translate(raw, perception)
            action["reason"] = self._thought(raw) or action.get("reason", "")
            return action
        except Exception as e:  # noqa: BLE001
            return {"action": "stop_explore", "reason": f"UI-TARS 调用失败: {e}"}

    # ---- 对话（多模态，无 json_object 约束，直接吐 Thought/Action 文本）----
    def _chat(self, goal, perception, history):
        w = (perception or {}).get("screen_w") or 0
        h = (perception or {}).get("screen_h") or 0
        hist_lines = []
        for hh in history[-10:]:
            a = (hh.get("action") or {}).get("action")
            t = (hh.get("action") or {}).get("target") or {}
            if a:
                hist_lines.append(f"- {a} {t}")
        hist_text = "\n".join(hist_lines) if hist_lines else "(none)"
        user = (
            f"Goal: {goal}\n"
            f"Screen size (pixels): {w}x{h}\n"
            f"Previous actions this session:\n{hist_text}\n\n"
            "Decide the next single action to progress toward the goal. "
            "Coordinates must be in [0,1000] space."
        )
        shot = (perception or {}).get("screenshot")
        if not shot:
            raise RuntimeError(
                "未提供截图：设备截屏为空（多为 macOS「屏幕录制」权限未授权，"
                "或运行 python3 app.py 的终端未在该权限列表中被勾选）"
            )
        url = shot if shot.startswith("data:") else f"data:image/png;base64,{shot}"
        messages = [
            {"role": "system", "content": self.SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": url}},
            ]},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 512,
        }
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # Ollama 4xx/5xx：把真实错误体透传给前端，便于定位
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            raise RuntimeError(f"Ollama HTTP {e.code}: {body[:400]}")
        return data["choices"][0]["message"]["content"]

    # ---- 视觉理解（VQA）：把图 + 问题发给模型，返回纯文本答案 ----
    def _vqa(self, question, image_b64):
        if not image_b64:
            raise RuntimeError(
                "未提供截图：设备截屏为空（多为 macOS「屏幕录制」权限未授权，"
                "或运行 python3 app.py 的终端未在该权限列表中被勾选）"
            )
        url = image_b64 if image_b64.startswith("data:") else f"data:image/png;base64,{image_b64}"
        messages = [
            {"role": "system", "content": "You are a GUI understanding assistant. Answer strictly as requested, no extra text."},
            {"role": "user", "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": url}},
            ]},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 512,
        }
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")
            except Exception:
                pass
            raise RuntimeError(f"Ollama HTTP {e.code}: {body[:400]}")
        return data["choices"][0]["message"]["content"]

    # ---- 解析 UI-TARS 原生协议 ----
    @staticmethod
    def _thought(raw):
        m = re.search(r"Thought\s*:\s*(.+?)(?=\nAction\s*:|$)", raw,
                      re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _action_block(raw):
        # 优先匹配 "Action: ..." 范式（UI-TARS 标准输出）
        blocks = re.findall(r"Action\s*:\s*(.+)", raw, re.IGNORECASE)
        if blocks:
            return blocks[-1].strip()
        # 退化格式：模型常省略 "Action:" 前缀，直接输出 "0. click(...)" 或 "click(...)"
        # （首次探索时尤其容易出现，导致此前被误判为「无法解析」→ 空链路）
        best = ""
        for line in raw.splitlines():
            low = line.lower()
            if "(" in line and any(k in low for k in _UITARS_KEYWORDS):
                best = line.strip()
        # 去掉行首步骤编号（"0. " / "1) " 等）
        return re.sub(r"^\s*\d+\s*[.)]\s*", "", best)

    @classmethod
    def _translate(cls, raw, perception):
        """把 UI-TARS 原生 Action 翻译为引擎 DSL。

        采用「动词关键词 + 数值坐标」的容错解析：不硬编码引号/空格格式，
        即使模型偶尔省略引号或改动排版也能命中；覆盖 click/tap/drag/scroll/
        type/hover/back/copy_link/finished，并对 wait/hotkey/双击 等做安全空操作。
        """
        block = cls._action_block(raw)
        low = block.lower()
        w = float((perception or {}).get("screen_w") or 0)
        h = float((perception or {}).get("screen_h") or 0)
        nums = [float(n) for n in re.findall(r"\d+\.?\d*", block)]

        def coord(i):
            # 取第 i 对 (x,y)，归一化到像素；坐标 >1000 视为已是像素
            if i + 1 >= len(nums):
                return None, None
            x, y = nums[i], nums[i + 1]
            if x > 1000 and w and h and x <= w and y <= h:
                return x, y
            if w and h:
                return x / 1000.0 * w, y / 1000.0 * h
            return x, y

        if re.search(r"\bfinished\b", low):
            return {"action": "stop_explore", "reason": "UI-TARS 判定目标已完成"}
        if re.search(r"\bback\b", low):
            return {"action": "back", "target": {}}
        if "copy_link" in low:
            return {"action": "copy_link", "target": {}}
        if "drag" in low and len(nums) >= 4:
            x1, y1 = coord(0)
            x2, y2 = coord(2)
            return {"action": "swipe", "target": {"direction": cls._swipe_dir(x1, y1, x2, y2)}}
        mdir = re.search(r"direction\s*=\s*'?(up|down|left|right)'?", low, re.IGNORECASE)
        if "scroll" in low:
            direction = mdir.group(1).lower() if mdir else "down"
            return {"action": "swipe", "target": {"direction": direction}}
        if "type" in low:
            m = re.search(r"content\s*=\s*'(.*?)'", raw, re.IGNORECASE | re.DOTALL)
            t = {"text": m.group(1) if m else ""}
            if len(nums) >= 2:
                x, y = coord(0)
                t["x"], t["y"] = x, y
            return {"action": "type_text", "target": t}
        if ("click" in low or "tap" in low) and len(nums) >= 2:
            x, y = coord(0)
            return {"action": "tap_coord", "target": {"x": x, "y": y}}
        if "hover" in low and len(nums) >= 2:
            x, y = coord(0)
            return {"action": "tap_coord", "target": {"x": x, "y": y}}
        # wait / hotkey / 双击 / 右键 等：安全空操作（assert 在解释器里为无害校验）
        if any(k in low for k in ("wait", "hotkey", "double", "right_single")):
            return {"action": "assert", "target": {}, "reason": "UI-TARS 等待/热键，落为空操作"}
        # 真正无法识别：安全停止，避免乱点
        return {"action": "stop_explore", "reason": f"UI-TARS 输出无法解析: {block[:80]}"}

    @staticmethod
    def _swipe_dir(x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        if abs(dx) > abs(dy):
            return "right" if dx > 0 else "left"
        return "down" if dy > 0 else "up"


# UI-TARS 动作关键词（用于退化格式下从原始输出里定位动作行）
_UITARS_KEYWORDS = (
    "finished", "copy_link", "drag", "scroll", "type",
    "click", "tap", "hover", "back", "wait", "hotkey",
    "double", "right_single",
)


def build_brain(brain=None, api_key=None, base_url=None, model=None):
    """按参数（优先）或环境变量构建大脑。

    参数覆盖环境变量，便于界面「识别模型」下拉框在运行时即时切换，无需重启/改环境变量。
    默认 mock（零依赖，仅返回占位值，不调用模型）。"""
    brain = (brain or os.environ.get("BRAIN", "mock") or "mock").lower()
    if brain in ("mock", "", None):
        return MockBrain()
    if brain in ("cloud", "local", "llm"):
        api_key = api_key if api_key is not None else os.environ.get("BRAIN_API_KEY", "")
        base_url = base_url if base_url is not None else os.environ.get("BRAIN_BASE_URL", "")
        model = model if model is not None else os.environ.get("BRAIN_MODEL", "")
        return LLMBrain(provider=brain if brain != "llm" else "cloud",
                        api_key=api_key, base_url=base_url, model=model)
    if brain in ("uitars", "ui-tars", "uitars2"):
        from engine.brain import UITARSBrain

        api_key = api_key if api_key is not None else os.environ.get("BRAIN_API_KEY", "")
        base_url = base_url if base_url is not None else os.environ.get("BRAIN_BASE_URL", "")
        model = model if model is not None else os.environ.get("BRAIN_MODEL", "ui-tars:7b")
        return UITARSBrain(api_key=api_key, base_url=base_url, model=model)
    # 未知取值回退 mock
    return MockBrain()
