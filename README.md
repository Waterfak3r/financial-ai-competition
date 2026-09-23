# 财务异常识别与舞弊风险核验智能体

面向北京市大学生金融人工智能竞赛第 2 题“上市公司财务报告分析”，通过财报解析、大模型分析、独立财务计算与证据核验，形成可追溯的财务异常和舞弊风险分析结果。

**当前阶段：目录骨架已建立。文本型 PDF 逐页解析、四项年度事实提取、确定性同比和 Chat Completions 文本连接器已可本地调用；公开年报的解析与字段计算已跑通。** 云端实测和智能体分析接口尚未实现。舞弊分析、独立原文核验和 Web 服务也未实现。当前没有可启动的 Web 服务，也还没有对云端模型做过实测。

## 协作入口

开始开发前阅读 [AGENTS.md](AGENTS.md)。项目开发由 gpt-6-astra 持续负责实现策略和方向、拆解、验收与汇报，由 grok-4.7 负责具体实施，通过 Herdr 协作。主控按任务复杂度和精细度决定 reasoning effort，并在派发时明确；派发前核对实际模型为 grok-4.7。较大任务在 Grok 完成后由主控审阅是否符合既定方向及验收要求。

每次协作经主控审阅后，主控使用 gpt-6-luna 子代理把事实性协作记录追加到 [coop.md](coop.md)。该分工用于项目开发协作，后端分析财报所用云端模型仍待选定。细则见 [AGENTS.md](AGENTS.md)。架构与模块职责见 [架构说明](docs/architecture.md)，数据与产物管理见 [数据管理说明](docs/data-policy.md)。比赛原文位于 [比赛通知.txt](比赛通知.txt)。

方案材料位于 `submission/proposal/`：[原始大纲](submission/proposal/多智能体协同财务欺诈识别方案总结大纲.docx)保持原样；[修订大纲](submission/proposal/多智能体协同财务欺诈识别方案总结大纲_修订版.docx)保留 13 部分结构，明确第一版仅采用 LangGraph 编排、技术范围、独立核验、基础财报分析与评测方案。其中规则阈值和验收安排属于拟实施方案，尚无实验结果。

## 技术方向

| 部分 | 约定 |
| --- | --- |
| 前端 | React + TypeScript + Vite |
| 后端 | Python + FastAPI |
| 展示方式 | 本地浏览器访问 Web 界面 |
| 未来默认开发地址 | 前端 http://localhost:5173，后端 http://localhost:8000 |
| 模型 | 已有供应方中立的 Chat Completions 文本连接器。默认供应方和型号未选定，由环境变量配置 |
| 编排 | 第一版采用 LangGraph，不叠加其他智能体协作框架 |
| 计算与核验 | 本地 Python 执行财务公式、统计筛查和数值核验，保留原文依据 |
| 当前依赖状态 | `backend/pyproject.toml` 声明本增量运行依赖 PyMuPDF。FastAPI、LangGraph 与前端依赖尚未引入，仓库也不代为安装 |

第一版路线确定为“云端 API + 本地轻量 Web + 本地统计计算”，不要求本地 GPU，也不纳入模型本地部署或训练。原始财报、索引、计算和运行记录保存在本地，模型调用仅发送本任务所需且允许外发的片段。云端 API 需要网络；受控运行限制资料范围与外部连接，现场是否允许模型联网仍需依据组委会环境说明核实。历史回放与在线重新运行明确区分，断网回放不能替代实时处理验收。

## 目录导航

```text
.
├── AGENTS.md          # AI 协作与文件管理规则
├── README.md          # 项目入口与当前状态
├── coop.md            # 主控审阅后追加的事实性协作记录
├── 比赛通知.txt        # 原始比赛资料
├── .gitignore         # 本地文件与生成产物的忽略规则
├── .env.example       # 模型连接配置项
├── docs/              # 架构、数据与证据管理说明
├── frontend/          # 本地 Web 前端
├── backend/           # 财报分析服务与智能体
├── config/            # 提示词与财务规则配置
├── data/              # 原始资料、处理结果和演示样例
├── artifacts/         # 运行记录、报告、评测结果和公共 tmp/
├── evaluation/        # 评测案例、标注与对照基线
├── tests/             # 单元、集成和完整流程测试
├── scripts/           # 可复用工程脚本
└── submission/        # 初赛计划书、视频和决赛材料
```

前端目录预留页面、组件、接口调用、类型和样式。后端按财报解析、检索、智能体、财务计算、核验和报告等职责划分，具体边界见架构文档。

## 文件存放约定

| 文件类型 | 存放位置 |
| --- | --- |
| 原始财报 | data/raw/ |
| 解析文本、表格、标准化数据与索引 | data/processed/ |
| 可交付演示样例 | data/samples/ |
| 任务执行记录和可复现中间结果 | artifacts/runs/<run_id>/ |
| 生成的分析报告 | artifacts/reports/<run_id>/ |
| 评测输出 | artifacts/evaluations/<evaluation_id>/ |
| 模块内临时脚本、截图和试验结果 | 所属模块的 tmp/<task_id>/ |
| 跨模块临时内容 | artifacts/tmp/<task_id>/ |
| 可复用脚本 | scripts/ |
| 评测案例和标签 | evaluation/cases/ |
| 比赛材料 | submission/ 对应阶段目录 |

tmp/ 按需创建，正式功能不能依赖其中的文件。已有 tmp/、任务子目录及其中临时文件默认保留，任务结束不主动清理；用户明确要求清理时，按 AGENTS.md 核对归属和路径后执行。正式运行证据仍需归档并保留。

正式空目录通过 .gitkeep 保留。原始数据、处理后数据及运行产物默认忽略；演示样例、评测案例和比赛材料可按来源与实际提交需要纳入版本管理。当前已初始化 Git 仓库。

## 配置示例

`.env.example` 预留三个空值，程序不在示例文件里选定供应方、型号或密钥：

- `MODEL_BASE_URL`：Chat Completions 服务根地址。连接器会在末尾追加 `/chat/completions`，根地址本身不要写这个路径。
- `MODEL_API_KEY`：访问密钥。可以写在未提交的本地 `.env` 里私下保存。错误信息不会回显密钥或完整请求正文。
- `MODEL_NAME`：请求体里的 `model`。

`load_model_settings()` 只读取进程环境变量 `os.environ`，没有 `.env` 自动加载器。把这三项写进本地 `.env` 并不会自动生效；调用前需由用户或后续启动器先载入进程环境。`complete_chat()` 只发送三家共同的字段 `model`、`messages` 和 `stream=false`，并接受 `system`、`user`、`assistant` 纯文本。默认超时 60 秒。这是 Chat Completions 的共有子集，不表示各家的工具调用、视觉、流式或其他扩展可以互换。

端点示例来自官方文档，不是本项目已经选定的服务：

| 供应方 | 根地址示例 | 文档 |
| --- | --- | --- |
| Qwen 华北 2（北京） | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | [阿里云兼容说明](https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope) |
| Qwen 新加坡 | `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` | 同上 |
| Qwen 美国（弗吉尼亚） | `https://dashscope-us.aliyuncs.com/compatible-mode/v1` | 同上 |
| DeepSeek | `https://api.deepseek.com` | [DeepSeek API 文档](https://api-docs.deepseek.com/zh-cn/) |
| OpenAI | `https://api.openai.com/v1` | [OpenAI Chat API](https://developers.openai.com/api/reference/resources/chat) |

北京和新加坡使用当前兼容文档推荐的工作空间专属域名。`{WorkspaceId}` 换成该地域的业务空间 ID，`MODEL_API_KEY` 必须属于同一地域。美国（弗吉尼亚）使用上表中的官方地址。

连接器不会自动发送整份 PDF，也还没有接到智能体分析接口。测试使用本地模拟 HTTP，不访问上述云端地址。

## 已实现：文本型 PDF 逐页解析

`finagent.ingestion.parse_text_pdf` 读取文本型 PDF，返回逐页文字块。结果包含文档标识、原始文件 SHA256、文件名、PDF 1-based 页序号、块文字和页面坐标，可用 `to_json()` 序列化。函数只在内存中打开文件字节，不修改原 PDF，也不写入 `data/processed/`。

文字块边界框使用未旋转页面坐标：原点在左上角，x 向右、y 向下，单位为 PDF point（1/72 英寸）。PyMuPDF 提取文字时按 0 度旋转计算边界框。结果里的页宽和页高来自当前 `page.rect`；页面旋转 90 或 270 度时，显示宽高与未旋转页面对调，不能拿这组宽高去套文字块坐标。

空白页、只有图片或矢量图形的页、以及提取结果只有空白的页，状态为 `no_extractable_text`，文字块为空。本增量不执行 OCR，也不编造文字。页内同时有文字和图片时，只返回文字块，并注明图片未做 OCR。加密或损坏的 PDF 会报错，不会被当成空白页。

在解析结果之上，还可以提取合并利润表营业收入、归属于母公司股东的净利润、合并现金流量表经营活动产生的现金流量净额，以及非经常性损益表的披露合计，并对同一指标的报告年和上一年做确定性同比。这不是通用财报抽取，也不是独立原文核验。云端侧目前只有同步 Chat Completions 文本连接器，尚未接到分析流程。舞弊或异常分析、检索、报告、FastAPI、LangGraph 编排和前端仍未实现。

### 安装、调用与测试

运行依赖是 PyMuPDF，声明在 `backend/pyproject.toml`。pytest 是可选测试依赖。在仓库根目录执行：

```powershell
python -m pip install -e .\backend
python -m pip install pytest
python -m pytest tests/unit
```

未做可编辑安装时，`tests/unit/conftest.py` 会把 `backend/src` 加入导入路径。当前环境已能导入 `pymupdf` 和 `pytest` 时，可直接在仓库根目录运行 `python -m pytest tests/unit`。

```python
from pathlib import Path
from finagent.ingestion import parse_text_pdf

parsed = parse_text_pdf(Path("data/raw/example.pdf"), document_id="example-001")
Path("data/processed/example-001.json").write_text(parsed.to_json(), encoding="utf-8")
```

上面的路径只说明调用方式。不要把整份解析 JSON 打印到终端。单元测试在临时目录里生成小型 PDF。公开年报原文的导入位置见下文，不在 `data/samples/`。

已有 `text_pdf.json` 时，可用可复用命令提取四项事实并计算同比。`--source-pdf` 会核对原始文件 SHA256；不一致就停止提取。运行目录必须新建，不能放在 `data/raw/`。

```powershell
python scripts/extract_annual_facts.py `
  --parsed data/processed/603288/2024/cninfo-1222994233/text_pdf.json `
  --company-id 603288 `
  --report-year 2024 `
  --run-root artifacts/runs `
  --source-pdf data/raw/603288/2024/cninfo-1222994233/1222994233.PDF
```

### 第三方依赖

| 项目 | 记录 |
| --- | --- |
| 软件 | PyMuPDF |
| Python 包 | `pymupdf`，声明于 `backend/pyproject.toml`，版本下限 `pymupdf>=1.24` |
| 来源 | PyPI [pymupdf](https://pypi.org/project/pymupdf/)，上游仓库 [pymupdf/PyMuPDF](https://github.com/pymupdf/PyMuPDF)，产品页 [pymupdf.io](https://pymupdf.io/) |
| 本次测试版本 | 运行单元测试时环境中的 `pymupdf.version` 为 `('1.27.2.3', '1.27.2', None)`。下标 0 是 PyMuPDF 1.27.2.3，写入 `extractor_version`；下标 1 是所绑定的 MuPDF 1.27.2 |
| 用途 | 从文本型 PDF 提取文字块和页面坐标，并按块类型计数图片。不执行 OCR，不把图片字节放进解析结果 |
| 许可 | 官方许可页 [pymupdf.io/licensing](https://pymupdf.io/licensing) 写明 AGPLv3 或商业许可。这里只记录该页表述 |

### 限制

- 只解析可选中文字的文本型 PDF，不处理扫描件 OCR。
- 文字来自 PyMuPDF 文本块。块内各行以换行拼接；较大视觉间隙可能被插入空格；不把行末连字符重新拼成词。
- 文字块坐标在未旋转页面上。页宽和页高来自旋转后的 `page.rect`，旋转 90 或 270 度时两者不能混用。
- 只记录 PDF 页序号，不识别印刷页码。
- 整份文件会读入内存。
- 没有 HTTP 服务或智能体编排入口。

## 公开样例

已导入一份真实公开年报，用于文本解析、四项年度事实和同比验收。它不是人工构造数据，也不在 `data/samples/`。原始 PDF、解析 JSON 和运行摘要默认不纳入 Git。

| 项目 | 内容 |
| --- | --- |
| 公司 | 佛山市海天调味食品股份有限公司，股票代码 603288 |
| 报告 | 2024 年年度报告，报告期 2024-12-31，披露日期 2025-04-03 |
| 来源 | https://static.cninfo.com.cn/finalpage/2025-04-03/1222994233.PDF |
| 原文件名 | 1222994233.PDF |
| 本地原文 | `data/raw/603288/2024/cninfo-1222994233/1222994233.PDF` |
| 取得时间 | 2026-09-23T10:25:30+08:00 |
| SHA256 | `5a97b13534438f5e85249752ef492fbd9e23af73e67845e47bad1ab7d92e20ee` |
| 大小 | 4,561,223 字节 |
| 使用条件 | 公开披露，仅本地开发使用，未确认再分发许可 |
| 解析 JSON | `data/processed/603288/2024/cninfo-1222994233/text_pdf.json` |
| 处理说明 | `data/processed/603288/2024/cninfo-1222994233/processing.md` |
| 验收摘要 | `artifacts/runs/pdf-parse-603288-2024annual-20260923-102530/summary.json` |

`parse_text_pdf` 使用 PyMuPDF 1.27.2.3。PDF 共 208 页，与预期一致；208 页都有可提取文字，无文字页为 0。解析结果里的 `source_sha256` 与原文件一致，解析没有改动原 PDF。

定位检查均通过：第 81 页有“合并利润表”，第 76–86 页窗口内第 81 与 83 页有“营业收入”；第 85 页有“合并现金流量表”；第 7–9 页有“非经常性损益”。全篇没有 U+FFFD 或 `(cid:)`。第 81 页和第 85 页各有 1 个图片块，未做 OCR。这两页的文字块多数内部含换行，表格没有拆成单元格。

正式运行是 `annual-facts-603288-2024-20260923-112307`。原始 PDF 哈希与 `source_sha256` 一致。2024 年金额是当年列，2023 年金额是这份 2024 年报的比较列；是否追溯调整尚未确认，`restatement_status` 为 `unknown`。营业收入没有“一、营业收入”主行，改用“其中：营业收入”。非经常性损益合计的口径是披露表格口径。单位为元。较早的 `annual-facts-603288-2024-20260923-111838` 仍保留。

| 指标 | 2024 | 2023 | 差额 | 约同比 |
| --- | --- | --- | --- | --- |
| 营业收入 | 26,900,977,516.70 | 24,559,312,356.59 | 2,341,665,160.11 | 9.53% |
| 归属于母公司股东的净利润 | 6,344,125,969.00 | 5,626,626,091.97 | 717,499,877.03 | 12.75% |
| 经营活动产生的现金流量净额 | 6,843,710,887.07 | 7,355,650,997.74 | -511,940,110.67 | -6.96% |
| 披露的非经常性损益合计 | 274,709,462.33 | 231,962,157.80 | 42,747,304.53 | 18.43% |

约同比由 Decimal 同比率按当前计算精度换成两位百分比，是近似值。差额和原文金额的完整十进制仍在该运行产物和 `processing.md`。这不是独立原文核验。

### 当前限制

- 只覆盖上述四个指标，不承诺其他报表项目或全部上市公司版式。
- 年度表头、单位、币种或口径缺失、矛盾、主行重复时弃权，不猜数值，也不把空单元格当零。
- 上期规范值为零时不计算同比率。
- 非经常性损益若表题未写合并，只记披露表格口径。
- 同比只比较同一文档哈希、公司、币种、单位倍率、口径和年度期间类型。
- 2023 年金额来自 2024 年报比较列。比较列是否经过追溯调整尚未确认。
- 同比率是当前 Decimal 除法精度下的近似小数；差额不依赖该近似值。

## 后续开发顺序

本项目的软件开发工作聚焦可运行系统、数据、核验和复现说明。初赛计划书 PDF 和项目介绍视频 MP4 不在本开发任务内。`submission/` 仍保留，用于存放比赛材料；比赛对计划书和视频的客观要求不变。本文不指定这些材料的完成人，已有比赛资料保持原样。

1. 文本型 PDF 的逐页文字与坐标已实现，并已对上述公开年报做过定位验收。
2. 四项年度事实和确定性同比已实现。独立原文核验、其他指标和完整财务分析仍未实现。
3. 接入模型、任务编排和调用记录，生成带证据的风险分析结果。
4. 实现后端接口与本地 Web 展示，贯通上传、执行、进度和结果浏览。
5. 建立对照评测，并整理可复现说明。

当前可运行增量是文本型 PDF 解析、四项年度事实提取、确定性同比、Chat Completions 文本连接器，以及 `tests/unit` 中的对应测试。连接器尚未做云端实测，也未接到智能体接口。HTTP 路由、独立原文核验和舞弊分析仍未实现。
