"""视觉感知层（可插拔）。

- MockVision：返回假数据，零依赖，用于演示/调试。
- RealVision：PaddleOCR 离线抽字段 + 定位文字；OpenCV 多尺度模板匹配定位图标。
  重依赖（paddleocr / opencv-python）均懒加载，未安装时不影响 Mock 模式运行。
"""
import os
import re


class BaseVision:
    def ocr_extract(self, region, fields, round=0, img=None):
        raise NotImplementedError

    def locate_text(self, img, text, fuzzy=True):
        """返回 [x, y, w, h] 或 None。"""
        raise NotImplementedError

    def match_template(self, img, template, threshold=0.8):
        """返回 [x, y, w, h] 或 None。"""
        raise NotImplementedError


# --------------------------------------------------------------------------
# Mock
# --------------------------------------------------------------------------
class MockVision(BaseVision):
    VENUES = [
        "阳光网球中心",
        "绿水球场",
        "中央体育馆",
        "北门外球场",
        "湖畔网球公园",
        "城南网球汇",
    ]

    def ocr_extract(self, region, fields, round=0, img=None):
        i = max(round, 1) - 1
        venue = self.VENUES[i % len(self.VENUES)]
        address = f"示例市示范区{round or 1}号"
        return {"venue": venue, "address": address}

    def locate_text(self, img, text, fuzzy=True):
        # Mock 不真正识别，返回一个占位框即可（MockDevice 不走 vision 定位）
        return [100, 100, 120, 40]

    def match_template(self, img, template, threshold=0.8):
        return [200, 40, 40, 40]


# --------------------------------------------------------------------------
# 真实识别：PaddleOCR + OpenCV
# --------------------------------------------------------------------------
class RealVision(BaseVision):
    def __init__(self):
        self._ocr = None
        self._cv2 = None

    # ---- 懒加载 ----
    def _model(self):
        if self._ocr is None:
            from paddleocr import PaddleOCR

            self._ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        return self._ocr

    def _cv(self):
        if self._cv2 is None:
            import cv2

            self._cv2 = cv2
        return self._cv2

    def _run_ocr(self, img):
        """返回 [(text, [x, y, w, h], conf), ...]（归一化到左上角坐标）。"""
        cv = self._cv()
        if isinstance(img, str):
            img_bgr = cv.imread(img)
        else:
            img_bgr = img
        if img_bgr is None:
            return []
        h, w = img_bgr.shape[:2]
        result = self._model().ocr(img_bgr, cls=True)
        lines = []
        if not result:
            return lines
        for block in result:
            if not block:
                continue
            for line in block:
                box = line[0]  # 4 点多边形
                text = line[1][0]
                conf = line[1][1]
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                x, y = int(min(xs)), int(min(ys))
                bw, bh = int(max(xs) - x), int(max(ys) - y)
                lines.append((text, [x, y, bw, bh], conf))
        return lines

    def _crop(self, img, region):
        """region: [x, y, w, h] 归一化(0~1) 或绝对像素。返回裁剪后路径。"""
        if not region:
            return img
        cv = self._cv()
        if isinstance(img, str):
            img_bgr = cv.imread(img)
            src = img
        else:
            img_bgr = img
            src = None
        h, w = img_bgr.shape[:2]
        if all(0 <= v <= 1 for v in region):
            x, y, rw, rh = [int(v * (w if i % 2 == 0 else h)) for i, v in enumerate(region)]
        else:
            x, y, rw, rh = [int(v) for v in region]
        crop = img_bgr[y : y + rh, x : x + rw]
        if src:
            import tempfile

            p = tempfile.mktemp(suffix=".png")
            cv.imwrite(p, crop)
            return p
        return crop

    # ---- 字段抽取 ----
    def ocr_extract(self, region, fields, round=0, img=None):
        if img is None:
            # 真实模式下 img 必须由设备提供；缺失则回退到空
            return {k: "" for k in (fields or {})}
        lines = self._run_ocr(self._crop(img, region))
        out = {}
        for name, pattern in (fields or {}).items():
            pat = re.compile(pattern)
            for text, box, conf in lines:
                m = pat.search(text)
                if m:
                    out[name] = m.group(1) if m.groups() else text
                    break
            if name not in out:
                out[name] = ""
        return out

    # ---- 文字定位 ----
    def locate_text(self, img, text, fuzzy=True):
        if img is None:
            return None
        lines = self._run_ocr(img)
        for t, box, conf in lines:
            if fuzzy:
                if text in t:
                    return box
            else:
                if t == text:
                    return box
        return None

    # ---- 模板匹配 ----
    def match_template(self, img, template, threshold=0.8):
        cv = self._cv()
        if isinstance(img, str):
            scene = cv.imread(img)
        else:
            scene = img
        if not os.path.isabs(template):
            template = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chains", template)
        tpl = cv.imread(template)
        if scene is None or tpl is None:
            return None
        best = None
        best_val = threshold
        # 多尺度匹配，提升分辨率适配鲁棒性
        for scale in [0.8, 0.9, 1.0, 1.1, 1.25]:
            tw, th = int(tpl.shape[1] * scale), int(tpl.shape[0] * scale)
            if tw < 5 or th < 5 or tw > scene.shape[1] or th > scene.shape[0]:
                continue
            resized = cv.resize(tpl, (tw, th))
            res = cv.matchTemplate(scene, resized, cv.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv.minMaxLoc(res)
            if max_val > best_val:
                best_val = max_val
                best = [max_loc[0], max_loc[1], tw, th]
        return best


# --------------------------------------------------------------------------
# 本地多模态模型定位（免装 PaddleOCR 重依赖）
# --------------------------------------------------------------------------
class LMVision(BaseVision):
    """用本地 GUI 专精模型（ui-tars:7b，经 Ollama）做文字定位，免装 PaddleOCR 重依赖。

    关键选型：qwen2.5vl:3b 经实测「读中文」可靠，但空间定位（像素/网格/象限）系统性
    失准，无法用于点击；ui-tars:7b 是 7B GUI 专精模型，原生输出点击坐标(start_box，
    [0,1000] 归一化)，实测对小程序按钮文字定位误差 <0.02（像素级）。

    流程：调用方传「全屏截屏」→ locate_text 内部按需缩小后送 ui-tars 找「文字所在元素」
    并点其中心 → 用 qwen2.5vl 裁命中点周边小图做缺字验证（目标不在屏上则优雅失败）→
    返回命中点像素 bbox，调用方点击。按钮位置随上方标题长度浮动也不怕——全屏扫描，
    命中即点，不依赖固定坐标。
    """

    def __init__(self, base_url=None, model=None, api_key=None):
        # 定位(grounding)用 ui-tars:7b（7B GUI 专精，坐标直出，实测像素级精度）；
        # 字段抽取(VQA)用 qwen2.5vl:3b（读中文强）。两者都走本地 Ollama，免装 PaddleOCR。
        self.base_url = (base_url or os.environ.get("OCR_LM_BASE_URL", "http://127.0.0.1:11434/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("OCR_LM_API_KEY", "ollama")
        self.model = model or os.environ.get("OCR_GROUND_MODEL", "ui-tars:7b")
        self.vqa_model = os.environ.get("OCR_VQA_MODEL", "qwen2.5vl:3b")
        self._last_point = None   # 调试：归一化命中点
        self._last_norm = None

    # ---- 工具 ----
    @staticmethod
    def _b64(obj):
        import base64 as _b
        from PIL import Image as _I
        import io as _io
        if isinstance(obj, str):
            with open(obj, "rb") as f:
                return _b.b64encode(f.read()).decode("ascii")
        if isinstance(obj, _I.Image):
            buf = _io.BytesIO(); obj.save(buf, "PNG")
            return _b.b64encode(buf.getvalue()).decode("ascii")
        buf = _io.BytesIO(); _I.fromarray(obj).save(buf, "PNG")
        return _b.b64encode(buf.getvalue()).decode("ascii")

    @staticmethod
    def _open(obj):
        from PIL import Image as _I
        if isinstance(obj, str):
            return _I.open(obj).convert("RGB")
        if isinstance(obj, _I.Image):
            return obj.convert("RGB")
        return _I.fromarray(obj).convert("RGB")

    def _vqa(self, question, b64, model=None):
        import json as _j, urllib.request as _u
        payload = {
            "model": model or self.vqa_model,
            "messages": [
                {"role": "system", "content": "You are a GUI text-localization assistant. Follow the user instruction exactly and output only what is asked."},
                {"role": "user", "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]},
            ],
            "temperature": 0.0,
            "max_tokens": 600,
        }
        req = _u.Request(
            self.base_url + "/chat/completions",
            data=_j.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with _u.urlopen(req, timeout=120) as resp:
            return _j.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_cells(resp, cols, rows):
        import re as _re, json as _j
        arr = None
        try:
            arr = _j.loads(resp)
        except Exception:
            pass
        cells = []
        if isinstance(arr, list):
            for item in arr:
                m = _re.search(r'[Rr](\d+)[Cc](\d+)', str(item))
                if m:
                    r, c = int(m.group(1)), int(m.group(2))
                    if 1 <= r <= rows and 1 <= c <= cols:
                        cells.append((c - 1, r - 1))
        if not cells:
            for r, c in _re.findall(r'[Rr](\d+)[Cc](\d+)', resp):
                if 1 <= int(r) <= rows and 1 <= int(c) <= cols:
                    cells.append((int(c) - 1, int(r) - 1))
        seen, uniq = set(), []
        for cell in cells:
            if cell not in seen:
                seen.add(cell); uniq.append(cell)
        return uniq

    def _ask_cells(self, b64, text, cols, rows, fuzzy):
        fuzzy_note = "（只要格子里出现了该文字即可，不必整格只有它）" if fuzzy else "（需整格文字恰好等于该文字）"
        q = (
            f'这张图被划分成 {rows} 行 × {cols} 列的网格：行从上到下编号 1..{rows}，'
            f'列从左到右编号 1..{cols}。请找出**包含**文字「{text}」的格子{fuzzy_note}。\n'
            f'只输出一个 JSON 数组，元素为 "R{{行}}C{{列}}" 形式的字符串，'
            f'例如 ["R2C3","R2C4"]；若没有任何格子包含该文字，输出 []。不要输出其他任何内容。'
        )
        try:
            resp = self._vqa(q, b64)
        except Exception as e:
            print(f"[LMVision] VQA 失败: {e}")
            return []
        return self._parse_cells(resp, cols, rows)

    # ---- 文字定位（核心）：ui-tars 原生 grounding ----
    # qwen2.5vl:3b 经实测「读中文」可靠，但空间定位（像素/网格/象限）系统性失准，
    # 无法用于点击。ui-tars:7b 是 7B GUI 专精模型，原生输出点击坐标(start_box，
    # [0,1000] 归一化)，实测对小程序按钮文字定位误差 <0.02，完美契合本场景。
    _GROUND_SYS = (
        "You are a GUI automation agent. Given a screenshot, decide the next single action "
        "to progress toward the goal. Output exactly one action using the format:\n"
        "Thought: <brief reasoning>\n"
        "Action: <action>\n\n"
        "Action space (coordinates are in the range [0,1000] as '(x,y)'):\n"
        "- click(start_box='(x,y)'): click/tap at the coordinate\n"
        "- finished(): the goal is fully accomplished; stop\n\n"
        "IMPORTANT: the Action line must be EXACTLY one of the two forms above, with real "
        "integer coordinates, e.g. Action: click(start_box='(512,300)'). Never output '=' or "
        "empty coordinates. If the target element does not exist, output Action: finished()."
    )

    def _ground(self, b64, text, fuzzy):
        contain = "contains" if fuzzy else "is exactly"
        q = (
            f'In this screenshot, find the UI element whose visible text {contain} "{text}". '
            f'Click its center. If no element with that text exists anywhere in the image, '
            f'output Action: finished() and write NOT_FOUND in Thought.'
        )
        correct = (
            "Your previous Action was malformed (missing or non-numeric coordinates). "
            "Re-output ONLY a valid line: Action: click(start_box='(x,y)') with the integer "
            "pixel coordinates (0-1000) of the element's center. Do not output '=' or blanks."
        )
        messages = [
            {"role": "system", "content": self._GROUND_SYS},
            {"role": "user", "content": [
                {"type": "text", "text": q},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]},
        ]
        import json as _j, urllib.request as _u, re as _re
        # 最多 3 次：第一次正常；若模型吐出 start_box='=' 之类的畸形坐标（ui-tars 偶发），
        # 用纠正消息多轮追问，通常第二次即给出合法坐标。
        for attempt in range(3):
            if attempt > 0:
                messages.append({"role": "user", "content": correct})
            payload = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": 300,
            }
            req = _u.Request(
                self.base_url + "/chat/completions",
                data=_j.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
                method="POST",
            )
            try:
                with _u.urlopen(req, timeout=180) as resp:
                    content = _j.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]
            except Exception as e:
                print(f"[LMVision] grounding 调用失败: {e}")
                return None
            m = _re.search(r"start_box\s*=\s*'\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*'", content)
            if not m:
                m = _re.search(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)", content)
            if m:
                x, y = int(m.group(1)), int(m.group(2))
                x = max(0, min(1000, x)); y = max(0, min(1000, y))
                return (x / 1000.0, y / 1000.0)  # 归一化命中点（相对输入图）
        return None

    def _verify(self, im0, nx, ny, text, fuzzy, pad=0.18):
        """用 qwen2.5vl（读中文可靠）确认命中点附近小图确实含目标文字。

        ui-tars 在目标文字缺失时仍可能点「最近的按钮状元素」，故加这一步让
        「文字不在屏上」能优雅失败，而不是误点。验证接口异常时保守放行（不阻断点击）。

        模糊(fuzzy)：命中点周边小图「包含」目标文字即可；
        精确(非 fuzzy)：命中点周边小图的文字须「恰好等于」目标文字（避免把「预约」误判为「立即预约」）。
        """
        iw, ih = im0.size
        pad_px = int(min(iw, ih) * pad)
        x = max(0, int(nx * iw - pad_px)); y = max(0, int(ny * ih - pad_px))
        w = min(iw - x, 2 * pad_px); h = min(ih - y, 2 * pad_px)
        if w < 4 or h < 4:
            return True
        patch = im0.crop((x, y, x + w, y + h))
        # 先让模型把小图里「实际读到的文字」转写出来，再据此判定，
        # 比单纯的 yes/no 更可靠，且能支撑精确匹配。
        q = 'Transcribe ALL visible text in this image. Output ONLY the text, with no quotes or extra words.'
        try:
            resp = self._vqa(q, self._b64(patch))
        except Exception:
            return True
        read = resp.strip().strip('"\u201c\u201d\u2018\u2019')
        if not read:
            return True  # 读不到文字时保守放行（仍按模型命中点点击）
        if fuzzy:
            return (text in read) or (read in text)
        # 精确匹配：小图文字须等于目标（允许同义/含空白差异）
        return read == text or read.replace(" ", "") == text.replace(" ", "")

    def locate_text(self, img, text, fuzzy=True, max_side=1280, verify=True):
        im0 = self._open(img)
        iw, ih = im0.size
        # 送「整图」(不裁剪)：实测 ui-tars 在整图上像素级精准，裁剪小图会偶发坐标畸形/漂移。
        # 缩小再送模型：ui-tars 对超大图(如 2× 截屏 2880×1800)既慢又易 OOM，
        # 且缩小不影响归一化坐标。grounding 返回 [0,1000] 归一化，按原图尺寸还原即可。
        im = im0
        if max(iw, ih) > max_side:
            scale = max_side / float(max(iw, ih))
            im = im0.resize((int(iw * scale), int(ih * scale)))
        pt = self._ground(self._b64(im), text, fuzzy)
        if not pt:
            self._last_point = None
            return None
        nx, ny = pt
        if verify and not self._verify(im0, nx, ny, text, fuzzy):
            print(f"[LMVision] 验证失败：命中点附近不含文字「{text}」，视为未找到")
            self._last_point = None
            return None
        self._last_point = [nx, ny]
        self._last_norm = [nx - 0.02, ny - 0.02, 0.04, 0.04]
        px, py = nx * iw, ny * ih  # 还原到「传入图」原始像素
        m = max(12, int(min(iw, ih) * 0.02))  # 调试可视框半宽
        return [int(round(px - m)), int(round(py - m)), 2 * m, 2 * m]

    # ---- 模板匹配：LM 后端不支持（需 cv2），交由 PaddleOCR 后端 ----
    def match_template(self, img, template, threshold=0.8):
        raise NotImplementedError("LMVision 不支持模板匹配；请用 OCR=real（PaddleOCR）或改用 text 模式")

    # ---- 字段抽取（两阶段：转录 + 确定性解析）----
    def _transcribe_lines(self, im):
        """Phase 1: 用 qwen2.5vl 把图中所有可见文字按行转写出来。"""
        import re as _re
        q_transcribe = (
            '请仔细读取这张手机App截图中的所有可见中文文字。'
            '按从上到下、从左到右的顺序逐行输出每一段文字。'
            '要求：\n'
            '- 包括标题、副标题、按钮文字、说明文字等所有可见文本\n'
            '- 英文/数字/符号也保留\n'
            '- 不要添加任何解释、标注或格式\n'
            '- 如果某行看不清就跳过\n'
            '- 只输出转写的文字内容，不要有其他内容'
        )
        raw = self._vqa(q_transcribe, self._b64(im))
        transcription = raw.strip()
        if transcription.startswith('```'):
            transcription = _re.sub(r'^```(?:text|plain)?\s*', '', transcription)
            transcription = _re.sub(r'\s*```\s*$', '', transcription)
        # 去掉可能的序号前缀
        transcription = _re.sub(r'^\d+[\.\、\)]\s*', '', transcription, flags=_re.MULTILINE)
        lines = [l.strip() for l in transcription.split('\n') if l.strip()]
        return lines

    @staticmethod
    def _looks_like_failed_transcription(lines):
        """判断模型是否给出无效转写（如 '无' / '没有文字' 等）。"""
        if not lines:
            return True
        if len(lines) == 1 and lines[0] in ('无', '没有', '没有文字', '无文字', '无可见文字', 'None', 'none'):
            return True
        return False

    def _extract_all_fields(self, lines, fields):
        """Phase 2: 从行列表中用启发式规则提取所有字段。"""
        result = {name: self._extract_field(name, lines, fields.get(name, "")) for name in fields}

        # venue/address 去重：如果两者相同，address 重新找下一行
        if result.get("venue") and result.get("address") == result.get("venue"):
            import re as _re
            venue = result["venue"]
            for line in lines:
                if line != venue and _re.search(r'[\u4e00-\u9fff]', line):
                    if _re.search(r'\d+号|路|区|街|座|层|栋|巷', line):
                        result["address"] = line.strip()
                        break
            if result.get("address") == venue:
                for line in lines:
                    if line != venue and len(line) >= 3 and _re.search(r'[\u4e00-\u9fff]', line):
                        result["address"] = line.strip()
                        break
        return result

    def _score_result(self, result):
        """给一次抽取结果打分：非空且不是纯占位符的字段越多分越高。"""
        score = 0
        for v in result.values():
            if v and v not in ('无', '没有', '没有文字', '无文字'):
                score += 1
        return score

    def ocr_extract(self, region, fields, round=0, img=None):
        """用「模型纯转写 + 确定性规则解析」从截图中抽取字段。

        旧方案（让 3B 模型同时读文字+输出 JSON）不稳定：
        - 模型经常返回错误格式（markdown 围栏、列表、空对象）
        - 小字（地址）容易被忽略
        - 弹窗遮挡时全空

        新方案分两阶段：
        Phase 1: 让 qwen2.5vl 做它擅长的——纯转写图中所有可见文字（按行输出）
        Phase 2: 用确定性启发式规则从纯文本中提取各字段（不依赖模型的 JSON 能力）

        重试策略：本方法本身只做单次识别；调用方（interpreter._act_ocr_extract）
        在结果不佳时会重新截图并再次调用，从而同时覆盖「模型抖动」和「页面未加载完」两种情况。
        """
        if img is None:
            return {k: "" for k in (fields or {})}
        im = self._open(img)
        if region:
            w, h = im.size
            if all(0 <= v <= 1 for v in region):
                x, y, rw, rh = [int(v * (w if i % 2 == 0 else h)) for i, v in enumerate(region)]
            else:
                x, y, rw, rh = [int(v) for v in region]
            im = im.crop((x, y, x + rw, y + rh))
        names = list((fields or {}).keys())
        if not names:
            return {}

        try:
            lines = self._transcribe_lines(im)
            print(f"[LMVision] ocr_extract 转写: {lines[:8]}")
        except Exception as e:
            print(f"[LMVision] ocr_extract 转写失败: {e}")
            return {k: "" for k in names}

        if self._looks_like_failed_transcription(lines):
            print(f"[LMVision] 转写无效（可能页面未加载/被遮挡）")
            return {k: "" for k in names}

        result = self._extract_all_fields(lines, fields)
        score = self._score_result(result)
        print(f"[LMVision] 抽取结果: {result} (score={score})")

        # 如果关键字段全空 → 用旧方案（直接 JSON 提取）再试一次
        if score == 0:
            print(f"[LMVision] 两阶段解析全空，尝试 JSON 兜底…")
            result_fallback = self._extract_json_fallback(names, im)
            for k in names:
                if result_fallback.get(k):
                    result[k] = result_fallback[k]

        # 清理常见前缀（如 "地址："）
        for k in result:
            import re as _re
            v = result[k]
            if isinstance(v, str):
                v = _re.sub(r'^\s*(?:地址|场地|venue|address|location)[:：\s]*', '', v).strip()
                result[k] = v

        return result

    def _extract_field(self, field_name, lines, pattern_hint):
        """从转写文本行中用启发式规则提取单个字段的值。

        规则优先级（针对小程序详情页的典型布局）：
        - venue: 第一行有意义的中文文本（跳过按钮/UI元素）
        - address: 包含地址特征词（号/路/区/座/层/街/巷/栋）的行，且不与 venue 重复
        - 其它: 按 pattern_hint 正则匹配
        """
        import re as _re
        name_lower = field_name.lower()

        # 跳过的 UI 元素关键词（按钮文字、提示条等）
        UI_SKIP_PREFIXES = ('点击', '请', '为您', '允许', '同意', '拒绝', '确定', '取消',
                            '返回', '关闭', '查看更多', '加载中')
        UI_SKIP_SHORT = ('无预定', '电话预定', '小程序预订', '立即订场', '场地预定',
                         '自助约课', '充值会员', '购买课程', '公司介绍', '预约',
                         'Reserve', '首页', '我的')
        # 由纯 UI 关键词组成的行（如 "无预定 电话预定 小程序预订"）也应跳过
        UI_KEYWORDS = set('无预定 电话预定 小程序预订 立即订场 场地预定 自助约课 '
                          '充值会员 购买课程 公司介绍 预约 Reserve 首页 我的 '
                          '点击 添加到 我的小程序 置顶 设置 反馈与投诉 重新进入 '
                          '复制链接 静音 转发给朋友 发送到朋友圈'.split())

        # venue / 场地名称 → 取第一行有意义的中文长文本（跳过按钮/UI元素）
        if name_lower in ("venue", "场地", "场馆", "name", "title"):
            for line in lines:
                # 跳过太短的行
                if len(line) < 3:
                    continue
                # 跳过纯英文/纯数字/特殊字符
                if _re.match(r'^[\d\s\-\(\)\[\]\{\}·\+]+$', line):
                    continue
                # 跳过 UI 元素
                if any(line.startswith(p) for p in UI_SKIP_PREFIXES):
                    continue
                if any(line == s for s in UI_SKIP_SHORT):
                    continue
                # 跳过由纯 UI 关键词组成的行（如 "无预定 电话预定 小程序预订"）
                line_words = set(line.replace('·', ' ').split())
                if line_words and line_words.issubset(UI_KEYWORDS):
                    continue
                # 跳过纯英文行
                if _re.match(r'^[A-Za-z\s\.]+$', line) and not _re.search(r'[\u4e00-\u9fff]', line):
                    continue
                # 优先选含中文的长行（场馆名通常是中文且较长）
                if _re.search(r'[\u4e00-\u9fff]{2,}', line):
                    # 去掉常见的英文副标题后缀
                    cleaned = _re.split(r'\s+[A-Z][A-Z\s\.\-]+$', line)[0].strip()
                    # 去掉末尾的标点
                    cleaned = _re.sub(r'[~×\s]+$', '', cleaned).strip()
                    if len(cleaned) >= 2:
                        return cleaned
            # 兜底：返回第一个非 UI 行
            for line in lines:
                if len(line) >= 2 and not any(line.startswith(p) for p in UI_SKIP_PREFIXES):
                    return line
            return lines[0] if lines else ""

        # address / 地址 → 找包含地址特征词的行（且不与 venue 重复）
        if name_lower in ("address", "地址", "位置", "location", "addr"):
            # 先获取 venue 值用于去重
            venue_val = ""  # 由调用方在外部处理去重，这里只做基本提取
            addr_patterns = [
                r'^.*?[\u4e00-\u9fff].*?\d+号',           # "天坛东路13号"
                r'^.*?[\u4e00-\u9fff].*?(?:路|区|街|巷|栋|单元|层|座|弄|苑|里|园|广场|中心|大厦|公寓|新村|花园|小区|道|胡同)',
                r'.*\(\d+\)\d+号',                        # "(2028)1190号"
                r'.*\d+\s*(?:号|楼|层)',                   # 数字+号/楼/层
            ]
            for pat in addr_patterns:
                for line in lines:
                    m = _re.match(pat, line)
                    if m:
                        return line.strip()
            # 兜底：找包含数字或特征词的非首行
            for i, line in enumerate(lines[1:], 1):
                if _re.search(r'[\u4e00-\u9fff]', line):
                    if _re.search(r'\d+|号|路|区|座|层', line):
                        return line.strip()
            # 最后兜底：第二行（如果存在且不像 UI 元素）
            if len(lines) >= 2:
                second = lines[1]
                if (_re.search(r'[\u4e00-\u9fff]', second)
                        and not _re.match(r'^[A-Z]', second)
                        and not any(second.startswith(p) for p in UI_SKIP_PREFIXES)):
                    return second
            return ""

        # 通用正则匹配（其它字段）
        if pattern_hint:
            try:
                compiled = _re.compile(pattern_hint)
                for line in lines:
                    m = compiled.search(line)
                    if m and m.groups():
                        return m.group(1).strip() if m.lastindex else line.strip()
            except Exception:
                pass

        return ""

    def _extract_json_fallback(self, names, im):
        """JSON 直接提取兜底：当两阶段全空时，让模型直接输出 JSON 再试一次。
        
        这是最后的手段；正常情况下两阶段应该已经能拿到结果。
        """
        import json as _j, re as _re
        try:
            q = (
                f'从这张图提取以下字段为 JSON 对象（只输出 JSON，不要其他内容）：'
                f'{_j.dumps(names, ensure_ascii=False)}'
            )
            resp = self._vqa(q, self._b64(im))
            c = resp.strip()
            if c.startswith('```'):
                c = _re.sub(r'^```(?:json)?\s*', '', c)
                c = _re.sub(r'\s*```\s*$', '', c)
            try:
                data = _j.loads(c)
            except Exception:
                m = _re.search(r'\{.*\}', c, _re.DOTALL)
                data = _j.loads(m.group(0)) if m else {}
            if isinstance(data, dict):
                return {k: str(data.get(k, "")).strip() for k in names}
        except Exception as e:
            print(f"[LMVision] JSON 兜底也失败: {e}")
        return {}
