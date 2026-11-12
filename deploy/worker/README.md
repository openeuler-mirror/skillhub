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
2. 启动补缺口 `run_remediation_on_startup`
3. discover 定时调度器（含首轮扫描）
4. license 清理调度器（等待 discover 首轮完成后启动）
5. LLM 审核调度器（等待 license 清理首轮完成后启动）
6. SkillSpector 安全采集 collector（`security.enable_audit` 开启时）

时序约束：license 清理必须等 discover 首轮扫描完成（`first_round_done`）后再启动；LLM 审核再等待 license 清理首轮完成，避免审核即将被删除的 skill。

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