# 年度原创模拟样例

受控年度演示材料位于 [`data/samples/annual/`](../data/samples/annual/)。当前样例为 `sim-annual-001-2025.pdf`，由 [`scripts/prepare_annual_samples.py`](../scripts/prepare_annual_samples.py) 1.2.0 生成。公司、公司 ID、金额、期间和经营叙述均为虚构。PDF 每页均标明“原创模拟｜非真实披露｜不可作为舞弊标签”；完整的分发范围及使用限制见样例目录的 [`README.md`](../data/samples/annual/README.md) 与 [`useconditions.md`](../data/samples/annual/useconditions.md)。

该样例可随 FINTRACE 比赛提交件和团队演示包分发、展示及用于本地功能演示。分发时保留 PDF 中的模拟提示，并附带使用条件和 SHA256。不能将它作为现实公司事实、投资依据、舞弊/违规或无舞弊标签、模型训练标签。

## 来源性质识别

后端的 `finagent.core.sample_provenance` 只按受控完整 PDF SHA256、公司 ID 和报告年度匹配模拟登记。Web 上传生成的新 `upload-<uuid>` 文档 ID 是归档身份，不是 PDF 自带信息，也不阻断相同已登记字节的性质识别；归档读取器仍校验 `run_id`、`document_id`、公司、年度和 PDF 哈希的绑定。若 SHA 或公司/年度不匹配，来源性质显示为“来源性质未确认”，不会从文件名、用户上传标签或模型输出推断。当前登记摘要与生成器的机器可读记录见 [`source_manifest.json`](../data/samples/annual/source_manifest.json)。

样例包含年度和比较期的收入、归母净利润、经营现金流量净额及资产负债表数据；非经常性损益合计的披露范围明确为未单独确认。原文陈述相关会计政策变更、会计估计变更、前期差错更正及追溯调整均不适用，并对期初未分配利润影响作出完整声明，以供现有比较核验按其支持范围工作。样例设计了利润上升、经营现金流下降的方向差异，同时明确没有确认差异成因或因果关系。

## 字体与重建

当前 PDF 实测使用本机 Windows 字体 **Microsoft YaHei Regular 6.31**，源文件 SHA256 为 `d79c55e68b1131eea0cc1c47be4f572d964f28c682e143db2ad09c1e4cb07a3f`。生成器不会复制或单独分发字体；PDF 中字体嵌入须符合随附 Windows 产品许可。字体元数据、许可来源、OpenType 位说明及历史生成版本见样例 `README.md` 与 `source_manifest.json`。实测 OS/2 `fsType=0x0008` 表示 Editable embedding，`0x0100`（No subsetting）位未设置，不据此推定禁止子集化。

在具备匹配字体且已安装 PyMuPDF 的环境中，可运行：

```powershell
python scripts/prepare_annual_samples.py
```

脚本只写入 `data/samples/annual/`；已有不同内容时拒绝覆盖。运行依赖 SHA 固定的字体文件，因此不能保证在缺少相同 Windows 字体的系统上重建。历史 Noto Sans SC 试验版在本任务的当前 PyMuPDF 管线实测未能抽取关键中文财务事实；该结果只描述该样例版本与本机管线，不推广到其他 Noto PDF 或 PyMuPDF 版本。
