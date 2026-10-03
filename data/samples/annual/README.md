# 年度原创模拟样例

`sim-annual-001-2025.pdf` 是 FINTRACE 团队原创构造的 7 页中文演示材料。公司“澄湾云算股份有限公司”、`SIM-ANNUAL-001` 标识、金额和叙述均为虚构；它不是现实公司的披露，也不是舞弊、违规或无舞弊标签。PDF 每页醒目标记“原创模拟、非真实披露、不可作为舞弊标签”。

模拟内容可随 FINTRACE 比赛提交件与团队演示包分发和展示。分发时保留 PDF 内的模拟标识并附带 [`useconditions.md`](useconditions.md)。不得将样例当作现实公司事实、投资依据或模型训练标签。

## 当前文件身份

- 报告年度：2025；比较年度：2024。
- 文档 ID：`sim-annual-001-2025`。
- 当前 PDF SHA256：`f193e7a497fe483d3ab70b7bde2880f715ff1127b1d8b14cc3f4fda678ba0bf7`。
- 机器可读来源登记：[`source_manifest.json`](source_manifest.json)。运行时以 PDF SHA256、公司 ID 和报告年度精确匹配受控登记；Web 上传分配的 `upload-<uuid>` 文档 ID 作为归档身份保留，不影响来源性质判定。文件名和模型输出不产生性质判断，归档读取器仍校验 run、文档 ID 与 PDF 哈希的绑定。
- 重建命令：`python scripts/prepare_annual_samples.py`。生成器只写入本目录；遇到不同的已存在输出会拒绝覆盖，不会下载或安装字体。

## 字体来源与嵌入

当前 PDF 用到本机 `C:\Windows\Fonts\msyh.ttc` 中的 **Microsoft YaHei Regular 6.31**。字体文件 SHA256 为 `d79c55e68b1131eea0cc1c47be4f572d964f28c682e143db2ad09c1e4cb07a3f`；本机字体名称表版权为 Microsoft，部分字形版权归北京方正电子有限公司，设计者为 Founder。PDF 里只嵌入文档字体，不随样例单独分发字体文件。

该字体 face 的 OS/2 `fsType=0x0008` 表示 Editable embedding；`0x0100`（No subsetting）未设置。嵌入条件仍受 Windows 产品许可约束。详见 Microsoft 的 [Microsoft YaHei 字体说明](https://learn.microsoft.com/en-us/typography/font-list/microsoft-yahei)、[Windows 字体嵌入及分发 FAQ](https://learn.microsoft.com/en-us/typography/fonts/font-faq) 和 [OpenType OS/2 `fsType` 规范](https://learn.microsoft.com/en-us/typography/opentype/spec/os2)。生成器不会复制、修改或单独分发系统字体。

## 受控线索边界

样例安排了归母净利润、营业收入上升而经营活动现金流量净额下降的受控方向差异。原文明确没有确认差异成因，也未提供足以建立因果关系的信息。该线索只演示事实抽取、独立核验、计算、引用和状态展示，不应用作现实主体判断。

## 历史生成版本

- Microsoft YaHei 预览版：SHA256 `a9131e1c660bd38ed80a1560fe5e3f3c8dd4a25c35b561019e04d6fa444f0d68`，CLI 归档 `annual-analysis-990a774a-665a-4ad7-ab17-87c5997523e6`。
- Noto Sans SC 2.04 试验版：SHA256 `ea215df01c6c37a51ccf609b2ee3977ff12adeb423e4407ab6af39fab1961440`，CLI 归档 `annual-analysis-30ce07eb-ec17-45fb-baac-f1abbe15fa4b`。在本任务当前 PyMuPDF 上，该 PDF 的文字提取会将部分中文字符映射为兼容字，原有抽取管线因此没有提取财务事实；这是本次版本的实测结果，不推广为对其他 Noto PDF 或其他版本的结论。
- 当前生成器版本：`scripts/prepare_annual_samples.py` 1.2.0。
