# R4 年报语义集成样例

`issue.pdf` 与 `clean.pdf` 是 FINTRACE 团队为离线软件集成测试原创构造的受控材料。公司、合同、金额、期间、披露内容及线索均为虚构；两份文件都不是现实公司的财报或财务事实，也不是舞弊、违规或“无舞弊”标签。它们用于验证流程边界，不是 golden answers、模型质量基准或人工核验结论。

文件身份：

- `issue.pdf`：SHA256 `c31b9e59ec2a31c0b9afce81be231466b6783213fda80ecf2501221365f265e9`
- `clean.pdf`：SHA256 `256edec135d12906cd862ff9c0aba972d8bf997ea422dc174724f894b5abaa68`

两份 PDF 从已接受的原创输入逐字节复制而来：[原始验收目录](../../../artifacts/evaluations/annual-recovery-r4-20261009/semantic-pressure-20261009T023745Z-d73354bd/)。生成器是一次性本地脚本 [`build_semantic_pressure_20261009T023745Z-d73354bd.py`](../../../artifacts/tmp/recovery-r4-semantic-pressure-20261009/build_semantic_pressure_20261009T023745Z-d73354bd.py)，SHA256 `8d37cfcb67f085532fc6eb008ab3bf9be57aa29cbd59a020e097576b13144863`。该脚本只从头生成模拟文本，不使用真实公司材料、外部服务或模型配置；正式集成测试只依赖本目录的两份 PDF，不依赖该临时生成器或原验收目录。

样例仅供本机离线测试和软件边界演示。不得将其中内容或模型对其生成的线索作为现实财务判断、投资依据、舞弊/违规结论、无舞弊结论或模型训练标签。
