"""SkillSpector LLM 复核：上下文解析、判定逻辑与结果应用。

- :mod:`remote_raw`：GitHub/GitCode/GitLab/Gitee raw 文件拉取；
- :mod:`context`：本地 repo 缓存 -> 远端 raw -> snippet 的复核上下文；
- :mod:`constants`：提示词版本、评分参数与共享常量；
- :mod:`analysis`：命中解析、结构化判定、评分与严重度排序；
- :mod:`prompt`：提示词构造、模型响应解析与 verdict 匹配；
- :mod:`apply`：复核结论应用（误报删除、分数重算、元数据）；
- :mod:`queue`：客户端限流与时间窗口计算。
"""
