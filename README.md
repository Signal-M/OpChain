# OpChain · 本地多模态 GUI Agent 自动化引擎

> **Agent 生成 RPA，确定性重放复用。** 把"看屏 → 决策 → 点击"的 GUI 操作声明成一份 JSON **操作链路**，用本地多模态模型（Ollama / Qwen-VL / UI-TARS）驱动探索；探索完成后自动**合成确定性链路**，之后交给解释器**零模型调用免费重放**。

OpChain 是一个本地优先（local-first）的 GUI 自动化引擎：后端纯 Python 标准库、前端零构建原生 JS，开箱即跑。设备与识别均为**可插拔适配器**——换场景改配置、换环境换适配器。内置适配器已在 **macOS 本机（Apple Silicon / Intel）**、**Android（ADB）**、**Windows PC 客户端**三类环境验证，其中 macOS 本机以某小程序 WebView 作为首要验证场景。

## 特性

- **混合架构**：GUI Agent 探索一次 → 合成确定性链路 → 原解释器零模型调用重放（秒级、可批量复用）。
- **声明式链路**：循环 / 子链 / 变量 / emit 全部 JSON 描述，引擎只解释执行。
- **实时可观测**：仿手机投屏 + 命中高亮、步骤时间线、分级日志、抽取数据表格（可导出 CSV）。
- **多模态大脑可插拔**：默认 `MockBrain`（零依赖状态机离线可跑），可切换云端/本地开源视觉语言模型。
- **零依赖起步**：Mock 模式仅需 Python 3.8+，无需装任何包。

## 快速开始（Mock 模式，零依赖）

```bash
git clone <your-repo-url> OpChain
cd OpChain
python app.py            # Python 3.8+ 自带即可
# 浏览器打开 http://127.0.0.1:8000
```

> **Windows 注意**：`DEVICE=... python app.py` 是 bash 语法，在 `cmd` 里拆成多行用 `set`：
> ```cmd
> cd OpChain
> set DEVICE=windows
> set OCR=real
> python app.py
> ```
> PowerShell 则用 `$env:DEVICE="windows"; $env:OCR="real"; python app.py`。

## 界面功能

- **执行控制**：开始 / 暂停 / 单步 / 停止 / 重置，速度可调。
- **实时投屏**：仿手机画面，随步骤显示当前页面与命中元素高亮。
- **步骤时间线**：每步状态（执行中/成功/失败/跳过）实时上色。
- **实时日志**：INFO / WARN / ERROR 分级。
- **抽取数据**：场地名 / 地址 / 链接 等字段实时流入表格，可导出 CSV。
- **GUI Agent 面板**：探索并合成链路、重放合成链路、重置；右侧「Agent 推理」页实时展示 `感知 → 规划 : 理由 → 校验`。

## 接入真机（可插拔适配器）

```bash
# 1) macOS 本机（Apple Silicon / Intel，零第三方依赖）
#    前置：系统设置 → 隐私与安全性 → 辅助功能 与 屏幕录制，授权运行本进程（Terminal / Python.app）
export DEVICE=mac
export BRAIN=uitars                       # UI-TARS 原生协议大脑（坐标直出，无需 OCR）
export BRAIN_BASE_URL=http://127.0.0.1:11434/v1
export BRAIN_MODEL=ui-tars:7b             # 本地 Ollama 部署
export BRAIN_API_KEY=ollama               # 本地 Ollama 占位即可
python app.py

# 2) Android 模拟器 / 真机
pip install paddleocr opencv-python
DEVICE=adb OCR=real python app.py
# 多设备指定序列号：ADB_SERIAL=emulator-5554

# 3) Windows PC 客户端
pip install paddleocr opencv-python pywinauto pyperclip pillow
DEVICE=windows OCR=real python app.py

# 默认（无硬件也能演示）
python app.py
```

### 环境变量一览

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DEVICE` | 设备适配器：`mock` / `mac` / `adb` / `windows` | `mock` |
| `OCR` | 识别引擎：`mock` / `lm` / `real` | `mock`（mac 下默认 `lm`） |
| `BRAIN` | 大脑：`mock` / `cloud` / `local` / `uitars` | `mock` |
| `BRAIN_BASE_URL` | 模型 OpenAI 兼容网关（云端/本地） | 空 |
| `BRAIN_MODEL` | 模型名（如 `qwen2.5vl:3b` / `ui-tars:7b`） | 空 |
| `BRAIN_API_KEY` | 云端密钥；本地 Ollama 填 `ollama` 占位 | 空 |
| `PORT` | 服务端口 | `8000` |
| `ADB_SERIAL` | 多设备时指定序列号 | 空 |

## 大脑（模型）切换

`engine/brain.py` 是**模型无关**的，默认 `MockBrain`（零依赖状态机）。可切换为真实多模态模型驱动探索：

```bash
# 云端（OpenAI 兼容接口，如阿里云百炼 / DashScope）
export BRAIN=cloud
export BRAIN_API_KEY=sk-xxxx
export BRAIN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export BRAIN_MODEL=qwen-plus
python app.py

# 本地开源（Ollama 部署 Qwen2.5-VL，已在 MacBook Air M5 16GB 验证）
#   1) 启动 Ollama（打开 Ollama.app 或 `ollama serve`，默认监听 127.0.0.1:11434）
#   2) 拉取模型：ollama pull qwen2.5vl:3b   # ~3.2GB，16GB 机型可跑；7B 更稳但更吃内存
export BRAIN=local
export BRAIN_BASE_URL=http://127.0.0.1:11434/v1
export BRAIN_MODEL=qwen2.5vl:3b
export BRAIN_API_KEY=ollama
python app.py
```

> 模型未配置或调用失败时，`LLMBrain` 会安全回退为 `stop_explore` 并在日志提示，不会阻塞原型。

### UI-TARS 大脑（macOS 本机推荐搭档）

`UITARSBrain`（`BRAIN=uitars`）让本地部署的 **UI-TARS**（坐标直出模型）输出其原生 `Thought / Action` 协议，再容错翻译为引擎 DSL（`tap_coord` / `swipe` / `type_text` / `back` / `copy_link` / `stop_explore`）。搭配 `DEVICE=mac` 时**无需 OCR 依赖**即可端到端跑通「看屏 → 规划 → 点击」。

## 目录结构

```
OpChain/
├── app.py                 # 后端：REST + SSE + 控制（含 Agent 端点）
├── engine/
│   ├── runtime.py         # Bus（事件总线）/ Control（执行控制）
│   ├── interpreter.py     # DSL 解释器：loop/子链/变量/emit（重放阶段复用）
│   ├── devices.py         # MockDevice / ADBDevice / WindowsUIDevice / MacDevice
│   ├── vision.py          # MockVision / RealVision（PaddleOCR + 模板匹配）
│   ├── brain.py           # 大脑接口：MockBrain / LLMBrain / UITARSBrain
│   ├── agent.py           # GUI Agent：探索 → 合成确定性链路 → 重放
│   └── loader.py          # 加载主链路 + 子链
├── chains/
│   ├── tennis_booking.json      # 示例主链路（列表遍历抓取）
│   └── copy_link_subchain.json  # 示例子链（复制链接）
├── static/index.html      # 前端仪表盘（含 Agent 面板 + 推理流）
├── _smoke_test.py         # 冒烟测试：探索→合成→重放
└── requirements.txt       # 真机/真实识别按需依赖
```

## 架构核心

链路声明化为 JSON，引擎只解释执行，设备与识别为可插拔适配器——换场景改配置、换环境换适配器。GUI Agent 负责"探索并生成确定性链路"，生成后的链路由解释器**零模型调用重放**，兼顾智能与成本。

## 合规声明

⚠️ **本项目仅供学习与研究目的。** 自动化操作第三方应用（尤其是带有反自动化机制的平台）可能违反其服务条款或相关法律法规。使用者须自行评估并承担全部合规责任；本项目不对任何滥用导致的账号封禁、法律责任或数据损失负责。请勿将本项目用于任何未获授权的访问或商业爬取。

## License

[MIT](./LICENSE) © 2026 The OpChain Authors
