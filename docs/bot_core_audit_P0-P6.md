# bot_core.py P0-P6 工程审计报告（基于源码事实）

范围与约束：

- 仅基于仓库内 `bot_core.py` 的静态实现进行拆解与测试设计；未出现的模块一律标注“未提供该模块代码”。
- 以你提供的阶段定义 P0-P6 为准，逐段对齐。
- “测试协议”以“可在本仓库离线运行”为优先：对 WS/Steam/LLM/Web/Heybox/B 站请求均使用 fake/monkeypatch。

## 总览

- 运行入口：`asyncio.run(main())` -> 启动 3 个后台 loop -> `await connect_loop(bot)` 常驻。
- 核心状态：
  - WS：`OneBotClient.ws` + `ws_lock`（发送串行化）+ `connected`（就绪事件）。
  - DB：单进程单连接 `sqlite3.connect(check_same_thread=False)` + `asyncio.Lock()` 串行化。
  - 工具/RAG：`search_verified`（KB-first -> web fetch -> 入库） + `_ensure_sources()` 兜底来源行。

## P0 (Core Infrastructure)

### 1) 技术实现细节

数据流与控制流：

- `main()`
  - 创建 `DB(DB_PATH)`、`SteamClient(STEAM_API_KEY)`、`OneBotClient(db, steam)`
  - `asyncio.create_task(steam_audit_loop(bot))`
  - `asyncio.create_task(proactive_user_loop(bot))`
  - `asyncio.create_task(proactive_group_loop(bot))`
  - `await connect_loop(bot)`：阻塞主协程，连接维护与消息接收在此进行。

- `connect_loop(bot)`
  - 从 `OB11_ACCESS_TOKEN` 构造 `Authorization: Bearer ...`。
  - 兼容 `websockets.connect` 参数重命名：
    - 若签名含 `additional_headers` 使用 `kw["additional_headers"] = headers`
    - 否则使用 `kw["extra_headers"] = headers`
  - 连接参数：`open_timeout=10`、`ping_interval=20`、`ping_timeout=20`。
  - 成功连接：
    - `bot.ws = ws`，`bot.connected.set()`。
    - `async for raw in ws`：每条消息 JSON 反序列化成 event。
    - `asyncio.create_task(bot.handle_event(event))` fire-and-forget，保持 recv loop 热。
  - 异常/断开：`finally` 中执行 `bot.connected.clear()`、`bot.ws = None`。
  - 重连：指数退避 `backoff`，sleep 上限 30 秒。

关键接口与状态：

- 环境变量：`WS_URL`、`OB11_ACCESS_TOKEN`。
- 状态：`OneBotClient.connected (asyncio.Event)`、`OneBotClient.ws`、`OneBotClient.ws_lock`。

### 2) 功能完备性与风险审查

- 异常捕获与降级：
  - `connect_loop` 对连接异常与坏消息 `log.exception`，不会终止主循环。
  - 单条 event JSON 解析失败会记录 `bad event`。

- 并发与资源安全：
  - `asyncio.create_task(bot.handle_event(...))` 无并发上限/背压；高吞吐时可能出现 Task 堆积与内存压力。
  - 后台任务未保存引用；虽各 loop 内部多处 try/except，但仍存在“Task exception was never retrieved”的理论窗口（例如新增路径抛异常且未捕获）。

### 3) 测试验证方案

目标：离线验证 header 参数选择与 `connected` 生命周期（set/clear）。

前置条件：无。

触发代码：运行 `pytest -q`（见 `tests/test_p0_connect_loop.py`）。

预期断言：

- 当 fake `websockets.connect` 暴露 `additional_headers` 参数时，`connect_loop` 传入该参数而非 `extra_headers`。
- 当 fake `websockets.connect` 仅暴露 `extra_headers` 参数时，`connect_loop` 走 else 分支。
- `connected` 在连接内为 set，退出连接上下文后被 clear。

## P1 (Message Dispatching)

### 1) 技术实现细节

数据流与控制流：

- `handle_event(event)` 仅处理 `post_type == "message"`。
- 文本抽取：`_extract_text(event)`
  - `raw_message: str` 优先
  - `message: str`
  - `message: list[segment]`：只拼接 `type=="text"` 的 `data.text`。

指令路由：

- `/` 开头：管理员指令（`ADMIN_QQ_LIST` env 逗号分隔）。
- 已实现：
  - `/bind <qq> <steamid64>`（正则强约束 17 位数字）-> `DB.upsert_binding`。
  - `/unbind <qq>` -> `DB.delete_binding`。
  - `/autopush <qq> on|off` -> `DB.set_push_enabled`。
  - `/group_sub <group> on|off` -> `DB.set_group_push_enabled`。
- 非命令：走 LLM path：`reply = await chat_with_tools(...)` -> `await reply(event, reply)`。

消息收发接口：

- `send_action(action, params)`：OneBot 动作调用，payload 含 `echo`。
- `reply(event, text)`：按 `message_type` 自动选择 `send_group_msg` / `send_private_msg`。

### 2) 功能完备性与风险审查

- 异常捕获与降级：管理员命令多数 try/except 并回错误；LLM 路径也包 try/except。
- 资源安全：`send_action` 未连接会抛异常；调用者多在 try/except 内，避免 loop 崩。

### 3) 测试验证方案

目标：离线验证 `_extract_text` 行为与 admin-only 规则，且 DB 产生确定性变化。

前置条件：设置 `bot_core.ADMIN_QQ_LIST = {"123"}`（测试内直接覆盖模块变量）。

触发：运行 `pytest -q`（见 `tests/test_p1_dispatch.py`）。

预期断言：

- 非管理员发送 `/bind ...`：回复包含“权限不足”，且 `user_bindings` 不新增记录。
- 管理员发送 `/bind ...`：回复包含“绑定成功”，且 `user_bindings(qq_id,steam_id,push_enabled)` 写入正确。

## P2 (LLM & Context)

### 1) 技术实现细节

LLM 客户端：

- `OneBotClient.__init__`：若 `LLM_API_KEY` 存在，创建 `AsyncOpenAI` 并注入 `DefaultAsyncHttpxClient(trust_env=True)`；可选 `LLM_BASE_URL`。

上下文与 Prompt：

- 基础 system prompt：`_system_prompt()`（日期、风格、版本意识、Steam AppID 规则、强制来源行、争议规则）。
- 私聊上下文：
  - 若已绑定 Steam：在 user content 前注入 `[System Context: 该用户的 Steam ID ...]`。
  - 若画像权重存在：注入 `[User Profile: 最近聊天关键词=...]`。
  - Heybox 风格：`_get_heybox_style_prompt()` 返回额外 system message（“仅风格，不得当作事实依据”）。

工具回合：

- `chat_with_tools` 最多 6 轮；每轮 `_llm_chat_stream(stream=True)` 聚合 delta。
- 若模型产生 tool_calls：`_dispatch_tool` 执行并以 `role=tool` 回填 JSON。

关键 guardrail：

- 若存在任何 tool 消息：
  - 工具返回 `dispute` 且最终文本未含“存在争议”：自动前置 `存在争议：...`。
  - 最终文本缺少 `[来源:`：从工具输出的 `sources` 里补齐，`_ensure_sources()` 追加来源行。

### 2) 功能完备性与风险审查

- 降级：未配置 LLM 时直接返回固定文本 `LLM 未配置...`。
- 风险：`_llm_chat_stream` 无超时/取消策略；网关卡死会挂住等待。
- 工具参数：`json.loads(arguments)` 解析失败会抛异常；外层 `handle_event` 会回“系统繁忙”。

### 3) 测试验证方案

目标：离线验证“来源兜底”和“争议注入”逻辑，无需真实 LLM。

前置条件：在测试内令 `bot.llm = object()`，并 monkeypatch `_llm_chat_stream` 与 `_dispatch_tool`。

触发：运行 `pytest -q`（见 `tests/test_p2_guardrails.py`）。

预期断言：

- 最终回复文本包含 `存在争议：...` 前缀。
- 最终回复文本末尾包含至少一条 `[来源: http...]`。

## P3 (RAG & Verification)

### 1) 技术实现细节

知识库：

- SQLite 表：`kb_items`。
- Freshness：`expires_at > now_ts` 才可被检索命中。
- 可选 FTS5：`kb_items_fts` 虚拟表 + 3 个 trigger 同步维护；若不可用，fallback 到 LIKE。

验证搜索工具：`search_verified`（由 `_dispatch_tool` 处理）

数据流：

1) KB-first

- `DB.search_kb(q, limit=3)` 命中则直接返回 `from="kb"`，sources 为 URL 列表。
- 争议检测 `_detect_dispute`：只比较 `trust>=0.8` 且 `use_case != tone` 的 evidence，抽取版本号/日期 token 判断不一致。

2) Web fallback

- `search_provider.search_structured`（默认 DDG） -> 选 top 3-6 -> `_fetch_url_text(url)` 拉取并 `_strip_html` -> upsert 入库。
- `use_case`：`_infer_use_case(url)` 将社区源（b站/小黑盒/reddit）标为 tone。

强制溯源：最终输出阶段（P2 guardrail）兜底附加 sources。

### 2) 功能完备性与风险审查

- 降级：DDG/HTTP 拉取均保证不抛异常（返回 `{ok:false,...}`），单 URL 失败跳过。
- 语义风险：web 分支若全部 fetch 失败，仍会 `ok:true` 且 evidences 为空（源码未对“空 evidence”做显式错误化）。
- SQLite：单连接 + asyncio.Lock 基本避免同进程写锁；未启 WAL，多进程共享同库会有锁争用风险。

### 3) 测试验证方案

目标：离线验证 KB-first 分支与 LIKE fallback。

前置：强制 `db.fts_enabled = False`，插入一条未过期 kb_item。

触发：`pytest -q`（见 `tests/test_p3_kb_search.py`）。

预期断言：

- `search_verified` 返回 `from == "kb"` 且 `sources` 含插入 URL。

## P4 (User Profiling)

### 1) 技术实现细节

关键词抽取：`extract_keywords(text, limit=12)`

- 清洗 URL/@/# 与非字母数字/CJK。
- CJK：保留 2-6 长度 chunk；英文：2-24。
- 频次排序并截断。

画像存储：

- `handle_event` 每条消息：
  - `DB.insert_chat_event(qq_id, ts, text, keywords)` -> `chat_events`。
  - `_update_user_profile_keywords(qq_id, kws)` -> `user_profile.keyword_weights_json`。

权重更新：

- 指数衰减：旧权重 * 0.97，过滤 <=0.05。
- 每次出现 +1。
- 上限 80 个 token，多余删除低权重。

### 2) 功能完备性与风险审查

- 降级：画像更新失败只 log，不影响命令/LLM。
- 并发：更新后 `create_task(_maybe_refresh_heybox_on_profile_shift)`，不等待（best-effort）。

### 3) 测试验证方案

目标：离线验证 chat_events 写入与权重衰减/累加结构。

触发：`pytest -q`（见 `tests/test_p4_profile.py`）。

预期断言：

- `chat_events.keywords_json` 为非空 JSON 数组。
- `user_profile.keyword_weights_json` 可解析为 dict，且包含新 token。

## P5 (Multimodal Degradation)

### 1) 技术实现细节

B站元信息：`_fetch_bilibili_view(bvid)`

- GET `https://api.bilibili.com/x/web-interface/view`，抽取 `cid/title/pic`。
- 403/412 返回 `{ok:false,error:"bilibili blocked"}`，承诺“Never throws”。

字幕抓取：`_fetch_bilibili_subtitles(bvid)`

- 先拿 view/cid；再 GET `https://api.bilibili.com/x/player/v2`，抽取 `subtitle_url`。
- 拉取字幕 JSON 后拼接 `body[].content`。
- 任意失败/无字幕/403/412：返回固定字符串 `无法获取视频字幕`（不会抛异常）。

入库入口：`OneBotClient.ingest_bilibili_video(bvid)`

- 依赖 LLM：无 `self.llm` 直接返回 `{ok:false,error:"LLM not configured"}`。
- Subtitle-first：字幕失败直接 `{ok:false,error:"无法获取视频字幕"}`。
- 成功时写入 `kb_items(media_type="video", source="bilibili", use_case="fact")`。
- 视觉预留：`VISION_ENABLED==1` 且 cover URL 存在时尝试 multimodal；失败不影响主流程。
- 强制来源：`_ensure_sources(text, [video_url])`。

### 2) 功能完备性与风险审查

- 降级：对 403/412 等反爬场景完全降级，不会崩 event loop。
- 资源：多次创建 `aiohttp.ClientSession`（view/player/subtitle 三段各自 session），高频 ingest 时连接开销偏大。
- 入库截断：`DB.upsert_kb_item` 内对 `text` `_truncate(..., 6000)`，长字幕提炼可能丢失细节。

### 3) 测试验证方案

目标：离线验证 blocked/no-subtitle 的错误结构与“永不抛异常”。

触发：`pytest -q`（见 `tests/test_p5_bilibili.py`）。

预期断言：

- `_fetch_bilibili_subtitles("")` 返回 `无法获取视频字幕`。
- monkeypatch `_fetch_bilibili_view` 返回 403 时，`ingest_bilibili_video` 返回 `{ok:false,error:"bilibili blocked",status:403}`。

## P6 (Event-Driven Loops)

### 1) 技术实现细节

Steam 旁路监听：`steam_audit_loop(bot)`

- 等待 WS ready：`if not bot.connected.is_set(): sleep(2); continue`。
- 对每个 push 用户：`steam.get_player_summaries(steam_id)` -> 计算 `_steam_state_hash(...)`。
- 持久化：
  - `steam_audit_events` 每次 poll 插入一行（含 `raw_json` + `dedupe_hash`）。
  - `steam_last_state` upsert 当前状态与 `last_hash`。
- 变化触发：
  - 触发 `_auto_push_game_intel`（`asyncio.create_task`，best-effort）。
  - 冷却：若距离 `last_push_ts` < `STEAM_AUDIT_PUSH_COOLDOWN` 则不 push。
  - push 文案：`_steam_state_push_text`（game_name 优先，否则上线/下线）。

画像偏移触发 Heybox 刷新：

- `_profile_shift_significant`（topK Jaccard >=0.60 或 L1 >=12.0）。
- 冷却：`cooldowns` 表 key `heybox_style:profile_shift`，窗口 12h。
- 触发：`create_task(_refresh_heybox_style(...))`。

冷却表：`cooldowns(qq_id,key,last_ts)`

- `DB.in_cooldown`：`now_ts - last_ts < window_sec`。

### 2) 功能完备性与风险审查

- 阻塞风险（事实）：`proactive_user_loop`/`proactive_group_loop` 使用同步 `search_gaming_info()`（duckduckgo_search），会阻塞 event loop。
- 数据增长：`steam_audit_events` 无限增长，源码未提供清理/归档逻辑。
- 并发：多个 create_task 无并发上限；但冷却表能降低重复触发频率。

### 3) 测试验证方案

目标：离线验证冷却逻辑、Steam state hash 稳定性、Steam last_state 的 upsert 结构。

触发：`pytest -q`（见 `tests/test_p6_cooldowns_and_steam_state.py`）。

预期断言：

- `set_cooldown_ts` 后在窗口内 `in_cooldown == True`。
- `_steam_state_hash` 对不同 game_id 产生不同 hash。
- `upsert_steam_last_state` 后 `get_steam_last_state` 字段一致。

## 缺失项声明（按你给的阶段定义对齐）

- P1：未提供 OneBot `notice/request/meta_event` 处理（当前只处理 message）。
- P3：未提供 embedding/vector store/chunking；仅基于 SQLite 文本 + FTS5（可选）。
- P5：视觉仅为 best-effort LLM 多模态调用占位；未提供图像缓存/结构化视觉解析。
