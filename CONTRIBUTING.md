# 贡献指南（Contributing）

感谢你考虑为 **OpChain** 做贡献！下面是一些上手约定，能让你的一次 PR 更顺畅地合入。

## 开发环境

OpChain 后端纯 Python 标准库，**Mock 模式零依赖**，开箱即跑：

```bash
git clone https://github.com/Signal-M/OpChain.git
cd OpChain
python app.py                 # Python 3.8+ 即可，默认 Mock 模式
# 浏览器打开 http://127.0.0.1:8000
```

真机设备 / 真实 OCR 定位按需安装依赖（见 `requirements.txt`）：

```bash
pip install -r requirements.txt   # paddleocr / opencv-python / pyperclip / pillow 等
```

## 运行测试

Mock 模式冒烟测试覆盖「探索 → 合成确定性链路 → 重放」全链路，零外部依赖：

```bash
python _smoke_test.py
```

语法检查（无需任何第三方包）：

```bash
python -m py_compile app.py engine/*.py _smoke_test.py
```

## 分支与提交

- 从 `main` 切出特性分支：`git checkout -b feat/your-feature`。
- 提交信息建议清晰、动词开头（如 `fix: ...` / `feat: ...` / `docs: ...`）。
- 保持每个 commit 聚焦单一改动。

## Pull Request 规范

- 一个 PR 解决一个问题 / 一个功能。
- 提交前请确认：
  - 跑通 `_smoke_test.py`；
  - 没有误提交 `runs/`、`data/`、`captures/`（这些已在 `.gitignore` 中排除）；
  - 没有硬编码密钥或绝对路径。
- 在 PR 模板中说明改动、关联 Issue 与测试情况。

## 代码风格

- 后端遵循 PEP 8，模块内部保持现有风格（函数式 + 清晰命名）。
- 适配器 / 大脑 / 视觉均为可插拔接口，新增设备请继承 `engine/devices.py` 的对应基类，并在 README 的「接入真机」章节补充环境变量说明。

## 行为准则

请友善、尊重地交流。安全相关问题请按 [SECURITY.md](./SECURITY.md) 私下上报，不要公开 Issue。
