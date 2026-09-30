"""本地 HTTP 接口。仅在显式读取 app 工厂时加载 ASGI 应用。"""

__all__ = ["create_app"]


def __getattr__(name: str):
    """保留公开导出，同时避免导入 API 子模块时启动应用状态恢复。"""

    if name == "create_app":
        from finagent.api.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
