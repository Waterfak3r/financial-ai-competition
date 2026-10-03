# 季度分析原创模拟样例

本目录由 `scripts/prepare_quarterly_samples.py` 生成。PDF 封面及每个财务表页均内含原创性质、用途及口径声明；公司名和代码均为虚构标识，不对应真实发行人。

PDF 使用 PyMuPDF 内建中文字体资源 `china-s` 绘制；没有另行下载或嵌入外部字体文件。

样例覆盖 2023/2024 Q1 与 H1 累计值，可推导两个年度 Q2，并展示同比与环比。2024Q1 经营现金流为负，用来演示保留差额且不计算增率。

来源、SHA256、期间和使用条件见 [source_manifest.json](source_manifest.json)。重新生成会覆盖本目录中的同名原创样例文件。
