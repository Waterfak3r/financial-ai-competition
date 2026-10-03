# 决赛源码包

源码包构建器从指定的完整 Git commit 读取 tree 和 blob 字节，不从当前工作树复制源码。构建前先将要交付的应用、受控原创样例、最终报告源和复现证据提交到同一快照；未提交的文件不会进入包。包是源码交付物，不含 Python、Node.js、安装好的依赖或 `node_modules`，也不等于在另一台电脑完成了干净安装验收。

## 纳入范围

包的 ZIP 根目录固定为 `FINTRACE_SOURCE/`，包括：

- 根目录的 `README.md`、`AGENTS.md`、`start.bat`、`.gitignore` 与经校验的空值 `.env.example`。
- `backend/src/finagent/`、`backend/pyproject.toml` 与 Windows x64 / CPython 3.14.3 受测 runtime 锁 `backend/requirements-final-win.txt`。
- 前端源码、`public/` 资产、构建配置、`package.json` 和精确 `package-lock.json`。
- `config/prompts/` 程序提示词、`config/rules/` 业务规则、Python/PowerShell/批处理脚本、Markdown 项目文档、自动化测试和版本化 `evaluation/` 协议。
- `submission/final/` 下的报告源文件与已审阅提交 PDF，以及正式复现证据 `.json`、`.txt`、`.csv`、`.png`、`.svg`。
- `data/samples/annual/` 和 `data/samples/quarterly/` 的受控原创模拟样例。准入依据是各自固定 `dataset_id`、对应 schema、明确的原创模拟性质和分发条件，并以 manifest 登记的 SHA256 校验实际 PDF 字节；不根据文件名推断许可。年度 manifest 的分发 flags 与季度 manifest 的中文 use conditions 分别按各自 schema 检查。

运行依赖闭包只从当前受测环境的已安装元数据解析 `fastapi`、`python-multipart`、`uvicorn`、`pymupdf`、`langgraph` 五个根及其适用 marker 闭包。包内 `THIRD_PARTY_LICENSES/python/manifest.json` 记录版本、PyPI 与上游元数据链接、许可元数据字段、附带许可文件路径、长度和 SHA256。许可文本仅从已安装 distribution 的 metadata 文件中收集，不复制 site-packages 或宿主绝对路径。

## 排除范围

- Git 元数据、所有非示例 `.env*`、虚拟环境、`node_modules`、缓存、构建输出、临时目录、依赖安装目录和本地 `artifacts/` 产物。
- `data/raw/`、`data/processed/`、未由受控 manifest 绑定的样例 PDF、整页真实来源图、历史运行/任务/评测归档、模型请求与响应记录。
- `submission/proposal/`、`submission/video/` 及其内容。
- 未确认有再分发许可的公开财报 PDF。源码包不附带真实 PDF；使用者应根据项目数据规则中的官方来源指引自行取得原文件，并核对其 SHA256 和适用条款。
- 嵌入式 Python/Node 运行时、Python wheel、npm 包和前端 `dist/`。

内部团队便携器 `scripts/build_windows_portable.py` 还包含未确认可公开再分发的真实海天 PDF 和历史回放材料，面向内部场景，不用于生成公开决赛源码包。

## 构建与验证

在仓库根目录使用完整 commit ID 和从未使用过的唯一 package ID：

```powershell
python scripts/build_final_source_package.py `
  --source-commit <完整的40位Git commit ID> `
  --package-id fintrace-final-source-20261003-a1b2c3d4
```

构建器会将 ZIP、外部 SHA256 sidecar 和逐文件清单写入 `artifacts/packages/<package_id>/`；内部 staging 留在 `artifacts/tmp/final-source-package-<package_id>/`，验证报告写入同名的 `artifacts/evaluations/` 任务目录。已有 package ID 或任务目录会拒绝覆盖。

只读复核成品时运行：

```powershell
python scripts/build_final_source_package.py --verify artifacts/packages/<package_id>
```

校验覆盖 Git 快照标识、固定 ZIP 根与路径安全、ZIP 外层长度和 SHA256、每个文件的长度和 SHA256、提示词/规则/前端锁/runtime 锁哈希、runtime 版本锁、依赖许可清单，以及样例 manifest 性质、分发条件和 PDF 字节 SHA。ZIP 和清单是本地产物，不提交到 Git。

当前 runtime 锁只限定于本机受测的 Windows x64、CPython 3.14.3；PEP 508 markers 按该环境求值，不能视为适用于所有 Python 版本或平台的通用锁。`httpx` 因 runtime 依赖闭包而出现在锁中；`pytest` 等测试专属依赖仍由 `backend/pyproject.toml` 的 `[test]` extra 管理。依赖和许可登记见 [第三方依赖来源登记](dependencies.md)。许可 metadata 与附带文本用于来源追踪，不表示已经完成全部分发法律审核；特别是 PyMuPDF metadata 声明 AGPLv3 或 Artifex 商业许可，分发方须评估适用条件。
