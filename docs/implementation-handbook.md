# AI 跑团工程实现手册

本文描述当前代码的结构、模块职责与约束。适用范围：单用户、本机、多存档、Python 后端和浏览器前端。技术栈为 Python asyncio、aiohttp、SQLite 和原生 JavaScript。运行时不自动退回 mock。安装、环境变量与最近的调整记录见 [README](../README.md)。

## 1. 产品行为与不可变约束

主页面有世界选择、只读主叙事窗口、可输入的交互窗口。交互窗口接收行动、规则讨论、风险询问和共创提案。主叙事显示 AI 对玩家已授权行动的转述及其直接后果，不显示原始输入、审核意见、技术错误或未确认提案。

交互窗口不区分输入模式，由意图识别分流；API 的 mode_hint 只是提示。明确的行动句可以被识别为行动；假设问题和咨询不能自动执行。不确定时仅在交互窗口澄清。咨询回复可附建议行动，玩家点击“执行这个行动”才显式提交。无需给每句明确行动增加确认。

AI 转述不得补充移动、心理、对话或目标。一次行动在新选择、意外、失败阻拦或完成时结束，不自动执行新决定。探索中 AI 拥有场景描述权，但必须服从玩家行动范围、已有事实和规则。

共创发生冲突时提供理由及最小修改建议。提案由玩家确认后才进入回合；确认绑定提案版本和世界版本，不用模糊的“好的”猜测接受了什么。讨论本身不推进游戏时间、不掷骰、不扣资源。

世界按内部因果存在，不按玩家能力缩放。未知、隐藏、尚未确定分别建模。各模块只能提出建议，唯一提交服务写正式状态。

## 2. 调用预算与设计取舍

模块是程序职责边界，不意味着每个模块都必须请求模型。程序先判定、后叙事；不使用关键词触发固定骰型；姓名不充当实体主键；未知字段与非法操作明确报错或记录丢弃原因，不静默应用。

典型路径：单纯讨论一次意图识别加一次咨询；熟悉场景的行动为意图、规划、叙事三次；需要补充世界事实时增加一次生成；涉及秘密或多实体修改时增加一次语义审核。不能为省调用让模型改判骰点。

## 3. 目录

包目录均有空 `__init__.py`，不承载业务。日志、数据库、缓存和场景运行产物不提交 Git。

```text
run.py                         进程入口
config/rules/four_dice.json    四骰规则参数
config/presets/sky_islands.json 默认预设世界
app/config.py                  读取 .env 与校验配置
app/bootstrap.py               组装与关闭运行时
app/api/                       server.py、routes.py、schemas.py、streams.py、notify.py
app/domain/                    types.py、changes.py、errors.py
app/application/               chat.py、turns.py、commit.py、context.py、entities.py、creation.py、workshop.py、recovery.py
app/modules/                   intent.py、planning.py、generation.py、npc.py、visibility.py、review.py、narration.py、memory.py
app/rules/                     four_dice.py、character.py
app/storage/                   database.py、repositories.py、dbutil.py、migrations/001_initial.sql、002_indexes.sql、003_workshop.sql
app/providers/                 base.py、chat_completions.py、trace.py
app/jobs/                      worker.py、world_time.py
app/prompts/                   loader.py 与 audit、cocreation_review、discussion、generation、intent、memory、narration、planning、workshop 九个提示词
web/                           index.html、styles.css、main.js、api.js、state.js、components/
scenarios/                     真实模型场景脚本输入
scripts/                       真实接口检查、场景运行、日志导出与存档查看
tests/                         离线回归测试
```

## 4. 共享类型

所有对象使用 UUID 字符串；时间为 UTC，游戏时间为非负整数分钟。`app/domain/types.py` 用 dataclass 定义以下类型：

- `World`: id, name, setting, phase(prepared/playing), version, clock, ruleset_id, ruleset_config, player_id。共创世界的世界书保存在 `ruleset_config.world_book`。
- `Entity`: id, world_id, kind(player/npc/place/item/faction/clue/quest), name, attributes, revision, source_event_id?。
- `Fact`: id, world_id, subject_id, predicate, value, status(active/superseded), source_event_id?, supersedes_id?, disclosure_rule?。事实应原子化，不把箱子外观与保管费放一条。
- `Knowledge`: id, world_id, observer_id, basis(observed/reported/inferred), source_event_id, learned_at, fact_id?, belief_text?。错误信念只写 belief_text，不改世界事实。
- `Relation`: id, source_entity_id, target_entity_id, kind, description, evidence_event_ids。
- `NpcCard`: entity_id, identity, traits, goals, boundaries, current_activity, location_id, condition。认知由 Knowledge 查询，关系另存。
- `Intent`: kind(discussion/explore/cocreate/clarify), target_ids, creation_statements, needs_clarification, requested_tools, normalized_action?, question?, unresolved。unresolved 只保留需要补充的世界事实缺口。
- `Proposal`: id, revision, world_id, base_version, original_text, proposed_text, fact_candidates, issues, status(pending/accepted/rejected/expired)。
- `CandidateBatch`: temp_entities, facts, npc_intents, challenges, changes, unresolved_refs。仅一次生成批次中的 `new:*` 可作为临时引用。
- `Issue`: severity(blocking/warning), path, reason, suggested_fix, reference_id?。
- `Turn`: id, request_id, payload_hash, world_id, base_version, stage, checkpoint, result?, error_code?, created_at。
- `ScheduledEvent`: id, world_id, due_at, prerequisites, actor_ids, intent, status, source_event_id?。
- `Summary`: id, world_id, event_ids, entity_ids, audience_id, text, covered_until_sequence。实体 ID 和事实 ID 不得混用。
- `DisclosureGrant`: fact_id, observer_id, turn_id, evidence_ids, check_id?。由程序签发，模型不能自报获得授权。
- `PublicOutcome`: authorized_action, observable_events, visible_entities, visible_facts, stop_reason。它是叙事模型唯一的游戏事实输入。
- `TurnDraft`: base_version, registered_candidates, resolution_ids, changes, events, public_outcome, narration, next_jobs。
- `Snapshot`: world 及 entities、facts、knowledge、relations、npc_cards、scheduled 的按 ID 字典。

以下结构以字典传递或存储，由对应模块校验：

- 挑战（planning 输出）：mode(automatic/check/blocked), goal, domain?, difficulty?, conditions, prerequisites, success_effects, failure_effects, duration_minutes。条件含 source_id、direction(advantage/disadvantage)；同一 basis_id 不重复计入修正。
- 判定结果（`FourDice.resolve`）：check_id, input_hash, ruleset_version, mode, dice, skill_bonus, modifiers, total?, dc?, margin?, success, selected_effects。
- 消息：id, world_id, channel(interaction/narrative), author(player/gm), text, sequence, request_id?, turn_id?。
- 事件：id, turn_id, game_time, kind, actor_ids, changes, evidence_ids, player_visible_summary。

状态操作使用带 `op` 字段的字典，合法值见 `CHANGE_OPS`：create_entity、set_attribute(expected/expected_revision)、resource_delta(expected_value)、transfer_item(expected_owner)、append_fact、supersede_fact、learn、relate、schedule、finish_schedule、update_npc_card(expected/patch/reason)，以及 fate-lite 角色专用的 mark_stress、clear_stress、take_consequence。发现通过 DisclosureGrant 转换为 learn。每个操作具有唯一 op_id。

## 5. 后端模块与接口

所有模型模块接收只读上下文，不持有数据库写连接；provider 为 None 或传入 fixture 时走离线路径，仅供测试使用。

### 5.1 启动与配置

`run.py`：解析 --host/--port/--config，构建运行时并启动 HTTP 服务，不写业务。

`app/config.py`：`load_settings(env_path) -> Settings` 读取 .env，已有环境变量优先；`validate_settings(settings, require_model=True)` 校验模型配置、端口、并发、叙事长度和请求参数。缺少密钥明确报错。

`app/bootstrap.py`：`build_runtime(settings, require_model, fixtures) -> Runtime` 连接数据库、执行迁移、创建 provider、ChatService 与 Worker；`shutdown(runtime)` 停止 worker、关闭 provider 和数据库。仅一个服务进程，内部按世界加锁。

### 5.2 API

`api/server.py`：`create_app(runtime)` 注册路由、静态资源、错误中间件和关闭钩子；SSE 端点 `/events` 在此实现。

`api/schemas.py`：`decode_request(route, body)` 拒绝未知字段并检查必填字段；`encode_response(value)`。

`api/routes.py`：世界、消息、提案、请求状态、视图与共创工作坊的处理函数。只做解析和服务调用。

`api/streams.py`：`StreamHub.subscribe(world_id, after_sequence)` 按序号读取已持久化的 stream_events，15 秒无事件发心跳；`notify(world_id)` 唤醒订阅者。`api/notify.py` 的 `notify_world` 供服务层在写入事件后调用。

### 5.3 应用层

`application/chat.py`：`ChatService.submit(world_id, request_id, text, mode_hint, reply_to?) -> Receipt`。在短事务中预留 request_id 与 payload_hash、保存交互消息，然后后台处理：识别意图；讨论走 `discuss`；共创进入 review 并保存提案；不明确时返回一个澄清问题；行动进入 TurnService。`reply_to` 表示玩家点击了某条建议，服务端验证来源、版本和是否已被选择。讨论只使用公开上下文。

`application/turns.py`：`TurnService.execute(command)` 持世界锁运行第 6 节流程；`resume(turn_id, world_id)` 从检查点重建命令；`choose_tools(intent, snapshot)` 为确定性白名单路由，只有存在世界事实缺口时才调用 generation；`first_stop(plan, upcoming_events)` 限制一次一个关键判定，并在到期事件处截断行动时长。

`application/entities.py`：`relevant_ids` 按 ID、姓名和别名解析目标，同名歧义时报错；`context_ids` 在相关实体之后补充所在地点与同地对象；`reconcile` 把与已有实体同名的候选映射到既有 ID 并改写引用；`duplicates` 检查旧计划中的重复实体。

`application/context.py`：`build_intent_context`、`build_world_context(snapshot, ids, budget)`、`build_public_context(snapshot, grants, events, authorized_action, stop_reason) -> PublicOutcome`、`public_book`。每种上下文显式白名单，不把完整世界交给所有模块。

`application/commit.py`：`validate(draft, snapshot)`；`commit_turn(db, world_id, turn_id, request_id, draft, grants, elapsed_minutes, runtime) -> CommittedTurn`。在调用方事务中写状态、事件、主叙事消息、检定说明、后续选项、outbox、stream 事件及世界版本；版本不符抛出冲突。不在模型等待期间保持事务。

`application/creation.py`：`prepare(runtime, preset_id, overrides) -> World` 从预设创建世界，无模型调用；`start(runtime, world_id, request_id)` 以预设开场意图提交一个普通行动，重复开始返回原回合。

`application/workshop.py`：共创世界与角色的草稿流程。`create`、`get`、`message`（调用 workshop 提示词更新世界书或角色卡）、`confirm`（accept_change/reject_change/confirm_world/confirm_character）。已确认条目被修改或模型报告冲突时进入 pending，需显式确认。确认角色后 `launch` 在同一事务中创建世界、地点、势力、角色与装备。

`application/recovery.py`：`retry(runtime, world_id, request_id)` 只恢复未完成阶段，复用已冻结的计划与骰点；`cancel` 仅在提交前生效。已完成请求原样返回。

### 5.4 模型模块

`modules/intent.py`：`recognize(provider, settings, context, text, mode_hint) -> Intent`。识别意图、规范化行动和目标；缺口按 world_fact/player_choice/npc_response/rule_check 分类，只有 world_fact 驱动生成。禁止写剧情或提前确定未知事实。

`modules/planning.py`：`propose(...)` 由主持人模型规划挑战、成败后果、公开风险与暂停时机（choice_timing: none/before_action/after_action），格式或状态操作不合法时附带原因修复一次。`validate_plan`、`validate_effects`、`validate_options`（2—4 个建议）、`check_notice`。没有挑战时不默认成功。

`modules/generation.py`：`propose(provider, settings, request) -> CandidateBatch` 是新实体候选的唯一入口；`normalize_batch` 把可识别的模型变体规整为引擎结构，并记录丢弃项。

`modules/npc.py`：`build_card`；`related_cards(snapshot, target_ids, text)` 本地读取目标、提及或同地 NPC 的角色卡、认知与关系，不额外请求模型；`validate_update` 校验 update_npc_card 的 expected/patch/reason。普通 NPC 反应由 planning 统一规划。

`modules/visibility.py`：`evaluate_disclosure(rule, action, scene, resolution)` 按地点、行动词、前置事实和检定结果判断；`issue_grants`；`filter_for(observer_id, facts, knowledge)`。没有证据就不签发；旧事实的规则不能临时改写来满足当前行动。

`modules/review.py`：`review_creation` 生成待确认提案；`validate_result` 为程序结构检查；`needs_semantic_audit` 由程序决定是否调用 `semantic_audit`。语义审核只检查世界因果、人物行为和信息披露，不检查字段和 ID。

`modules/narration.py`：`compose(provider, settings, public_outcome, style, previous_text?)` 输出 player_action_text（≤50 字，只转述已声明动作）和 gm_text（≤NARRATION_MAX_CHARS）。超限时附问题修复一次，仍超限才按上限截断。

`modules/memory.py`：`summarize` 由 worker 在每回合提交后生成玩家视角摘要；`validate_summary` 拒绝把事实 ID 当作实体 ID。摘要目前只写入，尚未用于上下文检索。

### 5.5 规则与状态

`domain/errors.py`：AppError(code, http_status, retryable) 及 ValidationError、Conflict、NotFound、ModelUnavailable、StaleProposal、StaleTurn、ModelRequestError，不暴露认证信息。

`domain/changes.py`：`register(batch, snapshot)` 为临时 ID 分配 UUID 并深度替换引用；`stabilize_changes` 按回合与 op_id 生成稳定 ID，重试保持一致；`validate_changes`；`apply_to_copy(snapshot, changes)` 在副本上应用并检查前置条件；`hash_inputs` 用于检定输入比对。

`rules/four_dice.py`：`FourDice.assess(challenge, actor)` 与 `resolve(assessment, random_record)`。4 个 {-1,0,+1} 等概率骰，总和 + 领域加值 + 情境修正，DC 简单0/普通2/困难4/极难6，每项条件 ±1，平手成功。automatic 无骰且成功；blocked 失败且不掷骰。fate-lite 规则下形象修正必须引用真实形象 ID 并给出原因。

`rules/character.py`：fate-lite-v1 规则常量与 `validate_character`。固定 19 项技能；初始一个 +2、两个 +1；两个形象；按体魄/意志生成三或四格压力轨；后果槽轻微2、中度4、严重6。

### 5.6 存储与后台任务

`storage/database.py`：`connect`、`migrate`、`transaction`；开启外键、busy_timeout 与 WAL。迁移按版本记录，执行前备份已有库为 `.bak-<版本>`。

`storage/repositories.py`：WorldRepo、EntityRepo、FactRepo、KnowledgeRepo、RelationRepo、NpcCardRepo、ScheduledRepo、TurnRepo、ProposalRepo、MessageRepo、EventRepo、CheckRepo、JobRepo、StreamRepo、SummaryRepo。只有提交服务写领域状态；交互消息和工作检查点允许单独写。

`jobs/worker.py`：轮询 outbox 执行摘要任务；每个 job 有唯一 dedup_key，失败指数退避，最多五次。

`jobs/world_time.py`：`due_events(snapshot, target_time)`。游戏时间只由行动推进，现实空闲不推进。后台 NPC 调度尚未实现。

### 5.7 模型、提示词和日志

`providers/base.py`：`ModelRequest`（module、prompt_version、schema_version、world_id、turn_id、call_id、messages、output_budget、output_mode、thinking、reasoning_effort）与 `ModelResponse`。

`providers/chat_completions.py`：OpenAI 兼容 Chat Completions 适配。默认 `stream: true`，把分片拼回完整消息后再解析；`LLM_STREAM=false` 时一次取回。JSON 模式下发送 response_format；DeepSeek 显式设置思考模式。网络故障、429 和部分 5xx 最多尝试三次，空白或非法 JSON 最多额外尝试一次，共享三次上限；截断、内容过滤和其他 4xx 不重试。全局并发由信号量限制。玩家可见的咨询回复、叙事正文和创建向导回复会在生成过程中推到页面对应位置，失败时撤下未提交草稿。

`providers/trace.py`：`record_request`、`record_response` 以 JSONL 按 call_id/attempt 记录请求、原始响应、解析结果、错误类别和耗时。密钥脱敏，文件权限 0600，日志不进入玩家接口。

`prompts/loader.py`：`load(module, version, prompts_dir)` 读取同名 md 文件。各提示词对应同名模块职责。

## 6. 回合流程与状态机

请求状态：received → interpreting → discussing / awaiting_confirmation 或 preparing → resolving → narrating → validating → committing → completed。任意未提交阶段可 failed_retryable；版本失效为 stale；用户取消为 cancelled。completed 不可再执行。

1. 接收消息，在短事务中预留 request_id 与 payload_hash，保存交互消息。相同 ID 相同正文返回现有任务；不同正文 409。
2. 读取快照与最近双通道历史，调用意图识别。
3. discussion 只生成公开咨询回复，世界版本和游戏时间不变。
4. cocreate 执行审核并保存提案；确认请求带 proposal_id/revision/base_version，过期时 409。确认后的设定作为行动提交。
5. explore 持世界锁；解析目标实体，只有世界事实缺口才调用 generation；候选经 reconcile 复用已有身份，再 register 分配 ID。
6. 读取相关 NPC 卡，调用 planning。计划与已注册候选写入检查点后才掷骰，重试沿用。
7. choice_timing=before_action 时以交互答复暂停，不掷骰、不推进时钟、不提交候选；下一次明确行动或点击建议继续使用候选。
8. 检定前在交互窗口公布领域、难度与可预见风险。程序按 check_id 持久化骰点；输入哈希变化时拒绝复用。
9. 选取对应成败后果，签发 DisclosureGrant，生成稳定 ID，在副本上预演变化。程序验证；需要时调用语义审核。
10. 只用可见事实组装 PublicOutcome，调用叙事。after_action 时附加后续选项。
11. commit_turn 在事务中提交并发布 turn.committed。之后 outbox 异步生成摘要。

主叙事的正式记录只保留已提交内容。生成过程中可以暂时显示叙事草稿，失败或改写时撤下，提交后由正式消息替换。

## 7. 数据库与事务

001_initial.sql 建立 worlds、entities、facts、knowledge、relations、npc_cards、turns、checks、proposals、messages、events、summaries、summary_entities、scheduled_events、outbox、stream_events；002_indexes.sql 建立检索索引；003_workshop.sql 建立创建草稿表。

每张领域表带 world_id，复合主键 (world_id, id)，禁止跨世界引用。turns 唯一 (world_id, request_id)，checks 唯一 (world_id, check_id)，messages 唯一 (world_id, sequence)，outbox 唯一 dedup_key。

JSON 字段以文本存储；被检索的 ID、状态、时间使用独立列。骰点在独立 checks 记录中提前持久化，不提前改变世界；取消不能用来重抽已公开的结果。

## 8. HTTP 接口与事件协议

所有接口前缀 `/api/v1`。本机单用户无账户；角色来自 world.player_id。

- GET /worlds → 世界摘要数组。
- POST /worlds {preset_id, overrides?, request_id?} → 201 世界摘要。
- POST /worlds/{id}/start {request_id} → 202 Receipt。
- POST /worlds/{id}/messages {request_id, text, mode_hint?, reply_to?} → 202 Receipt。
- POST /worlds/{id}/proposals/{pid}/confirm {request_id, revision, base_version} → 202 Receipt。
- POST /worlds/{id}/proposals/{pid}/reject {request_id, revision} → 200 提案状态。
- GET /worlds/{id}/requests/{rid} → {request_id, status, stage, result, error, retryable}。
- POST /worlds/{id}/requests/{rid}/retry → 202；POST …/cancel → 200，仅在提交前生效。
- GET /worlds/{id}/messages?channel=…&after=… → 消息列表，带选项的答复附 options/choice_request_id/choices_active。
- GET /worlds/{id}/view → 玩家可见设定、角色、资源、物品、已知事实；不返回检查点。
- GET /worlds/{id}/events?after=… → SSE，支持 Last-Event-ID 续接。
- GET/POST /workshop、GET /workshop/{id}、POST /workshop/{id}/messages、POST /workshop/{id}/confirm → 共创草稿。

Receipt={request_id, status_url, event_url}。SSE 事件为 {id, type, world_id, request_id?, payload}，类型包括 progress、check.announced、interaction.completed、proposal.ready、turn.committed、request.failed。请求由后台任务处理，断开 HTTP 连接不取消任务。

错误格式 {code, message, path?, retryable}。400 输入或校验问题，404 不存在，409 版本/幂等冲突，502 模型供应商失败，503 其他服务异常。

## 9. 前端

`index.html` 定义世界档案页与游戏页；主叙事没有输入框。`styles.css` 负责布局与消息样式。

`main.js`：`boot` 加载世界列表；`selectWorld` 重置订阅并加载双通道历史；`onSend`、`onChoose` 发送行动或选择的建议；`onEvent` 处理 SSE。

`api.js`：`request`、`sendInteraction`、`confirmProposal`、`rejectProposal`、`subscribe`。网络失败保持原 request_id，不自动创建新 ID 重发行动。

`state.js`：`reduce`、`getPendingRequest`、`savePendingRequest`（localStorage 保存待恢复 ID）、`rememberMessage`（按 message_id 去重）。

`components/`：world-picker、narrative（只接受已提交消息，使用 textContent）、interaction（消息、建议按钮、忙碌状态、错误重试）、proposal（提案确认）、workshop（世界与角色创建向导）。

## 10. 样例走通

预设：浮空群岛、人类稀少、飞艇探索贸易；小剑士剑术+2、自然知识+1；抵达贸易据点空港。

交互输入“我看看有哪些飞艇”：识别 explore，普通观察 automatic；生成可见飞艇细节；主叙事玩家条目只转述“我观察停泊的飞艇”，不追加未声明的动作；GM 条目用两三句描述可见结果。

交互输入“我能用自然知识判断风向吗”：discussion，说明适用能力与公开环境依据，不掷骰。用户点击“执行这个行动”后才执行。

交互输入“我想这里有一位认识我父亲的船长”：cocreate，审核与既有关系是否冲突。若改成“曾听过父亲名字”，展示建议并等待确认；不能同时强行让玩家走去交谈。

箱子外观、昨夜到港、保管费拆成三条事实。看得见箱子不代表知道费用；费用只有读到凭据或得到有来源的证言后签发授权。

## 11. 测试与工具脚本

`tests/conftest.py` 提供临时数据库、固定世界和离线运行时。离线测试覆盖：意图分流（test_intents）、四骰 81 组合与修正（test_rules）、发现与改名泄密（test_visibility）、状态操作前置条件（test_changes）、幂等与版本冲突（test_commit）、行动规划、暂停、重试与选项（test_action_flow、test_identity_and_pauses）、生成缺口与模型输出规整（test_generation_flow）、审核职责与 ID 稳定（test_audit_identity）、摘要引用（test_memory）、请求层策略（test_provider）、创建向导与 fate-lite 规则（test_workshop）、HTTP（test_api）、开场与观察（test_scenarios）。`tests/interaction_ui.mjs` 用 Node 检查前端选项与请求关联。

`scripts/test_api.py` 做一次独立的真实 JSON 连通检查；`run_scenario.py` 读取 scenarios，在 test-runs 下创建独立存档并记录结果；`test_workshop.py` 用虚构输入测试真实创建向导；`report_trace.py` 按 call_id 聚合耗时与 usage；`export_trace.py` 把日志导出为 Markdown；`show_world.py` 只读查看存档内容。

## 12. 已定与可调参数

已定：单人本机、双窗口、短回合、独立世界、四骰、难度 0/2/4/6、每项条件 ±1、平手成功、共创冲突需确认、统一提交。

可调：叙事长度、模型并发与超时、思考模式、各模块输出预算。实现时不得悄悄开启现实时间推进、完整 FATE 命运点系统或多人服务。
