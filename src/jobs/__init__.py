"""后台任务层：随进程生命周期运行、不服务 HTTP 请求的任务。

- ``discover_scheduler``：按配置时刻触发 skill discover 的调度循环；
- ``remediation``：衍生字段缺口（security/category/embedding）的补齐编排。

与 ``src/api/services/`` 的分工：后者是被 API 路由调用的请求层服务
（审计报告、遥测等）；本包是长驻后台任务，CLI（skillcrawler）与 API
共同调用，不反向依赖 ``src.api``。
"""
