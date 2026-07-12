"""工具层使用的统一异常。

工具参数可能由大模型生成，工具实现也可能访问数据库、行情接口或本地文件，因此失败
原因不能全部混在普通 ``ValueError`` 中。这里定义稳定的异常类型，让未来的 Agent 循环
可以针对“工具不存在、参数错误、执行失败”分别生成安全的工具结果并决定是否重试。
"""


class ToolError(RuntimeError):
    """所有工具层异常的基类。"""


class ToolNotFoundError(ToolError):
    """模型请求了注册中心中不存在的工具。"""


class DuplicateToolError(ToolError):
    """注册了重名工具，导致调用无法唯一分派。"""


class ToolValidationError(ToolError):
    """模型生成的工具参数未通过该工具的输入模型校验。"""


class ToolExecutionError(ToolError):
    """参数合法，但工具内部执行过程失败。"""
