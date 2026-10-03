# 季度分析切片

季度分析从多份文本 PDF 与来源清单读取原始累计数，分别核验来源、季度派生、期间可比性和最终比较。当前切片只覆盖营业收入、归母净利润和经营活动产生的现金流量净额；它不改变年度 `FinancialFactV2` 契约，也不把异常比较解释为舞弊结论。

## 期间和计算规则

流量指标的季度值从相邻累计期间推导：Q1 使用一季度累计值，Q2 为 H1 减 Q1，Q3 为 9M 减 H1，Q4 为 FY 减 9M。资产负债表时点值不做差分。跨年 Q1 的环比对象是上一年 Q4；同比对象是上一年同季。每个原始 observation 保留公司、报告与数据期间、指标、来源文档及 SHA256、报表、合并/母公司口径、列角色、币种、单位倍率，以及 PDF 页码和行列证据。

计算前必须确认公司、指标、期间相邻性、币种、单位与正倍率、报表类型及明确口径一致。缺少前序累计值、追溯调整/可比性依据或唯一原文定位时失败关闭；不把两个未知口径视为相等。比较基数为正时计算增率；基数为零或负时仅在差额仍有依据的情况下保留差额并弃权增率。材料不足与原文冲突分别标记为 `insufficient_evidence` 和 `conflict`。

同比可用本期报告明确披露的上年同期比较列，并由核验器重新打开 PDF 定位比较值。环比涉及两个报告的累计列；只有来源对相关指标和期间提供明确的未追溯调整/更正证据时，才签发可比性 proof。单纯没有检索到“追溯”字样不构成未调整证明。

每层核验器独立重新定位或重算。季度 proof 除包含可审计序列化内容外，还绑定当前 Python 进程签发的对象身份和规范目标内容摘要；从 JSON 重建、手造 proof 或修改目标后复用旧 proof 均不能获得授权。proof 归档供审计，不是可移植授权凭据。

## 模块和 CLI

季度模块使用英文文件名，位于现有领域分层内：

- `backend/src/finagent/schemas/quarterly.py`：累计观察、派生季度、比较和 proof 类型。
- `backend/src/finagent/ingestion/quarterly.py`：清单路径边界、PDF 文本和表格单元格候选提取。
- `backend/src/finagent/finance/quarterly.py`：季度差分和比较值计算。
- `backend/src/finagent/verification/quarterly.py`：原文重定位、独立派生/比较重算和当前进程 proof。
- `backend/src/finagent/reports/quarterly_analysis.py`：审计 Markdown 渲染。
- `scripts/analyze_quarterly.py`：季度独立命令行入口。

在仓库根目录运行原创样例的 Q2 同比和环比：

```powershell
python scripts/analyze_quarterly.py --year 2024 --quarter Q2
```

入口也接受 `--manifest` 指定 `data/` 下的来源清单，以及 `--relations` 选择 `year_over_year`、`quarter_over_quarter`。每次执行生成新的 `quarterly-analysis-<UUID>`，结构化审计记录位于 `artifacts/runs/<run_id>/analysis.json`，可读报告位于 `artifacts/reports/<run_id>/quarterly-analysis.md`。结果归档记录来源、派生和比较三层状态、限制、哈希与代码指纹。季度分析器不能读取隔离的 `evaluation/` 答案。

## 原创模拟材料与真实资料

`data/samples/quarterly/` 中的 PDF 由 `scripts/prepare_quarterly_samples.py` 在本地生成；样例目录内的清单使用仓库相对路径。生成器使用 PyMuPDF 内建中文字体资源 `china-s`，没有另行下载或嵌入外部字体文件。封面和每张财务表页均声明其原创模拟性质、分发用途和会计口径。可在干净源码目录中运行该准备脚本复现样例；它只维护这个专属的原创模拟目录。

真实公开报告须单独登记其来源 URL、下载日期、文档日期、文件 SHA256、具体用途和许可条件。许可未确认的报告只保留在本地 `data/raw/`，不得随提交件或演示包分发 PDF 或原文片段；标准化处理结果写入 `data/processed/`。真实材料若没有足够的追溯或可比性证据，报告应说明缺少的依据并弃权，不以模拟样例结果替代真人案例验证。
