# worker 服务

后台任务工作进程，与 `api` 共用同一个镜像（[../api/Dockerfile](../api/Dockerfile)），仅在 `compose.yaml` 中通过 `command` 覆盖启动命令：

```yaml
worker:
  build:
    context: ..
    dockerfile: deploy/api/Dockerfile
  command: ["python", "-m", "src.jobs.worker"]
```

## 职责

`api` 是无状态 Web 服务，不启动任何后台任务。本服务（单副本、`replicas: 1`）承载全部后台任务，按顺序执行：

1. 一次性迁移 `run_migration`
2. discover 定时调度器（含首轮扫描）
3. 下游任务：license 复核调度器与 LLM 审核调度器
4. SkillSpector 安全采集 collector（`security.enable_audit` 开启时）

时序约束：

- license 复核与 LLM 审核都等待 discover 首轮扫描完成（`first_round_done`
  事件）后再启动，两者互不依赖、并行运行；
- license 复核此后跟随每一轮 discover：扫描结束后触发一轮全量复核，
  避免与 discover 并发读取正在更新的本地 clone；discover 未启用时退化为
  `license_check.start_time` 每日触发；
- discover 扫描新入库/内容变化的 skill 时会同步完成一次 license 预检，
  新数据的 `license` / `license_compliant` 立即可用。

## 运行

```bash
cd deploy
docker compose up -d --build
# 单独启动 worker
docker compose up -d worker
```

日志落在 `<storage.local_path>/logs/worker.log`（默认 `/opt/wittyhub/logs/worker.log`），与 `api.log` 分离。

## 为什么没有独立的 Dockerfile

worker 与 api 共享全部应用代码、依赖与数据库迁移，仅启动入口不同（`python -m src.jobs.worker` vs `uvicorn src.api.main:app`）。为保持单一镜像、避免重复构建，worker 直接复用 `deploy/api/Dockerfile`，其 `ENTRYPOINT`（等待数据库就绪 + `alembic upgrade head` + `pg_advisory_lock` 串行化迁移）对两个服务均生效。