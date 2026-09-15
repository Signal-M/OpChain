# OpChain · 本地多模态 GUI workflow 自动化引擎

![CI](https://github.com/m2290526022-boop/OpChain/actions/workflows/ci.yml/badge.svg)

## 背景
OpChain 由 wechat-miniapp-link-copier 演进而来，是一个本地运行的 GUI workflow 自动化引擎。

**问题场景：**做网球约球小程序时，需采集网球场地的详细信息与订场链接。这类数据没有公开 API、也无法通过爬虫获取，从竞品小程序逐个人工收集成本极高。

**设计思路：**借鉴 computer use 与自动化测试的思想，把「人操作电脑」的过程拆解为可编排、可复用的自动化流程。

**执行策略：**确定性优先、模型兜底——位置固定的动作用坐标/区域定位直接模拟人工点击；需要视觉定位与判断的环节交由多模态大模型处理，以应对执行中的不确定性。

**编排执行：**全流程通过workflow 编排，支持循环，一次编排即可自动循环执行。
<img width="2940" height="1602" alt="baf8e15bceeb30eb233fb5e0f78c6854" src="https://github.com/user-attachments/assets/23d5ab72-a9f8-4b1e-9d0e-802a5e6bce3b" />
<img width="2940" height="1602" alt="cd4cff61e5bfcac4ff1461772498ef2c" src="https://github.com/user-attachments/assets/e112fd1a-98e0-42a2-bd09-2d8904b87471" />
<img width="2836" height="1514" alt="d68f74f945b1bbc57d341442db933f2f" src="https://github.com/user-attachments/assets/de33ba5a-40e4-43d1-b139-7b3adbfa6a10" />



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
- **标注模式**：创建自动化工作流，可以编辑工作流每个步骤执行的动作和执行动作的区域。执行动作可基于固定坐标执行，也可以通过大模型做视觉定位和理解然后再执行
- **执行控制**：开始 / 暂停 / 单步 / 停止 / 重置，速度可调。
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

# 2) Windows PC 客户端
pip install paddleocr opencv-python pywinauto pyperclip pillow
DEVICE=windows OCR=real python app.py



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


### UI-TARS 大脑（macOS 本机推荐搭档）

`UITARSBrain`（`BRAIN=uitars`）让本地部署的 **UI-TARS**（坐标直出模型）输出其原生 `Thought / Action` 协议，再容错翻译为引擎 DSL（`tap_coord` / `swipe` / `type_text` / `back` / `copy_link` / `stop_explore`）。搭配 `DEVICE=mac` 时**无需 OCR 依赖**即可端到端跑通「看屏 → 规划 → 点击」。


## 架构核心

标注模式：**自行标注每个步骤的操作位置、执行动作**，动作支持仿人工操作的点击、滑动、截图等，也支持llm 识别、视觉定位点击等 

探索模式：**通过自然语言让Agent自己探索生成 RPA，确定性重放复用。** 把"看屏 → 决策 → 点击"的 GUI 操作声明成一份 JSON **操作链路**，视觉定位用本地多模态模型（Ollama / Qwen-VL / UI-TARS）驱动探索；探索完成后自动**合成确定性链路**，之后交给解释器**零模型调用免费重放**。

## 合规声明

⚠️ **本项目仅供学习与研究目的。** 自动化操作第三方应用（尤其是带有反自动化机制的平台）可能违反其服务条款或相关法律法规。使用者须自行评估并承担全部合规责任；本项目不对任何滥用导致的账号封禁、法律责任或数据损失负责。请勿将本项目用于任何未获授权的访问或商业爬取。

## 贡献与安全

- 想参与开发？请看 [CONTRIBUTING.md](./CONTRIBUTING.md)（开发环境、测试、PR 规范）。
- 发现安全漏洞？请按 [SECURITY.md](./SECURITY.md) **私下**上报，不要公开 Issue。

## License

[MIT](./LICENSE) © 2026 The OpChain Authors
