# AI 跑团：单人本地后端与双窗网页

由大模型担任主持人的单人跑团。Python 3.10–3.13，asyncio + aiohttp + SQLite，单进程本机运行；需要一个兼容 OpenAI Chat Completions 的模型服务，运行时不会自动退回 mock。

模块职责、数据结构与回合流程见 [docs/implementation-handbook.md](docs/implementation-handbook.md)。

## 安装与启动

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # 填写 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
.venv/bin/python run.py
```

浏览器打开 http://127.0.0.1:8000 。在「创建世界」选择预设、已保存的世界设定或创建新世界。左侧对话整理想法，右侧条目可直接编辑；点击下方「创建世界」独立保存设定，再进入角色卡创建。已保存设定可复用为新草稿，修改不会覆盖来源。完成角色卡后进入冒险；「世界档案」用于继续已有冒险。

## 配置

`run.py` 启动时自动读取项目根目录的 `.env`（可用 `--config` 指定其他文件），已设置的环境变量优先。全部变量见 `.env.example`：

- `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`：必填，缺少时启动报错。
- `DB_PATH`：世界存档数据库，默认在项目的 `data/engine.sqlite3`。相对路径固定在项目的 `data/` 下，例如 `engine.sqlite3` 和 `data/engine.sqlite3` 指向同一个文件；也可显式指定绝对路径。SQLite 的 `-wal`、`-shm` 文件和 `.bak-*` 迁移备份会随数据库存放，后端启动时会打印实际的存档路径。
- `LOG_DIR`：模型调用日志，默认 `logs/`。
- `LLM_PROVIDER=auto`：仅识别官方 `api.deepseek.com` 并发送 DeepSeek 专用参数；使用 DeepSeek 代理时设为 `deepseek`，其他服务设为 `openai_compatible`。
- `LLM_JSON_OUTPUT=true`：发送 `response_format={"type":"json_object"}`；服务不支持时设为 `false`，仍在本地解析 JSON。
- `LLM_THINKING=disabled`：DeepSeek 默认关闭思考；设为 `enabled` 时使用 `LLM_REASONING_EFFORT`（`low/high/max`）。
- `LLM_TIMEOUT`、`LLM_CONCURRENCY`、`NARRATION_MAX_CHARS`、`PROMPT_VERSION`、`HOST`、`PORT`。

网络故障、429 和部分 5xx 最多尝试三次；空白或非法 JSON 最多额外重试一次。其他 4xx、输出截断和内容过滤不重试。每次尝试都写入 `logs/` 下的 JSONL，密钥会被脱敏。

## 玩法

- **交互窗口**：讨论规则、描述行动或提出新设定，意图由模型识别；不确定时主持人会先澄清。咨询类回答可附带建议行动，点「执行这个行动」才会真正执行。遇到新的风险或选择时，主持人给出 2—4 个建议，可以点击，也可以自由输入。
- **主叙事窗口**：只读，只显示已提交的玩家行动转述和世界的直接回应。
- **共创**：提出的新设定先经过审核，冲突时给出修改建议，玩家确认后才写入世界。
- 断线或刷新后，同一 `request_id` 的请求可以查询或重试；已掷的骰点不会重掷。

## 规则

- `four-dice-v1`（预设世界）：四枚 `{-1,0,+1}` 骰之和 + 领域加值 + 情境修正（每项 ±1），对比 DC：简单 0 / 普通 2 / 困难 4 / 极难 6，平手成功。
- `fate-lite-v1`（共创世界）：在四骰判定基础上使用简化 Fate 角色——两个形象（高概念、自由形象）、固定 19 项技能（初始一个 +2、两个 +1）、一条压力轨（1 / 2 / 3）、轻微 / 中度 / 严重伤痕（2 / 4 / 6）；无初始绝技，退让可获得命运点。形象修正必须引用真实形象并说明原因。

战斗的攻击与防守分别掷骰，正的攻防差值即伤害。一次受击最多用一格压力，可以叠加多道伤痕。受击前可退让并描述退出方式，获得 1 点命运点，再加本场新受的伤痕数。没有吸收掉全部伤害会出局。场景结束且能休息时，清除压力和轻微伤痕。

讨论不推进游戏时间、不掷骰；游戏时间只随行动推进。

## HTTP 接口（`/api/v1`）

- `GET/POST /worlds`，`POST /worlds/{id}/start`
- `POST /worlds/{id}/messages`
- `POST /worlds/{id}/proposals/{pid}/confirm|reject`
- `GET /worlds/{id}/requests/{rid}`，`POST …/retry|cancel`
- `GET /worlds/{id}/messages`、`/view`、`/events`（SSE，支持 `Last-Event-ID` 续接）
- `GET/POST /workshop`，`GET /workshop/{id}`，`POST /workshop/{id}/messages|confirm`

## 测试与脚本

```sh
.venv/bin/pytest -q                  # 离线测试，不调用模型
node tests/interaction_ui.mjs        # 前端交互契约

.venv/bin/python scripts/test_api.py                 # 真实模型连通检查
.venv/bin/python scripts/test_workshop.py            # 真实模型创建向导，使用临时存档
.venv/bin/python scripts/run_scenario.py --scenario scenarios/sky_port.json
.venv/bin/python scripts/inspect_agent.py --limit 5    # 按回合检查模型、工具、事件和分窗
.venv/bin/python scripts/report_trace.py logs/<file>.jsonl
.venv/bin/python scripts/export_trace.py             # 最新日志导出为 output/traces/*.md
.venv/bin/python scripts/show_world.py --events      # 只读查看存档
```

场景运行产物写入 `test-runs/`。

## 本地记忆查询

运行 `.venv/bin/python scripts/install_memory_model.py` 安装中文嵌入模型（约 24 MB），然后重启服务。主持人的 `lookup_public` 会结合关键词与 Chroma 的语义查询，首次检索按需索引当前存档，后续复用已有向量。向量索引保存在 SQLite 同目录的 `<数据库名>.vectors/`，可由存档重建。

索引来源是当前世界中玩家可见的实体、有效事实、公开世界书和已保存的叙事历史。SQLite 负责正式状态与可见性；向量查询返回的是相关记录。缺少模型文件或检索失败时会使用关键词。可用 `MEMORY_VECTOR=false` 关闭，或用 `MEMORY_MODEL_DIR` 指定包含同一模型文件的目录。

## 检查 Agent 的运作过程

在项目目录运行 `.venv/bin/python scripts/inspect_agent.py` 查看最新一个冒险请求。报告用回合 ID 串起 API 调用、工具回传、CrewAI 路由、判定、提交和实际窗口消息。旧日志若缺少路由事件，仍可查看已记录的模型输出和存档事件。

- `--limit 5` 查看最近五条，`--world 悬云海` 按世界筛选。
- `--request 请求ID前缀` 定位一轮，`--details` 展开模型请求与解析结果。
- 默认报告缩略显示内容。详细模式可能包含主持人专用上下文，适合在本机检查。

## 当前限制

- 冒险使用 CrewAI Flow 编排。交互 Flow 分流咨询、澄清、共创和行动；行动 Flow 串接计划、玩家选择、判定、审核、叙事和提交。规则与重试继续读取 SQLite 里的回合记录；主持人消息和正式叙事仍走各自的消息通道。主持人用原生 Tool Calls 选定输出路径，也可先查公开设定和已提交的叙事历史。模型网关需要支持 Chat Completions 的工具调用。判定和正式叙事仍由行动 Flow 执行。
- 后台 NPC 调度尚未实现；回合摘要会生成并保存，但还没有用于上下文检索。
- 只支持从源码目录运行（`pip install -e .`），提示词、迁移脚本与 `config/` 按源码路径读取。
