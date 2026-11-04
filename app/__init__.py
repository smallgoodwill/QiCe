"""期策 · 本地期货量化纸面模拟平台（后端包）。

模块划分：
    config     路径与运行期常量、.env 载入
    indicators 纯函数技术指标（SMA/EMA/ATR/RSI）
    engine     统一策略引擎（信号判定 + 撮合记账），回测与模拟盘共用
    backtest   回测编排（训练段/测试段切分、指标统计）
    trading    多策略模拟运行（逐 bar 推进、状态序列化）
    db         SQLite 持久化
    ai         DeepSeek / GLM 问答
    simnow     SimNow(CTP) 行情网关（可选依赖）
    api        HTTP 路由与静态资源白名单
    server     进程入口
"""

__version__ = "2.0.0"
