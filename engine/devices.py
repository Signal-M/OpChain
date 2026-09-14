"""设备控制适配层（可插拔）。

- MockDevice：模拟小程序页面流转，零依赖，用于演示/调试。
- ADBDevice：通过 adb 控制安卓模拟器/真机（Windows/Mac 均可跑，需安装 platform-tools）。
- WindowsUIDevice：通过 pywinauto 控制 Windows 上的 PC 微信客户端（仅 Windows 可用）。

所有真实设备都把"文字/图标定位"交给注入的 vision 实例，自身只负责执行点击/滑动/返回。
"""
import os
import re
import time
import base64
import random
import tempfile
import subprocess
import hashlib
import ctypes

# 模型/前端投屏截屏降采样上限（最长边像素）。实屏 Retina 可达 5000×3000+，
# 降采样后：① 大幅缩减发给 Ollama 的 base64 体积（更低延迟 / 更少 token）；
# ② 与坐标无关——UI-TARS 输出 [0,1000] 归一化坐标，按全屏尺寸映射，降采样不改落点。
# 可通过环境变量 MAC_MODEL_MAX_SIDE 调高（如 1920/2560）以提升小目标点击精度，代价是更多 token。
MAC_MODEL_MAX_SIDE = int(os.environ.get("MAC_MODEL_MAX_SIDE", "1280"))


def _is_png(path):
    """截屏是否拿到合法 PNG（screencapture 无「屏幕录制」权限时会写入失败/空文件）。"""
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except Exception:
        return False


class BaseDevice:
    page = "unknown"
    highlight = None
    round = 0
    card = 0

    def __init__(self, vision=None):
        self.vision = vision
        self._last_click = None     # 最近一次真实点击的逻辑点数 (x, y)
        self._last_region = None    # 最近一次点击的目标区域（归一化 [x,y,w,h]，相对全屏）

    def screenshot(self):
        raise NotImplementedError

    def tap_text(self, text, **kw):
        raise NotImplementedError

    def tap_image(self, template, **kw):
        raise NotImplementedError

    def tap_coord(self, x, y, **kw):
        raise NotImplementedError

    def tap_region(self, target):
        """框定区域点击（RPA 式确定性点击）：target.region=[x,y,w,h] 归一化(0..1,相对全屏)，
        mode=center(点区域中心)|template(区域内模板匹配)|text(区域内OCR文字)。
        基类无操作实现由子类完成。"""
        raise NotImplementedError

    def swipe(self, direction="up", **kw):
        raise NotImplementedError

    def back(self):
        raise NotImplementedError

    def copy_link(self):
        raise NotImplementedError

    def type_text(self, text, x=None, y=None, **kw):
        """在 (x,y) 聚焦后输入文本（真实键盘注入）。Mock/无输入场景抛 NotImplemented。"""
        raise NotImplementedError

    def can_scroll(self):
        return True

    def perceive(self):
        """感知当前屏幕，返回结构化「屏幕树」（优先）或截图兜底。
        Mock/真实设备均可重写。Agent 的 Plan 阶段消费此输出。"""
        return {"modality": "screenshot", "page": self.page,
                "highlight": self.highlight, "elements": [], "scrollable": True}

    def current_venue(self):
        """当前卡片对应的场地名（供探索期落库/调试）。"""
        return f"场地{self.card}"

    def snapshot_b64(self):
        """返回当前屏幕的 base64 PNG（供前端实时投屏）。无截图能力的设备返回空串。"""
        return ""

    def screen_fp(self):
        """当前屏幕的指纹（用于判断"页面是否变化"，如弹窗是否出现）。

        基类默认返回空串（视为"无法判断"），真实设备可重写。引擎的 wait(until_change)
        动作依赖它来"等到弹窗/页面跳转出现再继续"，从而消除步骤间的时序竞争。"""
        return ""

    # ---- 安全截图区域（框选范围）----
    def set_region(self, x, y, w, h):
        """设置截屏/操作安全区域（逻辑点数，左上原点）。基类无操作。"""
        return None

    def clear_region(self):
        """清除安全区域。基类无操作。"""
        return None

    def full_snapshot_b64(self):
        """全屏截屏（忽略区域）base64，供前端框选预览。基类返回空串。"""
        return ""

    def region(self):
        """当前安全区域 (x,y,w,h) 或 None。"""
        return None

    def screen_size(self):
        """当前设备逻辑点数屏幕尺寸 (w,h)。"""
        return (0, 0)

    # ---- 点击精度打磨 ----
    def set_click_offset(self, dx, dy):
        """设置点击全局偏移（逻辑点数），补偿模型点击系统性偏差。基类无操作。"""
        return (0.0, 0.0)

    def click_offset(self):
        """当前点击偏移 (dx, dy)。"""
        return (0.0, 0.0)

    def set_tap_retry(self, max_retry, jitter):
        """设置自愈重试参数 (max_retry 次数, jitter 抖动像素)。基类无操作。"""
        return (0, 0)

    def tap_retry(self):
        """当前自愈重试参数 (max_retry, jitter)。"""
        return (0, 0)

    def norm_click(self):
        """最近一次真实点击的归一化坐标 [x,y]（0..1，相对全屏逻辑点数）；无则 None。

        调试可视化用：前端把该点画在全屏预览图上，直观看到"到底点到了哪里"。"""
        return None


# --------------------------------------------------------------------------
# Mock：无需任何硬件即可跑通完整循环
# --------------------------------------------------------------------------
class MockDevice(BaseDevice):
    def __init__(self, total=6, vision=None):
        super().__init__(vision)
        self.total = total
        self.round = 0
        self.card = 0
        self.page = "list"
        self.highlight = None
        self.can_scroll_flag = True

    def screenshot(self):
        return None

    def can_scroll(self):
        return self.can_scroll_flag

    def tap_text(self, text, **kw):
        self._go(text)
        return True

    def tap_image(self, template, **kw):
        self._go("more_dots")
        return True

    def tap_coord(self, x, y, **kw):
        self._go("coord")
        return True

    def tap_region(self, target):
        # Mock 不真正定位；仅标记动作并推进状态机
        self._go("region")
        return True

    def swipe(self, direction="up", **kw):
        self.round += 1
        if self.round >= self.total:
            self.can_scroll_flag = False
        self.page = "list"
        self.highlight = None
        return True

    def back(self):
        back_map = {
            "detail": "list",
            "confirm": "list",
            "new_miniapp": "list",
            "menu": "new_miniapp",
        }
        self.page = back_map.get(self.page, "list")
        self.highlight = None
        return True

    def copy_link(self):
        # 短链形式：https://wxa.example.com/s/<8位短码>
        # 真实设备(ADB/Windows)从剪贴板读实际复制内容，此处 Mock 模拟短链。
        raw = f"venue={self.card}&ts={int(time.time())}"
        code = hashlib.md5(raw.encode()).hexdigest()[:8]
        return f"https://wxa.example.com/s/{code}"

    # ---- 结构树模态感知（GUI Agent 优先消费，免截图免 token）----
    def perceive(self):
        els = []
        if self.page == "list":
            els = [{"type": "text", "label": "预订", "id": "book"},
                   {"type": "text", "label": "筛选", "id": "filter"}]
        elif self.page == "detail":
            els = [{"type": "text", "label": "立即订场", "id": "book_now"},
                   {"type": "text", "label": "返回", "id": "back"}]
        elif self.page == "confirm":
            els = [{"type": "text", "label": "确定", "id": "confirm"}]
        elif self.page == "new_miniapp":
            els = [{"type": "image", "label": "more_dots", "id": "more"}]
        elif self.page == "menu":
            els = [{"type": "text", "label": "复制链接", "id": "copy_link"}]
        return {
            "modality": "tree",
            "page": self.page,
            "highlight": self.highlight,
            "round": self.round,
            "card": self.card,
            "elements": els,
            "scrollable": self.can_scroll(),
        }

    def current_venue(self):
        if not self.vision:
            return f"场地{self.card}"
        i = max(self.card, 1) - 1
        return self.vision.VENUES[i % len(self.vision.VENUES)]

    def _go(self, action):
        if action in ("预订", "card"):
            self.card += 1
        trans = {
            ("list", "预订"): "detail",
            ("list", "card"): "detail",
            ("detail", "立即订场"): "confirm",
            ("confirm", "确定"): "new_miniapp",
            ("new_miniapp", "more_dots"): "menu",
            ("menu", "复制链接"): "new_miniapp",
        }
        nxt = trans.get((self.page, action))
        if nxt:
            self.page = nxt
        self.highlight = action


# --------------------------------------------------------------------------
# ADB：安卓模拟器 / 真机
# --------------------------------------------------------------------------
class ADBDevice(BaseDevice):
    def __init__(self, vision=None, serial=None, total=None):
        super().__init__(vision)
        self.serial = serial
        self.total = total
        self.round = 0
        self.card = 0
        self.page = "unknown"
        self.highlight = None
        self._last_hash = None
        self._tmpdir = tempfile.mkdtemp(prefix="opchain_")

    # ---- adb 封装 ----
    def _adb(self, *args, capture=True):
        cmd = ["adb"]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += [str(a) for a in args]
        if capture:
            return subprocess.run(cmd, capture_output=True).stdout
        subprocess.run(cmd, check=False)

    def _screen_size(self):
        out = self._adb("shell", "wm", "size").decode("utf-8", "ignore")
        m = re.search(r"(\d+)x(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)

    # ---- 截屏 ----
    def screenshot(self):
        path = os.path.join(self._tmpdir, f"screen_{int(time.time() * 1000)}.png")
        proc = subprocess.run(
            ["adb"] + (["-s", self.serial] if self.serial else [])
            + ["exec-out", "screencap", "-p"],
            capture_output=True,
        )
        with open(path, "wb") as f:
            f.write(proc.stdout)
        return path

    # ---- 点击 ----
    def tap_coord(self, x, y, **kw):
        self._adb("shell", "input", "tap", str(int(x)), str(int(y)), capture=False)
        self.highlight = None

    def tap_text(self, text, **kw):
        img = self.screenshot()
        bbox = self.vision.locate_text(img, text, fuzzy=kw.get("fuzzy", True)) if self.vision else None
        if not bbox:
            raise RuntimeError(f"未找到文字: {text}")
        x = bbox[0] + bbox[2] // 2
        y = bbox[1] + bbox[3] // 2
        self.tap_coord(x, y)
        self.highlight = text

    def tap_image(self, template, **kw):
        img = self.screenshot()
        bbox = self.vision.match_template(img, template, kw.get("threshold", 0.8)) if self.vision else None
        if not bbox:
            raise RuntimeError(f"未匹配图标: {template}")
        x = bbox[0] + bbox[2] // 2
        y = bbox[1] + bbox[3] // 2
        self.tap_coord(x, y)
        self.highlight = template

    # ---- 滑动 ----
    def swipe(self, direction="up", **kw):
        W, H = self._screen_size()
        d = kw.get("distance_ratio", 0.6)
        if direction == "up":
            x1 = x2 = W // 2
            y1, y2 = int(H * (0.5 + d / 2)), int(H * (0.5 - d / 2))
        elif direction == "down":
            x1 = x2 = W // 2
            y1, y2 = int(H * (0.5 - d / 2)), int(H * (0.5 + d / 2))
        elif direction == "left":
            y1 = y2 = H // 2
            x1, x2 = int(W * (0.5 + d / 2)), int(W * (0.5 - d / 2))
        elif direction == "right":
            y1 = y2 = H // 2
            x1, x2 = int(W * (0.5 - d / 2)), int(W * (0.5 + d / 2))
        else:
            x1 = x2 = W // 2
            y1, y2 = int(H * 0.8), int(H * 0.2)
        self._adb("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), "300", capture=False)
        self.round += 1

    def back(self):
        self._adb("shell", "input", "keyevent", "KEYCODE_BACK", capture=False)

    # ---- 复制链接（尽力而为，受 Android 剪贴板权限限制）----
    def copy_link(self):
        # 前提：链路已点击"复制链接"。Android 高版本读取剪贴板受限，
        # 这里尝试两种老方法，失败返回空串并交由日志提示。
        try:
            out = self._adb("shell", "service", "call", "clipboard", "2").decode("utf-8", "ignore")
            m = re.search(r"'((?:\\.|[^'])*)'", out)
            if m:
                return m.group(1).encode().decode("unicode_escape")
        except Exception:
            pass
        return ""

    # ---- 滑不动判定：连续两帧像素一致即认为到底（引擎 max_rounds 兜底）----
    def can_scroll(self):
        img = self.screenshot()
        h = self._hash(img)
        if self._last_hash is None:
            self._last_hash = h
            return True
        changed = h != self._last_hash
        self._last_hash = h
        return changed

    @staticmethod
    def _hash(path):
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()


# --------------------------------------------------------------------------
# Windows UI Automation：PC 微信客户端（仅 Windows）
# --------------------------------------------------------------------------
class WindowsUIDevice(BaseDevice):
    def __init__(self, vision=None, window_title="微信", total=None):
        super().__init__(vision)
        self.window_title = window_title
        self.total = total
        self.round = 0
        self.card = 0
        self.page = "unknown"
        self.highlight = None
        self._last_hash = None
        self._tmpdir = tempfile.mkdtemp(prefix="opchain_")
        self._win = None

    def _ensure_win(self):
        if self._win is not None:
            return self._win
        import pywinauto  # 懒加载，仅 Windows 需安装
        app = pywinauto.Application().connect(title_re=self.window_title)
        self._win = app.window(title_re=self.window_title)
        return self._win

    def screenshot(self):
        # 截取微信窗口区域为图片
        from PIL import ImageGrab

        win = self._ensure_win()
        box = (win.rectangle().left, win.rectangle().top, win.rectangle().right, win.rectangle().bottom)
        path = os.path.join(self._tmpdir, f"screen_{int(time.time() * 1000)}.png")
        ImageGrab.grab(box).save(path)
        return path

    def _click_screen(self, x, y):
        import pywinauto

        win = self._ensure_win()
        rect = win.rectangle()
        pywinauto.mouse.click(coords=(rect.left + int(x), rect.top + int(y)))
        self.highlight = None

    def tap_coord(self, x, y, **kw):
        self._click_screen(x, y)

    def tap_text(self, text, **kw):
        img = self.screenshot()
        bbox = self.vision.locate_text(img, text, fuzzy=kw.get("fuzzy", True)) if self.vision else None
        if not bbox:
            raise RuntimeError(f"未找到文字: {text}")
        self._click_screen(bbox[0] + bbox[2] // 2, bbox[1] + bbox[3] // 2)
        self.highlight = text

    def tap_image(self, template, **kw):
        img = self.screenshot()
        bbox = self.vision.match_template(img, template, kw.get("threshold", 0.8)) if self.vision else None
        if not bbox:
            raise RuntimeError(f"未匹配图标: {template}")
        self._click_screen(bbox[0] + bbox[2] // 2, bbox[1] + bbox[3] // 2)
        self.highlight = template

    def swipe(self, direction="up", **kw):
        from PIL import Image

        img = Image.open(self.screenshot())
        W, H = img.size
        d = kw.get("distance_ratio", 0.6)
        pts = {
            "up": ((W // 2, int(H * (0.5 + d / 2))), (W // 2, int(H * (0.5 - d / 2)))),
            "down": ((W // 2, int(H * (0.5 - d / 2))), (W // 2, int(H * (0.5 + d / 2)))),
            "left": ((int(W * (0.5 + d / 2)), H // 2), (int(W * (0.5 - d / 2)), H // 2)),
            "right": ((int(W * (0.5 - d / 2)), H // 2), (int(W * (0.5 + d / 2)), H // 2)),
        }.get(direction, ((W // 2, int(H * 0.8)), (W // 2, int(H * 0.2))))
        import pywinauto

        win = self._ensure_win()
        rect = win.rectangle()
        pywinauto.mouse.move(coords=(rect.left + pts[0][0], rect.top + pts[0][1]))
        pywinauto.mouse.press()
        pywinauto.mouse.move(coords=(rect.left + pts[1][0], rect.top + pts[1][1]))
        pywinauto.mouse.release()
        self.round += 1

    def back(self):
        import pywinauto

        pywinauto.keyboard.send_keys("{ESC}")

    def copy_link(self):
        # PC 微信复制链接后，剪贴板可由 pyperclip 读取
        try:
            import pyperclip

            return pyperclip.paste()
        except Exception:
            return ""

    def can_scroll(self):
        img = self.screenshot()
        h = self._hash(img)
        if self._last_hash is None:
            self._last_hash = h
            return True
        changed = h != self._last_hash
        self._last_hash = h
        return changed

    @staticmethod
    def _hash(path):
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()


# --------------------------------------------------------------------------
# macOS 本机真实设备：screencapture 截屏 + CoreGraphics 注入点击/键盘/拖拽
# 零第三方依赖（仅调用系统 screencapture 与 CoreGraphics.framework via ctypes）。
# 前置：系统设置 → 隐私与安全性 → 辅助功能 中授权运行本进程（Terminal / Python）。
# --------------------------------------------------------------------------
class MacDevice(BaseDevice):
    def __init__(self, vision=None, total=None):
        super().__init__(vision)
        self.total = total
        self.round = 0
        self.card = 0
        self.page = "screen"
        self.highlight = None
        self._last_hash = None
        self._tmpdir = tempfile.mkdtemp(prefix="opchain_mac_")
        self._region = None          # 截屏/操作区域（逻辑点数 x,y,w,h）；None=全屏
        self._scale = 1.0            # 设备像素/逻辑点数（Retina 通常 2）
        self._screen_px = (1440, 900)
        self._screen = self._screen_size()
        self._click_offset = (0.0, 0.0)   # 点击全局偏移（逻辑点数），补偿模型系统性偏差
        self._tap_retry = (0, 0)          # 自愈重试 (max_retry, jitter)，均为 0=关闭
        self._hidden_name = None          # hide_browser 记录的被隐藏窗口进程名
        self._click_mode = "to_pid"       # 默认：CGEventPostToPid 直投微信进程（已验证对微信小程序生效）；其它模式见 set_click_mode
        self._click_mode_log = ""         # 最近一次实际使用的点击方式（证据记录用）

    # ---- 屏幕信息 ----
    @staticmethod
    def _cg():
        for path in (
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
        ):
            try:
                return ctypes.CDLL(path)
            except OSError:
                continue
        raise OSError("找不到 CoreGraphics 框架（仅 macOS 可用）")

    @classmethod
    def cg_available(cls):
        try:
            cls._cg()
            return True
        except OSError:
            return False

    def _screen_size(self):
        try:
            cg = self._cg()
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
            cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
            did = cg.CGMainDisplayID()
            pw = int(cg.CGDisplayPixelsWide(did))
            ph = int(cg.CGDisplayPixelsHigh(did))
            # 物理像素（裁图/视觉定位换算用）与逻辑点数（CGEvent / -R 坐标系）
            try:
                cg.CGDisplayCopyDisplayMode.restype = ctypes.c_void_p
                cg.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
                mode = cg.CGDisplayCopyDisplayMode(did)
                cg.CGDisplayModeGetPixelWidth.restype = ctypes.c_size_t
                cg.CGDisplayModeGetPixelWidth.argtypes = [ctypes.c_void_p]
                cg.CGDisplayModeGetPixelHeight.restype = ctypes.c_size_t
                cg.CGDisplayModeGetPixelHeight.argtypes = [ctypes.c_void_p]
                dev_w = int(cg.CGDisplayModeGetPixelWidth(mode))
                dev_h = int(cg.CGDisplayModeGetPixelHeight(mode))
                if dev_w and dev_h:
                    self._screen_px = (dev_w, dev_h)
                    self._scale = dev_w / pw if pw else 1.0
                cg.CGDisplayModeRelease.argtypes = [ctypes.c_void_p]
                cg.CGDisplayModeRelease(mode)
            except Exception:
                self._screen_px = (pw, ph)
                self._scale = 1.0
            return (pw, ph)   # 逻辑点数（与 CGEvent / screencapture -R 同一坐标系）
        except Exception:
            self._screen_px = (1440, 900)
            self._scale = 1.0
            return (1440, 900)

    # ---- 截屏（尊重安全区域）----
    def screenshot(self):
        path = os.path.join(self._tmpdir, f"screen_{int(time.time() * 1000)}.png")
        if self._region:
            # screencapture -R 接受「逻辑点数 x,y,w,h」，输出设备像素图（点数×scale）
            x, y, w, h = [int(round(v)) for v in self._region]
            subprocess.run(
                ["screencapture", "-x", "-R", f"{x},{y},{w},{h}", "-t", "png", path],
                check=False,
            )
        else:
            subprocess.run(["screencapture", "-x", "-t", "png", path], check=False)
        return path

    def _logical_center(self, bbox):
        """把 OCR/模板返回的「设备像素」bbox (x,y,w,h) 换算成「逻辑点数」中心点。

        screencapture 在 Retina 屏上输出 2× 设备像素图（如 2940×1912），而 CoreGraphics
        的 CGEvent 点击用的是逻辑点数（如 1470×956），二者差一个 self._scale。
        不做换算会让 OCR/模板点击系统性偏到 2× 远处——这正是「点不中按钮」的根因之一。
        """
        s = self._scale or 1.0
        cx = (bbox[0] + bbox[2] / 2.0) / s
        cy = (bbox[1] + bbox[3] / 2.0) / s
        return cx, cy

    def _norm_bbox(self, bbox):
        """把设备像素 bbox (x,y,w,h) 换算为归一化 [x,y,w,h]（0..1，相对全屏逻辑点数）。

        供调试可视化：前端把该框画在全屏预览图上，标出"本次点击瞄准的是哪一块"。"""
        s = self._scale or 1.0
        W, H = self._screen[0], self._screen[1]
        if not W or not H:
            return None
        x, y, w, h = bbox
        return [round((x / s) / W, 4), round((y / s) / H, 4),
                round((w / s) / W, 4), round((h / s) / H, 4)]

    def norm_click(self):
        """最近一次真实点击的归一化坐标 [x,y]（0..1，相对全屏逻辑点数）。"""
        if self._last_click is None:
            return None
        W, H = self._screen[0], self._screen[1]
        if not W or not H:
            return None
        return [round(self._last_click[0] / W, 4), round(self._last_click[1] / H, 4)]

    # ---- 点击（模型坐标在区域内 → 偏移到绝对点数 → 可选自愈重试）----
    def tap_coord(self, x, y, **kw):
        base_x, base_y = float(x), float(y)
        apply_region = kw.get("apply_region", True)  # tap_region 已自带绝对坐标，跳过区域偏移
        max_retry, jitter = self._tap_retry
        use_retry = max_retry > 0 and jitter > 0
        pre_fp = self._screen_fp() if use_retry else None
        for i in range(1 + max_retry):
            jx, jy = base_x, base_y
            if i > 0 and jitter > 0:
                # 自愈：第 i 次重点时施加 ±jitter 像素随机抖动，命中偏移目标
                jx += random.uniform(-jitter, jitter)
                jy += random.uniform(-jitter, jitter)
            tx, ty = jx, jy
            if apply_region and self._region:
                tx += self._region[0]
                ty += self._region[1]
            tx += self._click_offset[0]
            ty += self._click_offset[1]
            self._do_click(tx, ty)
            if not use_retry:
                break
            # 自愈：点击后页面无变化（疑似点偏未触发交互），轻微抖动重重点
            time.sleep(0.15)
            post_fp = self._screen_fp()
            if post_fp != pre_fp:
                break
            pre_fp = post_fp
            self._last_click = (tx, ty)   # 记录最终落点（调试可视化用）
            self.highlight = None

    def tap_norm(self, nx, ny):
        """统一坐标换算核心（P0 坐标管线）：归一化 nx,ny (0..1, 相对「当前截图画布」)
        → 全屏绝对逻辑点数。

        与 UI-TARS 的「0-1000 归一化 + 预处理镜像」同源：所有标注/模型坐标先归一到 0..1，
        再统一在此换算成绝对逻辑坐标，避免在各调用处散落换算导致漂移。
          - _region（安全区）已设置：截图画布即区域子图，nx,ny 相对子图，需叠加上左角偏移；
          - _region 未设置：截图画布即全屏，nx,ny 直接 × 全屏宽高。
        返回全屏绝对逻辑点数；调用方须以 apply_region=False 走 tap_coord，
        否则会被重复叠加 _region 偏移。
        """
        nx, ny = float(nx), float(ny)
        if self._region:
            rx0, ry0, rw0, rh0 = (float(v) for v in self._region)
            ax = rx0 + nx * rw0
            ay = ry0 + ny * rh0
        else:
            W, H = self._screen[0], self._screen[1]
            ax = nx * W
            ay = ny * H
        return ax, ay

    def region_to_screen_norm(self, reg):
        """画布归一化 region [x,y,w,h]（标注画布相对）→ 全屏归一化 region。

        _region（安全区）设置时画布即区域子图，需把子图归一化映射回全屏归一化，
        供 _crop_to_region_img / sanity 比较等以全屏为基准的逻辑复用。未设置 _region 时原样返回。
        """
        rx, ry, rw, rh = (float(v) for v in reg)
        if self._region:
            rx0, ry0, rw0, rh0 = (float(v) for v in self._region)
            W, H = self._screen[0], self._screen[1]
            fx = (rx0 + rx * rw0) / W
            fy = (ry0 + ry * rh0) / H
            fw = (rw * rw0) / W
            fh = (rh * rh0) / H
            return [fx, fy, fw, fh]
        return [rx, ry, rw, rh]

    # ---- 点击精度打磨接口 ----
    def set_click_offset(self, dx, dy):
        dx = float(dx) if dx is not None else 0.0
        dy = float(dy) if dy is not None else 0.0
        self._click_offset = (dx, dy)
        return dict(dx=dx, dy=dy)

    def click_offset(self):
        return dict(dx=self._click_offset[0], dy=self._click_offset[1])

    def set_tap_retry(self, max_retry, jitter):
        max_retry = int(max_retry) if max_retry is not None else 0
        jitter = float(jitter) if jitter is not None else 0.0
        if max_retry < 0:
            max_retry = 0
        if jitter < 0:
            jitter = 0.0
        self._tap_retry = (max_retry, jitter)
        return dict(max_retry=max_retry, jitter=jitter)

    def tap_retry(self):
        return dict(max_retry=self._tap_retry[0], jitter=self._tap_retry[1])

    def _screen_fp(self):
        """归一化屏幕指纹（64px JPEG 的 md5），用于判断点击后页面是否变化。

        直接用原生 PNG 字节比对并不可靠（screencapture 可能写入时间戳等元数据，
        导致视觉相同的两帧哈希不同）。归一化为小 JPEG 后重编码，可比性稳定。
        """
        try:
            path = self.screenshot()
            if not _is_png(path):
                return ""
            tmp = path + ".fp.jpg"
            r = subprocess.run(
                ["sips", "-Z", "64", path, "--setProperty", "format", "jpeg", "--out", tmp],
                capture_output=True, text=True,
            )
            if r.returncode != 0 or not os.path.exists(tmp):
                return self._hash(path)
            with open(tmp, "rb") as f:
                h = hashlib.md5(f.read()).hexdigest()
            try:
                os.remove(tmp)
            except Exception:
                pass
            return h
        except Exception:
            return ""

    def tap_text(self, text, **kw):
        img = self.screenshot()
        bbox = self.vision.locate_text(img, text, fuzzy=kw.get("fuzzy", True)) if self.vision else None
        if not bbox:
            raise RuntimeError(f"未找到文字: {text}")
        self._last_region = self._norm_bbox(bbox)  # 记录瞄准区域（调试用）
        cx, cy = self._logical_center(bbox)  # 设备像素 → 逻辑点数（Retina 2x 修正）
        self.tap_coord(cx, cy)  # 默认 apply_region=True：有安全区域时叠加偏移
        self.highlight = text

    def tap_image(self, template, **kw):
        img = self.screenshot()
        bbox = self.vision.match_template(img, template, kw.get("threshold", 0.8)) if self.vision else None
        if not bbox:
            raise RuntimeError(f"未匹配图标: {template}")
        self._last_region = self._norm_bbox(bbox)  # 记录瞄准区域（调试用）
        cx, cy = self._logical_center(bbox)  # 设备像素 → 逻辑点数（Retina 2x 修正）
        self.tap_coord(cx, cy)
        self.highlight = template

    # ---- 键盘输入 ----
    def type_text(self, text, x=None, y=None, **kw):
        if x is not None and y is not None:
            self.tap_coord(x, y)
        self._type(str(text))
        self.highlight = None

    # ---- 滑动（安全区域内滑动；无区域则用全屏）----
    def swipe(self, direction="up", distance_ratio=None, distance_px=None, **kw):
        if self._region:
            ox, oy, rw, rh = self._region
            W, H, bx, by = rw, rh, ox, oy
        else:
            W, H, bx, by = self._screen[0], self._screen[1], 0, 0
        midx, midy = bx + W / 2.0, by + H / 2.0
        # 距离（逻辑点 / px）：用户显式指定用 distance_px；否则按比例给合理默认。
        if distance_px:
            mag = float(distance_px)
        else:
            d = distance_ratio if distance_ratio is not None else 0.5
            mag = min(W, H) * d * 0.6
        # 关键：微信小程序 WebView 对 PID 直投滚轮(被过滤)与拖拽手势(不识别)均无反应，
        # 仅对「全局(HID层)滚轮事件」CGEventPost(0) 响应（实测像素比对证据）。
        # 故滑动统一走全局滚轮：竖向=上/下，横向=左/右。
        v_delta = {"up": mag, "down": -mag}.get(direction, 0)
        h_delta = {"left": mag, "right": -mag}.get(direction, 0)
        self._scroll_wheel(midx, midy, v_delta=v_delta, h_delta=h_delta)
        self.round += 1
        self.highlight = None

    def back(self):
        # 微信小程序 macOS：ESC(键码53) 不能翻页返回；改用 iOS 式"从左边缘向右滑"返回手势。
        # 起点贴左边缘(x≈5)，终点约向右 45% 宽度；沿活动区域（或全屏）中垂线滑动。
        if self._region:
            ox, oy, rw, rh = self._region
            bx, by, W, H = ox, oy, rw, rh
        else:
            W, H, bx, by = self._screen[0], self._screen[1], 0, 0
        y = by + H / 2.0
        x1 = bx + 5.0
        x2 = bx + W * 0.45
        # 全局投递：微信小程序 WebView 对全局事件才有响应（PID 直投被过滤）；
        # 实测拖拽手势滚动无效，但左边缘右滑返回手势仍可能靠全局拖拽触发。
        self._drag(x1, y, x2, y)
        time.sleep(0.3)
        self.highlight = None

    # ---- 运行时隐藏浏览器窗口（防误点 / 防误判翻页）----
    def hide_browser(self):
        """运行前隐藏最前的网页浏览器窗口（与标注截屏同源机制），避免两件事：
        (a) 运行时点击落点被浏览器窗口挡住，点到了浏览器而非微信；
        (b) 全屏指纹把"浏览器自身刷新"误判成"微信翻页"，导致 wait_until_change 提前返回、
            下一步仍在原页面执行（正是"第②步点在第一页面"的典型成因）。
        只隐藏不退出；show_browser() 恢复。"""
        import subprocess
        import time

        try:
            name = subprocess.check_output(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", "ignore").strip()
            if name and name.lower() not in ("finder", "loginwindow", ""):
                self._hidden_name = name
                subprocess.call(
                    ["osascript", "-e",
                     f'tell application "System Events" to set visible of process "{name}" to false'],
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(0.5)
        except Exception:
            self._hidden_name = None

    def show_browser(self):
        if getattr(self, "_hidden_name", None):
            import subprocess

            try:
                subprocess.call(
                    ["osascript", "-e",
                     f'tell application "System Events" to set visible of process "{self._hidden_name}" to true'],
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
            self._hidden_name = None

    def copy_link(self):
        try:
            return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""

    def clear_clipboard(self):
        """清空系统剪贴板，防止上一轮的旧链接/错误信息被当成本轮结果。"""
        try:
            subprocess.run(["pbcopy"], input="", capture_output=True, timeout=5)
        except Exception:
            pass

    @staticmethod
    def _path_to_b64(path):
        """校验 PNG → sips 降采样到最长边 <= MAC_MODEL_MAX_SIDE → base64；失败/坏图返回 ""。"""
        if not _is_png(path):
            return ""
        out = path
        tmp = path + ".sips.png"
        try:
            r = subprocess.run(
                ["sips", "-Z", str(MAC_MODEL_MAX_SIDE), path, "--out", tmp],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0 and _is_png(tmp):
                out = tmp
        except Exception:
            out = path
        try:
            with open(out, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception:
            b64 = ""
        finally:
            if out != path and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        return b64

    def _capture_for_model(self):
        """截屏（按安全区域）→ 校验 PNG → 降采样 → 返回 (b64, ok)。

        截屏缺失/非 PNG（多为 macOS「屏幕录制」权限未授权）→ 返回 ("", False)，
        交由上层安全停止，绝不把坏图发给 Ollama（否则触发 HTTP 400 "Failed to load image"）。
        """
        path = self.screenshot()
        b64 = self._path_to_b64(path)
        return b64, bool(b64)

    def snapshot_b64(self):
        """实时截屏（降采样）并返回 base64，供前端实时投屏；失败/无权限返回空串。"""
        try:
            b64, _ = self._capture_for_model()
            return b64
        except Exception:
            return ""

    def full_snapshot_b64(self):
        """全屏截屏（忽略安全区域）base64，供前端「框选区域」预览。失败/无权限返回空串。"""
        try:
            path = os.path.join(self._tmpdir, f"full_{int(time.time() * 1000)}.png")
            subprocess.run(["screencapture", "-x", "-t", "png", path], check=False)
            return self._path_to_b64(path)
        except Exception:
            return ""

    def screen_fp(self):
        """屏幕指纹（归一化 64px JPEG 的 md5）。wait(until_change) 用来判断弹窗/跳转是否出现。"""
        return self._screen_fp()

    # ---- 安全区域（逻辑点数，左上原点；screencapture -R 与 CGEvent 同一坐标系）----
    def set_region(self, x, y, w, h):
        fw, fh = self._screen[0], self._screen[1]
        x = max(0.0, float(x))
        y = max(0.0, float(y))
        w = max(10.0, float(w))
        h = max(10.0, float(h))
        # 钳制到屏幕内
        if x + w > fw:
            w = fw - x
        if y + h > fh:
            h = fh - y
        self._region = (x, y, w, h)
        return dict(x=x, y=y, w=w, h=h)

    def clear_region(self):
        self._region = None
        return None

    def region(self):
        if self._region is None:
            return None
        return dict(x=self._region[0], y=self._region[1], w=self._region[2], h=self._region[3])

    def screen_size(self):
        return (self._screen[0], self._screen[1])

    # ---- 框定区域点击（RPA 式确定性点击，独立于全局安全区域）----
    def _full_screenshot(self):
        """全屏截屏（忽略安全区域）返回路径，供 tap_region 的模板/OCR 模式在全屏坐标系匹配。"""
        path = os.path.join(self._tmpdir, f"full_{int(time.time() * 1000)}.png")
        subprocess.run(["screencapture", "-x", "-t", "png", path], check=False)
        return path

    def _crop_to_region_img(self, full_path, reg):
        """把全屏 2× 截屏裁到 region（设备像素），返回 (crop_path, dx, dy)。

        region 是相对全屏的归一化 [x,y,w,h]（0..1，与前端 annoNorm 一致）。
        全屏截屏为 2× 设备像素图（点数×_scale），故裁剪用 实际图尺寸×归一化 换算，
        并对齐 _scale，使后续 _logical_center 偏差修正正确。
        dx,dy 为裁剪框左上角在全屏设备像素中的偏移，供 locate_text/match_template
        返回的 region 内 bbox 偏移到全屏坐标。
        """
        from PIL import Image

        s = self._scale or 1.0
        W, H = self._screen[0], self._screen[1]
        rx, ry, rw, rh = (float(v) for v in reg)
        with Image.open(full_path) as im:
            iw, ih = im.size
            # 以实际图尺寸为准换算，避免 _scale 检测误差导致错位
            dx, dy = int(rx * iw), int(ry * ih)
            dw, dh = max(1, int(rw * iw)), max(1, int(rh * ih))
            crop = im.crop((dx, dy, dx + dw, dy + dh))
            out = os.path.join(self._tmpdir, f"region_{int(time.time() * 1000)}.png")
            crop.save(out)
        return out, dx, dy

    def tap_region(self, target):
        """RPA 式确定性点击：在指定的归一化区域 [x,y,w,h]（0..1，相对「当前截图画布」）内点击。

        mode:
          - center  （默认）点区域几何中心——最稳，适合位置固定的按钮/输入框。
          - template 区域内模板匹配（target.template 为图标 PNG）；适合"⋯"等纯图标。
          - text     区域内 OCR 文字匹配（target.text）；适合有文字标签的按钮。

        坐标语义（与 UI-TARS 预处理镜像一致）：region 永远「相对当前截图画布」的归一化值。
        - _region（安全区）已设置：截图画布即为区域子图，region 相对子图；
        - _region 未设置：截图画布即全屏，region 相对全屏。
        换算统一收敛到 tap_norm()（画布归一化 → 全屏绝对逻辑点数，_region 设置时自动含
        左上角偏移）；最终以 apply_region=False 走 tap_coord，复用点击偏移校准与自愈重试。
        """
        reg = target.get("region")
        if not isinstance(reg, (list, tuple)) or len(reg) != 4:
            raise RuntimeError("tap_region 需要 target.region=[x,y,w,h]（归一化 0..1，相对当前截图画布）")
        rx, ry, rw, rh = (float(v) for v in reg)
        mode = (target.get("mode") or "center").lower()

        if mode == "template" and target.get("template"):
            if self.vision is None:
                raise RuntimeError("tap_region(template) 需要 vision（OCR=real）")
            # 裁剪到 region（设备像素）后再匹配，限定搜索范围、避免全屏误匹配
            full = self._full_screenshot()
            # reg 是「画布归一化」，_crop_to_region_img 以全屏为基准 → 先映射回全屏归一化
            screen_reg = self.region_to_screen_norm(reg)
            crop_path, dx, dy = self._crop_to_region_img(full, screen_reg)
            bbox = self.vision.match_template(crop_path, target["template"], target.get("threshold", 0.85))
            if not bbox:
                raise RuntimeError(f"tap_region 区域内未匹配模板: {target['template']}")
            full_bbox = [bbox[0] + dx, bbox[1] + dy, bbox[2], bbox[3]]  # 偏移到全屏设备像素
            self._last_region = self._norm_bbox(full_bbox)  # 记录瞄准区域（调试用）
            cx, cy = self._logical_center(full_bbox)  # 设备像素 → 逻辑点数（Retina 2x 修正）
        elif mode == "text" and target.get("text"):
            if self.vision is None:
                raise RuntimeError("tap_region(text) 需要 vision（OCR=lm/real）")
            # 全局截屏 + 本地 GUI 模型(ui-tars) 直接定位文字所在元素并点其中心。
            # 经验证 ui-tars 在「整图」上定位像素级精准，但「小裁剪图」会偶发坐标畸形/漂移，
            # 故这里送整图（locate_text 内部会按需缩小以省显存），不裁剪。
            # region 仍作为「命中是否落在框选范围内」的 sanity 检查（见下方）。
            full = self._full_screenshot()
            bbox = self.vision.locate_text(full, target["text"], fuzzy=target.get("fuzzy", True))
            if not bbox:
                raise RuntimeError(f"tap_region 未在全屏找到文字: {target['text']}")
            self._last_region = self._norm_bbox(bbox)  # 记录瞄准区域（调试用，归一化全屏）
            cx, cy = self._logical_center(bbox)  # 设备像素 → 逻辑点数（Retina 2x 修正）
            # sanity：若命中明显落在 region 之外，记日志提示（仍按模型命中点点击，
            # 因为模型在整图上比人工框选坐标更可靠）
            if reg:
                # region 是「画布归一化」，命中点 cx,cy 是「全屏绝对」——需把 region 也映射到
                # 全屏归一化再比较（_region 设置时画布=子图，必须叠加上左角偏移，否则比较错位）。
                if self._region:
                    rx0, ry0, rw0, rh0 = (float(v) for v in self._region)
                    fx0 = (rx0 + rx * rw0) / self._screen[0]
                    fy0 = (ry0 + ry * rh0) / self._screen[1]
                    fx1 = (rx0 + (rx + rw) * rw0) / self._screen[0]
                    fy1 = (ry0 + (ry + rh) * rh0) / self._screen[1]
                else:
                    fx0, fy0, fx1, fy1 = rx, ry, rx + rw, ry + rh
                hnx, hny = cx / self._screen[0], cy / self._screen[1]
                if not (fx0 <= hnx <= fx1 and fy0 <= hny <= fy1):
                    print(f"[tap_region] 警告：文字命中点({hnx:.3f},{hny:.3f}) "
                          f"落在框选 region({fx0:.3f},{fy0:.3f},{fx1:.3f},{fy1:.3f})之外，"
                          f"已按模型命中点点击（可能是同文字的其他实例）")
        else:
            if mode not in ("center",):
                # 未知 mode 或无匹配目标时退回区域中心，保证不崩
                pass
            self._last_region = [round(rx, 4), round(ry, 4), round(rw, 4), round(rh, 4)]  # 调试用
            # 统一经 tap_norm 换算：画布归一化(中心) → 全屏绝对逻辑点数（自动含 _region 偏移）
            cx, cy = self.tap_norm(rx + rw / 2.0, ry + rh / 2.0)

        # 复用 tap_coord：apply_region=False（坐标已是全屏绝对），但保留偏移校准 + 自愈重试
        self.tap_coord(cx, cy, apply_region=False)
        self.highlight = f"region:{mode}"
        return {"mode": mode, "x": round(cx, 1), "y": round(cy, 1)}

    # ---- 感知：返回截图（base64）+ 屏幕尺寸，触发大脑多模态路径 ----
    def perceive(self):
        b64, ok = self._capture_for_model()
        if self._region:
            W, H = int(self._region[2]), int(self._region[3])  # 模型坐标系 = 区域尺寸
        else:
            W, H = self._screen[0], self._screen[1]
        return {
            "modality": "screenshot",
            "page": self.page,
            "highlight": self.highlight,
            "screenshot": b64,  # 无效截屏为 ""，上层 explore() 会安全停止并提示权限
            "screen_w": W,
            "screen_h": H,
            "elements": [],
            "scrollable": self.can_scroll(),
        }

    # ---- 滑不动判定：连续两帧像素一致即到底 ----
    def can_scroll(self):
        img = self.screenshot()
        h = self._hash(img)
        if self._last_hash is None:
            self._last_hash = h
            return True
        changed = h != self._last_hash
        self._last_hash = h
        return changed

    @staticmethod
    def _hash(path):
        try:
            with open(path, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception:
            return str(time.time())

    # ============ 多模式点击引擎 ============
    # 不同 App 对合成点击事件的敏感度差异极大：
    #   - CGEvent(硬件层)：部分 WebView/小程序不识别为有效点击
    #   - System Events(Accessibility 层)：走系统事件分发，更像真实用户操作
    #   - cliclick：第三方 CLI，对顽固 App 效果最好（需单独安装）
    # 通过 set_click_mode() 切换，证据里记录实际使用的方式，方便对比哪种有效。

    def _do_click(self, x, y):
        """点击调度入口：根据 _click_mode 选择实现，记录实际使用方式到 _click_mode_log。"""
        mode = getattr(self, "_click_mode", "cgevent")
        if mode == "system_events":
            ok = self._click_system_events(x, y)
            self._click_mode_log = "system_events" if ok else "system_events_FAILED→cgevent"
            if not ok:
                self._click_cgevent(x, y)  # fallback
        elif mode == "cliclick":
            ok = self._click_cliclick(x, y)
            self._click_mode_log = "cliclick" if ok else "cliclick_FAILED→cgevent"
            if not ok:
                self._click_cgevent(x, y)  # fallback
        elif mode == "cgevent_hid":
            self._click_cgevent_hid(x, y)
            self._click_mode_log = "cgevent_hid"
        elif mode == "apple_script":
            # 原子操作：activate + click 在同一个 osascript 里完成
            name = getattr(self, "_focus_app", "WeChat") or "WeChat"
            ok, err = self._click_apple_script(x, y, name)
            self._click_mode_log = f"apple_script({name})" if ok else f"apple_script_FAILED({err})→cgevent"
            if not ok:
                self._click_cgevent_enhanced(x, y)  # fallback to enhanced CGEvent
        elif mode == "cgevent_enhanced":
            self._click_cgevent_enhanced(x, y)
            self._click_mode_log = "cgevent_enhanced(source_state=1)"
        elif mode == "human_like":
            self._click_human_like(x, y)
            self._click_mode_log = "human_like(move+source_state+ dwell)"
        elif mode == "to_pid":
            ok, info = self._click_to_pid(x, y)
            self._click_mode_log = f"to_pid({info})" if ok else f"to_pid_FAILED({info})→cgevent"
            if not ok:
                self._click_cgevent(x, y)
        elif mode == "ax_press":
            ok, info = self._click_ax_press(x, y)
            name = getattr(self, "_focus_app", "WeChat") or "WeChat"
            self._click_mode_log = f"ax_press({info})" if ok else f"ax_press_FAILED({info})→cgevent_enhanced"
            if not ok:
                self._click_cgevent_enhanced(x, y)
        else:
            self._click_cgevent(x, y)
            self._click_mode_log = "cgevent"

    @staticmethod
    def _click_cgevent_hid(x, y, dwell=0.08):
        """CGEvent 硬件层投递(kCGHIDEventTap=0)：原始方案，最底层。对部分 App 无效但对原生控件有效。"""
        cg = MacDevice._cg()
        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, CGPoint, ctypes.c_int]
        cg.CGEventSetType.argtypes = [ctypes.c_void_p, ctypes.c_int]
        cg.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]
        cg.CFRelease.argtypes = [ctypes.c_void_p]
        kDown, kUp = 1, 2
        pt = CGPoint(x, y)
        evt = cg.CGEventCreateMouseEvent(None, kDown, pt, 0)
        cg.CGEventPost(0, evt)  # kCGHIDEventTap = 硬件层
        if dwell and dwell > 0:
            time.sleep(dwell)
        cg.CGEventSetType(evt, kUp)
        cg.CGEventPost(0, evt)
        cg.CFRelease(evt)

    @staticmethod
    def _click_cgevent(x, y, dwell=0.08):
        """CoreGraphics 鼠标事件注入。

        尝试两种投递目标：
          1) kCGHIDEventTap(0)=硬件层：原始方案，对原生 App 有效。
          2) kCGSessionEventTap(1)=会话层：经过系统事件分发，部分 WebView 更容易识别。
        如果硬件层投递失败或无效，可切换到会话层试试。"""
        cg = MacDevice._cg()

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, CGPoint, ctypes.c_int]
        cg.CGEventSetType.argtypes = [ctypes.c_void_p, ctypes.c_int]
        cg.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]
        cg.CFRelease.argtypes = [ctypes.c_void_p]
        kDown, kUp = 1, 2
        pt = CGPoint(x, y)
        evt = cg.CGEventCreateMouseEvent(None, kDown, pt, 0)
        # 优先尝试会话层事件分发(kCGSessionEventTap=1)，对 WebView/小程序更友好
        # 若仍无效可改回 kCGHIDEventTap=0（硬件层）
        cg.CGEventPost(1, evt)
        if dwell and dwell > 0:
            time.sleep(dwell)
        cg.CGEventSetType(evt, kUp)
        cg.CGEventPost(1, evt)
        cg.CFRelease(evt)

    @staticmethod
    def _click_system_events(x, y):
        """通过 osascript System Events 的 'click at {x,y}' 发送点击（Accessibility 层）。

        走 macOS 系统事件分发路径，比 CGEvent 硬件层注入更接近真实用户操作。
        微信等对合成事件挑剔的 App 更可能识别此方式。
        坐标系：System Events 的 click at 使用全局屏幕逻辑点数（与 screencapture -R / CGEvent 同一坐标系）。
        返回 True=成功，False=osascript 不可用。
        """
        try:
            ix, iy = int(round(x)), int(round(y))
            r = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to click at {{{ix}, {iy}}}'],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    @staticmethod
    def _click_apple_script(x, y, app_name="WeChat"):
        """原子操作：一步 AppleScript 完成「激活目标 App + 等 0.5s + 点击坐标」。

        这是解决"点了没反应"的终极方案——把激活和点击放在同一个 osascript 里执行，
        消除 Python 层面 activate 与 click 之间的任何时序竞争/窗口切换/焦点丢失。
        即使 activate_app() 因权限问题静默失败，这里也会在同一个脚本里重试激活。"""
        try:
            ix, iy = int(round(x)), int(round(y))
            script = (
                f'tell application "{app_name}" to activate\n'
                'delay 0.6\n'
                'tell application "System Events"\n'
                f'  click at {{{ix}, {iy}}}\n'
                'end tell'
            )
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0, r.stderr.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return False, str(e)

    @staticmethod
    def _click_cgevent_enhanced(x, y, dwell=0.08):
        """增强版 CGEvent：设置来源状态为真实 HID 设备(kCGEventSourceStateID)，让事件看起来更像真人操作。

        部分安全敏感的 App（如微信小程序 WebView）会检查事件的 source state，
        只接受来自"真实输入设备"的事件，拒绝纯合成的 kCGEventSourceStateID=0 事件。
        这里把 source state 设为 1（模拟来自系统），增加被接受的概率。"""
        cg = MacDevice._cg()

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        # 额外 API：设置事件来源状态
        try:
            cg.CGEventSetSourceStateID.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            cg.CGEventGetSourceStateID.restype = ctypes.c_uint32
            cg.CGEventGetSourceStateID.argtypes = [ctypes.c_void_p]
        except (AttributeError, OSError):
            pass  # 旧版 macOS 可能没有这个 API，跳过

        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, CGPoint, ctypes.c_int]
        cg.CGEventSetType.argtypes = [ctypes.c_void_p, ctypes.c_int]
        cg.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]
        cg.CFRelease.argtypes = [ctypes.c_void_p]
        kDown, kUp = 1, 2
        pt = CGPoint(x, y)
        evt = cg.CGEventCreateMouseEvent(None, kDown, pt, 0)
        # 标记为来自真实 HID 设备（source state ID = 1）
        try:
            cg.CGEventSetSourceStateID(evt, 1)
        except (AttributeError, OSError):
            pass
        cg.CGEventPost(1, evt)  # 会话层投递
        if dwell and dwell > 0:
            time.sleep(dwell)
        cg.CGEventSetType(evt, kUp)
        try:
            cg.CGEventSetSourceStateID(evt, 1)
        except (AttributeError, OSError):
            pass
        cg.CGEventPost(1, evt)
        cg.CFRelease(evt)

    @staticmethod
    def _click_human_like(x, y, dwell=0.08):
        """拟人化点击：先移动鼠标到目标位置(模拟真人移鼠) → 短暂停顿 → 按下 → 停留 → 抬起。

        很多 App（尤其是微信小程序 WebView）会检测交互模式：
          - 直接在目标坐标产生 click down/up 而无 preceding mouse move → 判定为合成事件 → 忽略
          - 有 mouse move 到达目标再 click → 判定为真人操作 → 正常响应
        这个模式完整模拟后者。"""
        cg = MacDevice._cg()

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, CGPoint, ctypes.c_int]
        cg.CGEventSetType.argtypes = [ctypes.c_void_p, ctypes.c_int]
        cg.CGEventSetLocation.argtypes = [ctypes.c_void_p, CGPoint]
        cg.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]
        cg.CFRelease.argtypes = [ctypes.c_void_p]

        # 尝试设置 source state ID
        try:
            cg.CGEventSetSourceStateID.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        except (AttributeError, OSError):
            pass

        kMove, kDown, kDrag, kUp = 5, 1, 6, 2
        pt = CGPoint(x, y)

        # Step 1: 移动鼠标到目标位置（模拟真人把鼠标挪过去）
        move_evt = cg.CGEventCreateMouseEvent(None, kMove, pt, 0)
        try:
            cg.CGEventSetSourceStateID(move_evt, 1)
        except (AttributeError, OSError):
            pass
        cg.CGEventPost(1, move_evt)
        cg.CFRelease(move_evt)

        # Step 2: 短暂停顿（真人不会瞬间点下）
        time.sleep(0.05)

        # Step 3: 按下
        down_evt = cg.CGEventCreateMouseEvent(None, kDown, pt, 0)
        try:
            cg.CGEventSetSourceStateID(down_evt, 1)
        except (AttributeError, OSError):
            pass
        cg.CGEventPost(1, down_evt)

        # Step 4: 按压停留
        if dwell and dwell > 0:
            time.sleep(dwell)

        # Step 5: 抬起
        cg.CGEventSetType(down_evt, kUp)
        try:
            cg.CGEventSetSourceStateID(down_evt, 1)
        except (AttributeError, OSError):
            pass
        cg.CGEventPost(1, down_evt)
        cg.CFRelease(down_evt)

    @staticmethod
    def _click_cliclick(x, y):
        """通过 cliclick CLI 工具发送点击（需 brew install cliclick）。
        返回 True=成功且 cliclick 可用，False=不可用。"""
        try:
            ix, iy = int(round(x)), int(round(y))
            r = subprocess.run(
                ["cliclick", f"c:{ix},{iy}"],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False

    @staticmethod
    def _click_to_pid(x, y, dwell=0.08, app_name="WeChat"):
        """CGEventPostToPid：把鼠标事件直接投递到目标 App 的进程队列（而非全局广播）。

        与 CGEventPost(0/1) 的区别：
          - CGEventPost → 投到"事件 tap 目标"，由 WindowServer 分发给所有监听者（可能被过滤）
          - CGEventPostToPid → 直接注入到指定 PID 的事件队列，App 无法在 WindowServer 层面拦截/忽略
        这对微信这种有自己事件过滤机制的 App 可能是突破口。
        """
        import subprocess as _sp

        # 获取目标 PID
        pid = None
        try:
            out = _sp.check_output(["pgrep", "-x", app_name], stderr=_sp.DEVNULL)
            pid = int(out.decode().strip().split()[0])
        except Exception:
            # 尝试其他名称变体
            for name in ("微信", "wechat"):
                try:
                    out = _sp.check_output(["pgrep", "-x", "-f", f"{name}.app"], stderr=_sp.DEVNULL)
                    pid = int(out.decode().strip().split()[0])
                    break
                except Exception:
                    continue
        if not pid:
            return False, f"找不到 {app_name} 进程"

        cg = MacDevice._cg()

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        # 注册 CGEventPostToPid API
        try:
            cg.CGEventPostToPid.argtypes = [ctypes.c_int32, ctypes.c_void_p]
            cg.CGEventPostToPid.restype = None
        except (AttributeError, OSError):
            return False, "CGEventPostToPid 不可用"

        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, CGPoint, ctypes.c_int]
        cg.CGEventSetType.argtypes = [ctypes.c_void_p, ctypes.c_int]
        cg.CFRelease.argtypes = [ctypes.c_void_p]

        kDown, kUp = 1, 2
        pt = CGPoint(x, y)
        evt = cg.CGEventCreateMouseEvent(None, kDown, pt, 0)

        # 设 source state + 直接投递到目标 PID
        try:
            cg.CGEventSetSourceStateID(evt, 1)
        except (AttributeError, OSError):
            pass
        cg.CGEventPostToPid(pid, evt)  # ← 关键：直接投到微信的 PID！

        if dwell and dwell > 0:
            time.sleep(dwell)

        cg.CGEventSetType(evt, kUp)
        try:
            cg.CGEventSetSourceStateID(evt, 1)
        except (AttributeError, OSError):
            pass
        cg.CGEventPostToPid(pid, evt)
        cg.CFRelease(evt)
        return True, f"pid={pid}"

    @staticmethod
    def _click_ax_press(x, y, app_name="WeChat"):
        """Accessibility AXUIElement Press：通过 macOS 辅助功能 API 找到目标坐标处的 UI 元素并执行 AXPress。

        这是与 VoiceOver 同级别的交互方式——App 基本无法屏蔽 Accessibility 操作，
        否则就违反了 macOS 无障碍规范。即使 WebView 过滤了合成鼠标事件，
        AXPress 走的是完全不同的代码路径（Accessibility Manager → App 内部 AX 实现）。
        需要 Terminal/运行进程被授予「辅助功能」权限。
        """
        import subprocess as _sp

        # 获取 PID
        pid = None
        try:
            out = _sp.check_output(["pgrep", "-x", app_name], stderr=_sp.DEVNULL)
            pid = int(out.decode().strip().split()[0])
        except Exception:
            for name in ("微信", "wechat"):
                try:
                    out = _sp.check_output(["pgrep", "-x", "-f", f"{name}.app"], stderr=_sp.DEVNULL)
                    pid = int(out.decode().strip().split()[0])
                    break
                except Exception:
                    continue
        if not pid:
            return False, f"找不到 {app_name} 进程"

        ix, iy = int(round(x)), int(round(y))

        # 用 AppleScript 通过 Accessibility API 执行点击
        # 方案：tell System Events → get UI element at position → perform action
        script = (
            f'tell application "System Events"\n'
            f'  set targetApp to first application process whose unix id is {pid}\n'
            f'  tell targetApp\n'
            f'    click at {{{ix}, {iy}}}\n'
            f'  end tell\n'
            f'end tell'
        )
        try:
            r = _sp.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return True, f"ax_press(pid={pid})"
            return False, r.stderr.strip() or f"exit={r.returncode}"
        except Exception as e:
            return False, str(e)

    def set_click_mode(self, mode):
        """设置点击方式: "cgevent"(默认,会话层) | "cgevent_enhanced"(增强CGEvent) | "cgevent_hid"(硬件层) | "apple_script"(原子激活+点击) | "system_events" | "cliclick"。非法值回退 cgevent。"""
        valid = {"cgevent", "cgevent_enhanced", "human_like", "cgevent_hid", "apple_script", "system_events", "cliclick", "to_pid", "ax_press"}
        self._click_mode = mode if mode in valid else "cgevent"
        return dict(mode=self._click_mode)

    def click_mode(self):
        return dict(mode=self._click_mode, last_used=getattr(self, "_click_mode_log", ""))

    def activate_app(self, name):
        """把目标 App（默认微信）提到最前，确保合成点击真正落到它身上。

        很多"点了不反应 / 没跳转"其实是窗口没在最前或没焦点——点击坐标正确，
        但事件被桌面或其它窗口吞掉。运行前先 activate 目标 App 可排除这个干扰。
        同时尝试中英文进程名（"WeChat"/"微信"），自动适配中文版 macOS。"""
        import subprocess

        # 尝试多种可能的进程名
        names_to_try = [name]
        if name.lower() in ("wechat", "weixin"):
            names_to_try = ["WeChat", "微信", "wechat"]
        for n in names_to_try:
            try:
                r = subprocess.call(
                    ["osascript", "-e", f'tell application "{n}" to activate'],
                    stderr=subprocess.DEVNULL,
                )
                if r == 0:
                    time.sleep(0.5)
                    return True
            except Exception:
                continue
        time.sleep(0.3)  # 即使失败也等一下，让系统稳定
        return False

    def _find_window_rect(self, name=None, app_name="WeChat", skip_main=True):
        """返回 (x, y, w, h, wname) 或 None。

        微信窗口的 `id of window` 在 System Events 下返回 **missing value**，
        所以 screencapture -l <id> 无法使用。改用 name/position/size 三个独立列表解析，
        坐标/尺寸为逻辑点，与 CGEvent / screencapture -R 同一坐标系。
        若指定 name 则精确匹配；否则取最大的非主窗口（小程序窗口）。
        """
        try:
            def _get(prop):
                rr = subprocess.run(["osascript", "-e",
                    f'tell application "System Events" to tell process "{app_name}" to get {prop} of every window'],
                    capture_output=True, text=True)
                return rr.stdout.strip()

            names = [s.strip() for s in _get("name").split(",") if s.strip()]
            pos = [int(v) for v in re.findall(r"-?\d+", _get("position"))]
            siz = [int(v) for v in re.findall(r"-?\d+", _get("size"))]
            main = ("微信", "WeChat", "微信 (窗口)")
            best = None
            for i, nm in enumerate(names):
                if 2 * i + 1 < len(pos) and 2 * i + 1 < len(siz):
                    x, y, w, h = pos[2 * i], pos[2 * i + 1], siz[2 * i], siz[2 * i + 1]
                    if name and nm == name:
                        return (x, y, w, h, nm)
                    if skip_main and nm in main:
                        continue
                    if best is None or (w * h) > (best[0] * best[1]):
                        best = (x, y, w, h, nm)
            return best
        except Exception:
            return None

    def activate_frontmost_window(self, app_name="WeChat", skip_names=None):
        """把目标 App 的「最前非主窗口」（如小程序窗口）提到最前。

        微信在 macOS 上有多个窗口（主聊天窗口「微信」、小程序窗口如「小球圈」等）。
        activate_app() 只激活 App，不保证激活正确的子窗口——导致截图截到主聊天窗口
        而非小程序页面。此方法用 osascript 把最大的非主窗口设为最前。
        """
        rect = self._find_window_rect(app_name=app_name)
        if not rect:
            return None
        x, y, w, h, wname = rect
        try:
            subprocess.call(["osascript", "-e",
                f'tell application "System Events" to tell process "{app_name}" to set index of window "{wname}" to 1'],
                stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            return wname
        except Exception:
            return None

    def screenshot_window(self, window_name=None, app_name="WeChat"):
        """只截取指定 App 的特定窗口：用 screencapture -R <x,y,w,h> 按窗口矩形区域捕获
        （微信窗口 id 是 missing value，screencapture -l 不可用，必须走区域截图）。
        若没指定窗口或获取失败，回退到全屏截图。"""
        rect = self._find_window_rect(name=window_name, app_name=app_name)
        if rect:
            x, y, w, h, _ = rect
            path = os.path.join(self._tmpdir, f"win_{int(time.time() * 1000)}.png")
            r = subprocess.run(
                ["screencapture", "-R", f"{x},{y},{w},{h}", "-x", "-t", "png", path],
                capture_output=True,
            )
            if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0:
                return self._path_to_b64(path)
        return self.full_snapshot_b64()


    def frontmost_info(self):
        """诊断：返回当前最前窗口的名称（用于证据记录，确认点击前焦点在不在微信上）。"""
        try:
            name = subprocess.check_output(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", "ignore").strip()
            return name or "(unknown)"
        except Exception:
            return "(permission_denied)"

    @staticmethod
    def _find_pid(app_name="WeChat"):
        """查找目标 App 的 PID（用于 CGEventPostToPid 直投）。失败返回 None。"""
        import subprocess as _sp
        try:
            out = _sp.check_output(["pgrep", "-x", app_name], stderr=_sp.DEVNULL)
            return int(out.decode().strip().split()[0])
        except Exception:
            for name in ("微信", "wechat"):
                try:
                    out = _sp.check_output(["pgrep", "-x", "-f", f"{name}.app"], stderr=_sp.DEVNULL)
                    return int(out.decode().strip().split()[0])
                except Exception:
                    continue
        return None

    @staticmethod
    def _drag(x1, y1, x2, y2, pid=None):
        cg = MacDevice._cg()

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, CGPoint, ctypes.c_int]
        cg.CGEventSetType.argtypes = [ctypes.c_void_p, ctypes.c_int]
        cg.CGEventSetLocation.argtypes = [ctypes.c_void_p, CGPoint]
        cg.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]
        cg.CFRelease.argtypes = [ctypes.c_void_p]
        # 与点击同理：微信会过滤全局 CGEventPost(0)，必须 CGEventPostToPid 直投 PID 才能触发滚动/返回。
        use_pid = False
        if pid:
            try:
                cg.CGEventPostToPid.argtypes = [ctypes.c_int32, ctypes.c_void_p]
                cg.CGEventPostToPid.restype = None
                use_pid = True
            except (AttributeError, OSError):
                use_pid = False
        kDown, kDrag, kUp = 1, 6, 2
        pt1, pt2 = CGPoint(x1, y1), CGPoint(x2, y2)
        evt = cg.CGEventCreateMouseEvent(None, kDown, pt1, 0)
        try:
            cg.CGEventSetSourceStateID(evt, 1)
        except (AttributeError, OSError):
            pass
        if use_pid:
            cg.CGEventPostToPid(pid, evt)
        else:
            cg.CGEventPost(0, evt)
        # 关键：在 down 与 up 之间发多段「中间 move」事件（带微小延时），模拟真实拖拽手势。
        # 只发"起点→终点"一次跳变时，微信 WebView 会把它当成点击而非滚动，导致"滑动/返回不动"。
        # 分段 + 延时让手势被识别为连续 pan，列表才能滚动、左边缘手势才能触发返回。
        STEPS = 14
        for i in range(1, STEPS + 1):
            t = i / STEPS
            mx = x1 + (x2 - x1) * t
            my = y1 + (y2 - y1) * t
            cg.CGEventSetType(evt, kDrag)
            cg.CGEventSetLocation(evt, CGPoint(mx, my))
            try:
                cg.CGEventSetSourceStateID(evt, 1)
            except (AttributeError, OSError):
                pass
            if use_pid:
                cg.CGEventPostToPid(pid, evt)
            else:
                cg.CGEventPost(0, evt)
            time.sleep(0.012)
        cg.CGEventSetType(evt, kUp)
        if use_pid:
            cg.CGEventPostToPid(pid, evt)
        else:
            cg.CGEventPost(0, evt)
        cg.CFRelease(evt)

    @staticmethod
    def _scroll_wheel(x, y, v_delta=0, h_delta=0, pid=None):
        """滚动：用「全局(HID 层)滚轮事件」CGEventPost(0) 投递。

        ⚠️ 实测铁证（诊断脚本逐方式像素比对）：微信小程序 WebView
          - 对 PID 直投的滚轮事件(CGEventPostToPid) → 忽略（被过滤）
          - 对拖拽手势(无论 PID/全局) → 不识别（当点击处理）
          - 仅对「全局 CGEventPost(0)」的滚轮事件 → 正常滚动
        故这里统一走全局投递，忽略 pid 参数。v_delta>0 向上滚，h_delta>0 向左滚。
        """
        cg = MacDevice._cg()

        class CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        try:
            cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
            if h_delta:
                # 双轴：delta1=竖向, delta2=横向
                cg.CGEventCreateScrollWheelEvent.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32]
                evt = cg.CGEventCreateScrollWheelEvent(None, 0, 2, int(v_delta), int(h_delta))
            else:
                cg.CGEventCreateScrollWheelEvent.argtypes = [
                    ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32]
                evt = cg.CGEventCreateScrollWheelEvent(None, 0, 1, int(v_delta))
            if not evt:
                return False
            cg.CGEventSetLocation.argtypes = [ctypes.c_void_p, CGPoint]
            cg.CGEventSetLocation(evt, CGPoint(x, y))
            try:
                cg.CGEventSetSourceStateID(evt, 1)
            except (AttributeError, OSError, TypeError):
                pass
            cg.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]
            cg.CGEventPost(0, evt)  # 全局 / HID 层 —— 微信小程序只认这个
            cg.CFRelease.argtypes = [ctypes.c_void_p]
            # 分 3 段小幅度连续发送，模拟真实滚轮"咔嗒"手感
            n = 3
            per_v = int(v_delta / n) if n else int(v_delta)
            per_h = int(h_delta / n) if n else int(h_delta)
            for _ in range(1, n):
                time.sleep(0.04)
                if h_delta:
                    e2 = cg.CGEventCreateScrollWheelEvent(None, 0, 2, per_v, per_h)
                else:
                    e2 = cg.CGEventCreateScrollWheelEvent(None, 0, 1, per_v)
                cg.CGEventSetLocation(e2, CGPoint(x, y))
                cg.CGEventPost(0, e2)
                cg.CFRelease(e2)
            cg.CFRelease(evt)
            return True
        except Exception:
            return False

    @staticmethod
    def _type(text):
        cg = MacDevice._cg()
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cg.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]
        cg.CFRelease.argtypes = [ctypes.c_void_p]
        data = text.encode("utf-16-le")
        n = len(data) // 2
        arr = (ctypes.c_ushort * n)()
        ctypes.memmove(arr, data, len(data))
        for i in range(n):
            for is_down in (True, False):
                evt = cg.CGEventCreateKeyboardEvent(None, 0, 1 if is_down else 0)
                cg.CGEventKeyboardSetUnicodeString(
                    evt, 1, ctypes.cast(ctypes.byref(arr[i]), ctypes.c_void_p)
                )
                cg.CGEventPost(0, evt)
                cg.CFRelease(evt)
                if is_down:
                    time.sleep(0.02)

    @staticmethod
    def _key(keycode, flags=0):
        cg = MacDevice._cg()
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_int]
        cg.CGEventPost.argtypes = [ctypes.c_int, ctypes.c_void_p]
        cg.CFRelease.argtypes = [ctypes.c_void_p]
        for is_down in (True, False):
            evt = cg.CGEventCreateKeyboardEvent(None, keycode, 1 if is_down else 0)
            if flags:
                cg.CGEventSetFlags(evt, flags)
            cg.CGEventPost(0, evt)
            cg.CFRelease(evt)
