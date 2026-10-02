# Windows 便携包

## 队友如何启动

先将完整 ZIP 解压到文档、桌面等自己有写权限的位置，再双击包根目录的“启动 FINTRACE.bat”。浏览器会自动打开已完成的海天 603288、2024 年报 AI 分析历史页面。不要在压缩包预览窗口中直接运行。

这是面向 Windows 10/11 x64 的内部演示包。使用者无需安装 Python、Node.js、npm、pip、项目依赖或管理员工具。包内含匹配的 CPython 3.14.3 运行时、后端依赖、预构建前端、项目必要源码与所选演示归档。首次启动只在本机 `127.0.0.1` 提供页面；默认尝试 18765–18785 中第一个可用端口。同一个解压目录只允许一个服务实例，重复双击会提示切换到已打开的窗口。关闭启动窗口可停止本机服务；若目录不可写或端口范围均被占用，窗口会保留错误提示，不会结束未知进程。

页面“分析年报”可使用随包海天 PDF 启动新的确定性分析，亦可选择 M3 试行筛查。它们使用包内的嵌入 Python 子进程；无需联网调用模型。若队友要实际使用 AI 调查，需自行选择供应方和型号，在页面设置里填写服务地址与 API 密钥。没有随包默认供应方、型号或密钥；只有显式开始 AI 任务才会向自行选择的服务发送所需年报片段。密钥只写入解压目录中的本地 `.env.model`，请妥善保管该目录，不要把设置文件转发给其他人。

## 随包演示材料

包内含海天原始 PDF `data/raw/603288/2024/cninfo-1222994233/1222994233.PDF` 和来源记录、所需的解析材料、旧预检归档，以及以下已存在的历史分析与任务归档：

| 历史用途 | 运行记录 |
| --- | --- |
| AI 调查与最终评审 | `annual-analysis-8036d75c-6a13-432a-b602-5f869f9396ce` |
| 缺少原因的确定性结果 | `annual-analysis-21d8da07-097b-4cdf-97ce-6e109c8cc1b8` |
| M3 四规则试行结果 | `annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21` |
| 标准年度分析 | `annual-analysis-a736d7e1-a932-4f88-9a0c-057f20d6c998` |
| 兼容的旧预检 | `annual-precheck-603288-2024-20261002-133803` |

这些记录用于回看演示，不代表随包实时运行结果，也不构成跨公司泛化证据。团队内部传递此次指定材料不等于取得真实年报、原文片段或历史报告的公开再分发许可。请勿把整包或其中资料公开上传。包内 `portable-package.json` 保存源码提交、所选运行标识和逐文件 SHA256；`THIRD_PARTY_LICENSES.json`、`MICROSOFT_RUNTIME_SOURCE.txt` 与 `许可与来源.txt` 登记运行时的来源与许可文件。

## 维护和重建

正式 ZIP 与外部 SHA256、清单保存在 `artifacts/packages/<package_id>/`。该目录为本地生成产物，按项目规则忽略且不提交 Git；不要覆盖旧 package ID。重建前更新前端 dist：

```powershell
Set-Location frontend
npm run build
Set-Location ..
python scripts/build_windows_portable.py --package-id fintrace-windows-x64-20261002-rebuild01
```

构建器要求在 Windows x64 CPython 3.14.3 项目开发仓库中运行，并使用已验证的官方 Python embedded ZIP、Microsoft x64 Visual C++ Redistributable DLL 布局和项目所需 Python 依赖。构建器通过 Git 读取已跟踪源码；交付副本没有 `.git` 元数据，不能在便携包内重建。每次重建前将示例中的 package ID 换成一个新的、符合字符规则的唯一值，避免覆盖旧产物。任务缓存与每次唯一 staging/ZIP 临时文件放在 `artifacts/tmp/windows-portable-20261002/` 并按规则保留。外部 ZIP 的 SHA256 位于同目录 `.sha256` 与 manifest；包内文件清单位于 `portable-package.json`。第三方来源和许可证索引见 [dependencies.md](dependencies.md)。

## 验收边界

本次在现有 Windows x64 主机上，从包含中文和空格的路径解压副本运行验收。进程环境模拟没有全局 Python/Node 路径且清空模型凭据变量，并验证了包内运行时隔离和本机功能；这不是在干净虚拟机或另一台 Windows 10/11 电脑上的安装测试。运行记录及限制见[验收记录](verification-history.md)。
