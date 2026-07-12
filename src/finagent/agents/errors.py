"""Agent 编排层的统一异常。

Agent 错误与模型 Provider 错误、工具错误分开：Provider 负责“模型是否可访问”，工具层
负责“单个工具能否执行”，Agent 层负责“整个多步循环能否安全结束”。CLI 因而可以
针对不同层级给出不同提示。
"""


class AgentError(RuntimeError):
    """所有 Agent 编排异常的基类。"""


class AgentResponseError(AgentError):
    """模型响应无法形成最终回答，也没有可执行的工具调用。"""


class AgentStepLimitError(AgentError):
    """Agent 在限制的模型调用步数内没有产生最终回答。"""
