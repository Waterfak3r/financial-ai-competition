"""文本型 PDF 解析的输入与格式错误。"""


class PdfParseError(Exception):
    """解析失败的基类。"""


class PdfInputError(PdfParseError):
    """路径或文档标识不能接受。"""


class PdfFormatError(PdfParseError):
    """文件不是本增量可以读取的 PDF。"""
