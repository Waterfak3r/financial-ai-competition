# 决赛技术报告证据索引

本目录收录 FINTRACE 团队原创模拟样例、生成的结构化结果、代码/包指纹、脱敏演练摘要与原创示意图。真实年报 PDF、真实材料整页图、模型原始请求/响应、密钥和宿主路径均不纳入可分发证据。原创样例的性质、许可与用途限制见项目 [`docs/data-policy.md`](../../../docs/data-policy.md) 及样例目录的使用说明；端到端本机操作与记录表见[运行与交付验收指南](../运行与交付验收指南.md)。

## 年度原创样例

`SIM-ANNUAL-001`（澄湾云算股份有限公司，虚构）2025 年 PDF SHA256 为 `f193e7a497fe483d3ab70b7bde2880f715ff1127b1d8b14cc3f4fda678ba0bf7`。下列归档均为 deterministic、`completed`、`model_called=false`，来源字节与样例 SHA 一致。前两行是先前完成的 CLI/Web 历史运行；新 UI 演练的 run manifest 记录冻结提交 `a0b5b3bc26b3004afcd359df48544adb9cba4a13` 和 `git_dirty=true`：

| 用途 | Job / run ID | Git 快照 | 工作树 | 来源文档 ID |
| --- | --- | --- | --- | --- |
| CLI 确定性分析 | `annual-analysis-4b7a43fb-e9ad-47b1-9319-5b8b7533211e` | `c267a2bce9d6b7b996590a7f8b1e1ce62523d469` | `git_dirty=true` | `sim-annual-001-2025` |
| Web 确定性分析与接受的 API 导出 | job `annual-job-4303a6f6da3d4f57bc930c6421d3a809`；run `annual-analysis-caffff8e-1937-4544-ad2f-b3be7f406c47` | `af13785d9ba9d635ea79e670c42beb54415d9bbd` | `git_dirty=true` | `upload-4783d32278ce461897bad41dec94b5f0` |
| 本轮 UI 上传、三格式下载、证据点击与刷新回看 | job `annual-job-12b2970426b64de7a6b48d5caededfa3`；run `annual-analysis-a96e4ee6-2341-46c6-ac18-7729849a16c0` | `a0b5b3bc26b3004afcd359df48544adb9cba4a13` | `git_dirty=true` | `upload-c00acd385bd348fc8caf635deb4f2d6e` |

前两次归档的 30 个年度源码指纹与其历史内容保持不变；新 UI 演练用的 31 个年度后端源码指纹（含导出模块）逐项匹配软件冻结提交，前端工作树文件也与冻结提交一致。新演练结果包含 8 条已核验事实、8 项已核验计算、16 条 verified Claim 和 2 条待核查候选；下载 JSON、Markdown、HTML 均成功，桌面 1366×900、1440×900、768×1024、390×844 均无文档横向溢出。手机截图时人工将报告标题滚动到视口顶部（`block: start`）后拍摄，不构成自动定位检查。完整、脱敏的 job/run、文件 SHA 和演练边界见 [`annual-sample-web-ui-smoke.json`](annual-sample-web-ui-smoke.json)；精确代码指纹见 [`annual-sample-ui-code-fingerprint.json`](annual-sample-ui-code-fingerprint.json)。桌面图与真正 390×844 视口图分别为 [`annual-sample-web-ui-run-a96e4ee6-2341-46c6-ac18-7729849a16c0-desktop.png`](annual-sample-web-ui-run-a96e4ee6-2341-46c6-ac18-7729849a16c0-desktop.png) 和 [`annual-sample-web-ui-run-a96e4ee6-2341-46c6-ac18-7729849a16c0-mobile-390x844.png`](annual-sample-web-ui-run-a96e4ee6-2341-46c6-ac18-7729849a16c0-mobile-390x844.png)。

主控已接受年度白名单导出专项与 UI/离线打印验收。接受的 API 导出 `annual-analysis-caffff8e-1937-4544-ad2f-b3be7f406c47` JSON 为 [`annual-sample-api-export-run-caffff8e.json`](annual-sample-api-export-run-caffff8e.json)，175,190 字节，SHA256 `2f5801a2e7cbc57946767fab5ad6012d027f58057af16949b5f96779793a3a31`；最新 UI 运行的 JSON 另存为 [`annual-sample-web-ui-run-a96e4ee6-2341-46c6-ac18-7729849a16c0.json`](annual-sample-web-ui-run-a96e4ee6-2341-46c6-ac18-7729849a16c0.json)，SHA256 `a9e0fd8be501bf655f6d2ea579972faaed5c7ed480846f0199387e29afc2a971`。C03 专项评测的脱敏摘要见 [`annual-report-export-c03-summary.json`](annual-report-export-c03-summary.json)；原始详细审阅记录 `artifacts/evaluations/report-export-c03-20261003/evidence.json` 及其 SHA256 `dd6922057f20f2e18345900bc890a86a62d599845fe3dd7b03f5c04411bb38a4` 只在当前开发机，不随源码 ZIP 提供。导出规则见[年度报告导出说明](../../../docs/annual-report-export.md)。

## 季度原创样例

`SIM-QUARTERLY-001` 的完整 2024 Q2 分析归档 `quarterly-analysis-6e7807fc-f25c-4dbc-bf80-5323fc902296` 为 `completed`；读取 4 份原创模拟 PDF，24 个来源观察、9 个季度派生值、6 个比较均为 `verified`。季度分析是本地 deterministic CLI，未调用模型。分发的 [`quarterly-analysis-6e7807fc-f25c-4dbc-bf80-5323fc902296.json`](quarterly-analysis-6e7807fc-f25c-4dbc-bf80-5323fc902296.json) 从正式运行归档逐字节复制，SHA256 为 `25fc2fe1e5941ea44a3bbc475742b736e8c2f0653d3f3f549d6ea396325e8190`；JSON 内含 7 个实际分析/验证模块的 SHA256、4 个输入 PDF 哈希及来源性质。

随包复现依据为本目录分发的季度 `analysis.json`、[季度模拟来源清单](../../../data/samples/quarterly/source_manifest.json)及[技术报告第 6 节](../FINTRACE-决赛技术报告.md#6-原创季度案例与真实材料边界)。`artifacts/reports/quarterly-analysis-6e7807fc-f25c-4dbc-bf80-5323fc902296/review-evidence.md` 仅是当前开发机的审阅记录，不随分发包提供。分发包中的季度 JSON、`source_manifest.json` 与报告第 6 节才是可复现材料。真实海天季度 run `quarterly-analysis-8118d2c1-aa50-4c2f-a6c2-7775b0557253` 的报告与证据仅供本地核验，不拷入本提交目录。

## 软件冻结检查与源码包候选同机烟测

[`software-freeze-checks.json`](software-freeze-checks.json) 记录软件冻结 `a0b5b3bc26b3004afcd359df48544adb9cba4a13` 的完整后端套件、前端 typecheck/build 以及平台相关跳过原因。浏览器新运行演练及其接受边界见上方年度 UI 摘要。仓库使用 `.gitattributes` 的 `evidence/*.json -text` 规则保留正式证据 JSON 的原始字节，尤其用于季度归档 JSON 的 SHA256 逐字节核对；该仓库字节保护规则不改变应用行为，也不作为运行时文件纳入源码包。

[`source-copy-smoke-candidate.json`](source-copy-smoke-candidate.json) 是候选源码包在当前 Windows x64 / CPython 3.14.3 主机、中文空格路径下的脱敏 smoke 摘要。它记录候选源码 commit/tree、ZIP 与 runtime lock SHA，以及 deterministic 年度 CLI、季度 CLI 和本机 API 的成功状态、核验计数与 `model_called=false`；不记录本机绝对路径、凭据或环境变量值。该候选烟测不代表最终文档交付快照或另一台电脑/干净 Windows 环境已通过。最终源码 ZIP 按正式文档交付 commit/tree 构建，具体 package ID、source commit/tree、ZIP SHA256 与包校验记录以包外 manifest、`.sha256` sidecar 和独立验收记录为准。

## 原创图示

- [`architecture.svg`](architecture.svg)：根据当前架构契约由本项目绘制，SHA256 为 `10a6f4a9a505cee22c6cdf9ac5260e2bb5ed538ac84f4d68750733be5253a775`。图中年度走 Web/API、季度同比环比走独立 CLI；PDF 来源进入基础核验、固定候选与全文发现分支，最终评审只读基础报告和有限发现、不重读全文，M3 不输入 AI。PDF 中以 ReportLab 原生矢量重绘相同数据流，不是运行截图。 最终 SVG 的 Edge 1320×650 原尺寸检查截图留在 `artifacts/tmp/annual-discovery-final-20261005/architecture-svg-browser-1320x650.png`，主控已查看并接受最终评审节点行距与小箭头。

## 年报开放式发现 mock 展示与软件检查

当前应用软件基线为 `07cb4e97e7b43327aa35ed7d6db260ec28fb1c59`（10/05）。[`annual-discovery-software-checks.json`](annual-discovery-software-checks.json) 是本轮脱敏软件检查摘要，汇总后端完整套件、两份构造 PDF 的 CLI→本地 mock→归档 GET/页图/三格式导出，以及分开运行的前端 17 场景和旧终态 job 单场景、typecheck/build。后端首轮失败日志及修复后最终日志均保留；`.invalid` dummy connector 的尝试次数无法精确确认，不能概括所有检查均由全局网络哨兵阻断。上述都是构造数据、本地替身和指定机器上的软件检查，不是在线 provider 输出或全文语义准确性验收。

下列普通视口截图逐字节复制自已审阅的前端 mock QA 截图，没有裁切或图像加工；页面中的公司/PDF 文本和动态发现均为构造数据，不能当成真实公司分析。桌面截图为 1366×768，手机截图为 390×844。浏览器 mock 使用 1×1 PNG 仅检查抽屉交互与来源协议；这里的页面截图不证明生产 PDF 页图的视觉定位。mock 下载文件也不验证导出内容，导出字段边界由后端合成 PDF E2E 测试检查。

- [桌面 mock 首屏，1366×768](annual-discovery-mock-desktop-1366x768.png)（SHA256 `95338f6a8f40e215489edbf105f860014bad3f7b4f06c1e0072b6d23d4697b91`）
- [手机 mock 首屏，390×844](annual-discovery-mock-mobile-390x844.png)（SHA256 `84d1531f077fd29a3b049e57e4493d01581ba6bd63736636a33023973bbefcf7`）

版本代码原字节与 Git LF 规范化字节指纹见 [`annual-discovery-code-fingerprint.json`](annual-discovery-code-fingerprint.json)。PDF 页面输出、检查命令与报告日期记录见[验收历史](../../../docs/verification-history.md#2026-10-05-年报开放式发现软件阶段验收与决赛文档装配)。当前决赛技术报告 PDF SHA256 为 `0971e763370e55cf4253a5e10c6f0b18130aed66affbf28398376090a3dcd302`，共 9 页；逐页 110 dpi PNG 尺寸和 SHA256 见[渲染清单](../../../artifacts/evaluations/annual-discovery-final-20261005/pdf-render-manifest.json)。修正 Markdown 硬换行后重建，新版与此前已接受 PDF（SHA256 `4e0d8ba8459e3ae3ca7b6852a76e75495d819f5a12b242770f43d51fd7e7318b`）的 9 页 PNG 均逐字节一致，主控已复核并接受当前 PDF。真实 provider 长任务耗时/发现质量、跨公司语义人审、OCR/图像文字（当前不支持）尚未验收；其他决赛外部关卡也未因本地 mock 通过而完成。

## 验收状态

本索引是决赛成品装配中的审计入口，不代表整体决赛验收完成。软件冻结 `a0b5b3bc26b3004afcd359df48544adb9cba4a13`、完整后端/前端检查、年度白名单导出和报告/PDF逐页视觉检查已接受。最终源码 ZIP 从含本报告与正式证据的文档交付 commit/tree 构建；确切 package ID、source commit/tree、ZIP SHA256 和构建校验结果以 ZIP 同目录包外 manifest、`.sha256` sidecar 与独立验收记录为准，不在报告内自引。C-01 真人复核、C-02 真实季度完整人工复核、另一台 Windows 电脑干净安装、组委会现场网络/API 规则和非金融用户理解试验仍按产品路线与验收记录跟踪。计划书 PDF、视频 MP4 与平台上传由独立材料负责人完成。
