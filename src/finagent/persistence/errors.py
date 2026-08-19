"""持久化基础设施对应用层暴露的稳定异常。"""


class PersistenceError(RuntimeError):
    """所有数据库基础设施错误的基类。"""


class DatabaseSchemaError(PersistenceError):
    """数据库尚未迁移，或结构版本与当前代码不一致。"""
