"""本地 HTTP 接口。当前只有年度财务预检，无账户系统。"""

from finagent.api.app import app, create_app

__all__ = ["app", "create_app"]
