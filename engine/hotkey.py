"""键盘安全开关（纯标准库，零原生依赖，绝不导致 Python 崩溃）。

agent 接管鼠标键盘时提供「随时可打断」的能力。在运行 app.py 的那个终端里按键：
  s = 紧急停止（explore / replay 立即终止）
  p = 暂停 / 继续（toggle）
  q = 停止并退出进程

实现说明：
- 早期版本用 CoreGraphics CGEventTap 做全局拦截，但 ctypes 调用在部分 macOS 上
  会引发原生崩溃（"Python 意外退出"），故改为读取终端 stdin（cbreak 模式）。
- 仅在 stdin 是真实终端（TTY）时启用；管道 / IDE 启动自动跳过，此时用 Ctrl+C 终止。
- 该监听读取「运行 app.py 的终端」输入，因此需该终端聚焦。若 agent 正在操作屏幕，
  可随时 Cmd+Tab 回终端按 s 急停，或直接 Ctrl+C 杀掉进程。
"""
import os
import sys
import threading
import termios
import tty


class HotkeyMonitor:
    def __init__(self, control):
        self.control = control
        self._thread = None

    def start(self):
        """启动后台监听线程；仅在真实终端(TTY)下启用，成功返回 True。"""
        if not sys.stdin.isatty():
            return False
        try:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True
        except Exception:
            return False

    def _run(self):
        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except Exception:
            return
        self._fd = fd
        self._old = old
        try:
            # cbreak：关闭行缓冲与回显，但保留信号（Ctrl+C 仍能杀进程）
            tty.setcbreak(fd)
            while True:
                ch = sys.stdin.read(1)
                if not ch:
                    break
                if ch in ("s", "S"):
                    self.control.signal("stopped")
                elif ch in ("p", "P"):
                    if self.control.mode == "paused":
                        self.control.mode = "running"
                        self.control.wake()
                    else:
                        self.control.signal("paused")
                elif ch in ("q", "Q"):
                    self.control.signal("stopped")
                    os._exit(0)
        except Exception:
            pass
        finally:
            self.restore()

    def restore(self):
        """还原终端属性（避免退出后终端无回显）。"""
        if getattr(self, "_old", None) is not None and getattr(self, "_fd", None) is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except Exception:
                pass
            self._old = None
