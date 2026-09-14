"""运行时原语：事件总线、执行控制、异常。"""
import threading
import queue


class StopExecution(Exception):
    """由控制层（stop）或失败策略（abort）抛出，终止执行。"""
    pass


class Bus:
    """极简发布/订阅，用于把引擎事件推给 SSE 与状态消费者。"""

    def __init__(self):
        self._subs = []

    def subscribe(self):
        q = queue.Queue()
        self._subs.append(q)
        return q

    def publish(self, obj):
        for q in self._subs:
            try:
                q.put(obj)
            except Exception:
                pass


class Control:
    """执行控制：idle / running / paused / stepping / stopped。
    stepping = 单步执行完一步后自动回到 paused。"""

    def __init__(self):
        self.mode = "idle"
        self.speed = 0.6
        self._ev = threading.Event()

    def signal(self, mode):
        self.mode = mode
        self._ev.set()

    def wake(self):
        self._ev.set()
