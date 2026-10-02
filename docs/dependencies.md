# 第三方依赖来源登记

> 从既有 README 迁入，保留当时的版本、来源、用途及许可记录；这是 2026-09-23 至 2026-09-30 的环境登记，不表示当前机器已安装或在线源信息已重新核对。当前约束以 backend/pyproject.toml、frontend/package.json 和锁文件为准。更换依赖时更新登记，不把历史版本强制当作唯一可用版本。


| 项目 | 记录 |
| --- | --- |
| 软件 | PyMuPDF |
| Python 包 | `pymupdf`，声明于 `backend/pyproject.toml`，版本下限 `pymupdf>=1.24` |
| 来源 | PyPI [pymupdf](https://pypi.org/project/pymupdf/)，上游仓库 [pymupdf/PyMuPDF](https://github.com/pymupdf/PyMuPDF)，产品页 [pymupdf.io](https://pymupdf.io/) |
| 本次测试版本 | 运行单元测试时环境中的 `pymupdf.version` 为 `('1.27.2.3', '1.27.2', None)`。下标 0 是 PyMuPDF 1.27.2.3，写入 `extractor_version`；下标 1 是所绑定的 MuPDF 1.27.2 |
| 用途 | 从文本型 PDF 提取文字块和页面坐标，并按块类型计数图片。不执行 OCR，不把图片字节放进解析结果 |
| 许可 | 官方许可页 [pymupdf.io/licensing](https://pymupdf.io/licensing) 写明 AGPLv3 或商业许可。这里只记录该页表述 |

本地预检和其测试还使用下面三个已安装的包。版本和许可证字段来自本环境 `importlib.metadata`，来源链接来自同一元数据中的项目地址。

| 软件 | 本次测试版本 | 来源 | 元数据中的许可证 | 用途 |
| --- | --- | --- | --- | --- |
| FastAPI | 0.136.3 | [fastapi.tiangolo.com](https://fastapi.tiangolo.com/)，仓库 [fastapi/fastapi](https://github.com/fastapi/fastapi) | `License-Expression`: MIT | 本地年度预检 HTTP 接口 |
| Uvicorn | 0.49.0 | [uvicorn.dev](https://uvicorn.dev/)，仓库 [Kludex/uvicorn](https://github.com/Kludex/uvicorn) | `License-Expression`: BSD-3-Clause | 按文档命令启动预检应用的 ASGI 服务器 |
| httpx | 0.28.1 | [python-httpx.org](https://www.python-httpx.org)，仓库 [encode/httpx](https://github.com/encode/httpx) | `License`: BSD-3-Clause | `TestClient` 调用本地预检测试；预检本身不靠它访问云端 |

`frontend/package.json` 声明了下面这些精确版本。2026-09-23 用 `npm view <包>@<版本> license` 核对过 registry 的 `license` 字段，本机 `npm ls --depth=0` 看到的安装版本与声明一致。许可证栏仍是该 registry 字段，没有另录安装目录中的许可证全文。

| 包 | 声明版本 | registry `license` 字段 | 用途 |
| --- | --- | --- | --- |
| react | 19.3.0 | MIT | 年度预检页面 |
| react-dom | 19.3.0 | MIT | 在浏览器中渲染该页面 |
| vite | 8.3.0 | MIT | 本机开发服务与构建。产品页 [vite.dev](https://vite.dev) |
| @vitejs/plugin-react | 6.1.1 | MIT | Vite 的 React 转换 |
| typescript | 5.9.3 | Apache-2.0 | `tsc --noEmit` 类型检查。产品页 [typescriptlang.org](https://www.typescriptlang.org/) |
| @types/react | 19.3.0 | MIT | React 的 TypeScript 类型 |
| @types/react-dom | 19.3.0 | MIT | ReactDOM 的 TypeScript 类型 |


## LangGraph 历史安装登记

本机验收环境通过保留 TLS 证书校验的清华大学 PyPI 镜像安装 LangGraph 1.2.12（https://pypi.tuna.tsinghua.edu.cn/simple）。该 wheel 的 SHA256 95403af7b510de8d79164742f71daaf866c69ca804a8cb72bef2cc1fa1ab9813 与官方 PyPI 1.2.12 发布元数据一致；官方包元数据标注 MIT 许可证。该包仅用于 agents 可选 extra 的 StateGraph 编排。

第三方资料来源与使用条件另见 [数据规则](data-policy.md)。以上为来源登记，不构成对分发条件的完整审查。

## Windows x64 便携运行时（2026-10-02）

便携构建器固定使用 Python.org 官方 [CPython 3.14.3 Windows x64 embeddable ZIP](https://www.python.org/ftp/python/3.14.3/python-3.14.3-embed-amd64.zip)，SHA256 `ad4961a479dedbeb7c7d113253f8db1b1935586b73c27488712beec4f2c894e6`。运行依赖从 Windows x64 CPython 3.14.3 构建环境按 `fastapi`、`python-multipart`、`uvicorn`、`pymupdf` 与 `langgraph` 的已安装 metadata 解析闭包；包内 `THIRD_PARTY_LICENSES.json` 逐项记录版本、PyPI 来源、上游许可字段及附带许可文件。构建器只复制 runtime closure，不复制全局 site-packages、console entrypoint 或 `node_modules`。

Microsoft Visual C++ Runtime DLL 取自经 Authenticode 验证的 Microsoft 官方 x64 [Visual C++ Redistributable](https://aka.ms/vc14/vc_redist.x64.exe)：版本 14.51.36247.0，签名者 Microsoft Corporation，原安装程序 SHA256 `843068991daaa1f73ad9f6239bce4d0f6a07a51f18c37ea2a867e9beca71295c`。候选包实际纳入经校验的 x64 payload 文件集合：`concrt140.dll`、`msvcp140.dll`、`msvcp140_1.dll`、`msvcp140_2.dll`、`msvcp140_atomic_wait.dll`、`msvcp140_codecvt_ids.dll`、`vcamp140.dll`、`vccorlib140.dll`、`vcomp140.dll`、`vcruntime140.dll`、`vcruntime140_1.dll`、`vcruntime140_threads.dll`。这是随包的 runtime payload 清单，不表示其中每个文件都单独被 PyMuPDF 调用；逐文件哈希见包内 `THIRD_PARTY_LICENSES.json`。这些 DLL 来自微软官方签名安装程序，分发和使用受微软软件许可条款约束，使用者需确认并遵循适用条款。

构建后的 Vite JS 使用 React 19.3.0、React DOM 19.3.0 与 scheduler 0.28.0。便携包仅附带这三个运行组件各自的原版 MIT `LICENSE`，不带 `node_modules`；包内许可清单记录 npm 来源、版本与文件路径。包的完整第三方来源、版本、文件哈希和许可索引均随 ZIP 分发；重建时必须更新并复核这些记录。

## 既有连接地址参考（历史登记）

以下从旧 README 原样迁入，未在本次文档整理中重新联网核对。实际配置以供应方当前说明为准，不自动选择供应方。

原记录：端点示例来自官方文档，不是本项目已经选定的服务：

| 供应方 | 根地址示例 | 文档 |
| --- | --- | --- |
| Qwen 华北 2（北京） | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | [阿里云兼容说明](https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope) |
| Qwen 新加坡 | `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` | 同上 |
| Qwen 美国（弗吉尼亚） | `https://dashscope-us.aliyuncs.com/compatible-mode/v1` | 同上 |
| DeepSeek | `https://api.deepseek.com` | [DeepSeek API 文档](https://api-docs.deepseek.com/zh-cn/) |
| OpenAI | `https://api.openai.com/v1` | [OpenAI Chat API](https://developers.openai.com/api/reference/resources/chat) |

北京和新加坡使用当前兼容文档推荐的工作空间专属域名。`{WorkspaceId}` 换成该地域的业务空间 ID，`MODEL_API_KEY` 必须属于同一地域。美国（弗吉尼亚）使用上表中的官方地址。

