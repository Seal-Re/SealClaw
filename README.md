# SealClaw

OneBot11 反向 WebSocket 机器人（Python/asyncio）。

- P0：WS 连接维持与事件循环生命周期
- P1：消息解析与管理员指令（Steam 绑定/推送开关）
- P2：LLM + 工具调用（OpenAI 兼容网关，stream 聚合）
- P3：RAG-lite（SQLite KB + 可选 FTS5 + Web 拉取 + 争议检测 + 强制来源行兜底）
- P4：用户画像（聊天关键词权重，指数衰减）
- P5：B 站视频字幕入库（字幕优先，403/412 优雅降级；可选多模态封面补充）
- P6：事件驱动闭环（Steam 状态旁路监听、自动情报推送、Heybox 风格刷新、12h 冷却）

## 运行

### Python

```bash
python -m pip install -r requirements.txt
python bot_core.py
```

### Docker

仓库内提供 `Dockerfile` / `docker-compose.yml`，按你的部署方式运行即可。

## 环境变量

关键项（详见 `bot_core.py` 顶部）：

- OneBot WS
  - `WS_URL`：默认 `ws://host.docker.internal:3001`
  - `OB11_ACCESS_TOKEN`：可选 Bearer token
- Steam
  - `STEAM_API_KEY`
  - `STEAM_AUDIT_INTERVAL`（轮询间隔，源码最小 sleep 15s）
  - `STEAM_AUDIT_PUSH_COOLDOWN`
- LLM
  - `LLM_API_KEY`（不设置则只跑 WS/指令/数据库，不输出 LLM 回复）
  - `LLM_BASE_URL`（可选 OpenAI 兼容网关）
  - `LLM_MODEL`（默认 `gpt-5.2`）
- RAG/KB
  - `DB_PATH`：默认 `data/bot.db`
- Heybox 风格
  - `HEYBOX_COOKIE`
  - `HEYBOX_STYLE_ENABLED`
  - `HEYBOX_FETCH_INTERVAL`
- 多模态
  - `VISION_ENABLED`：`1` 开启封面 best-effort 多模态补充

## 工程审计报告

见 `docs/bot_core_audit_P0-P6.md`。

## 测试

测试用例设计目标：离线可跑，不依赖外网/真实 OneBot/真实 Steam/真实 LLM。

```bash
python -m pip install -r requirements.txt
pytest -q
```
