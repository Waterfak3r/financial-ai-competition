from __future__ import annotations

from typing import Any

from finagent.api.narrative_reading_context import build_reading_context


def _block(x0: float, y0: float, text: str, *, x1: float = 506.0) -> tuple[Any, ...]:
    return (x0, y0, x1, y0 + 11.6, text, 0, 0)


def test_context_restores_chinese_word_wrap_and_stops_at_indented_paragraph() -> None:
    blocks = [
        _block(84.85, 600.19, "2024 年是公司四五规划的开局首年，是承压破局的一年。面对外部环境的不确定性，公司坚\n"),
        _block(63.83, 618.53, "守产品品质优先，坚持刀刃向内的变革，以用户至上的价值导向牵引各业务快速转型。变革有效\n"),
        _block(63.83, 636.21, "激发了组织与人员活力，使部分工作的成效在2024 年的体感更实，也让我们对企业“长期主义发\n"),
        _block(63.83, 654.24, "展”的决心更加坚定。2024 年，公司营业收入及归属上市公司股东净利润双增长。 \n", x1=441.92),
        _block(84.85, 672.58, "我们转向以用户需求为导向的全链条服务革新。持续的摸索、实践及反思，让我们对用户需求有了更多理解。"),
    ]

    context = build_reading_context(
        blocks,
        3,
        pdf_page=9,
        page_width=595.0,
        source_sha256="a" * 64,
    )

    assert context["text"] == (
        "2024 年是公司四五规划的开局首年，是承压破局的一年。面对外部环境的不确定性，公司坚守产品品质优先，"
        "坚持刀刃向内的变革，以用户至上的价值导向牵引各业务快速转型。变革有效激发了组织与人员活力，使部分工作"
        "的成效在2024 年的体感更实，也让我们对企业“长期主义发展”的决心更加坚定。2024 年，公司营业收入及归属"
        "上市公司股东净利润双增长。"
    )
    assert context["expanded"] is True
    assert context["limited"] is False
    assert [item["block_index"] for item in context["source_blocks"]] == [0, 1, 2, 3]
    assert context["bbox"] == {"x0": 63.83, "y0": 600.19, "x1": 506.0, "y1": 665.84}


def test_context_does_not_join_numeric_table_rows_or_another_column() -> None:
    citation = _block(63.83, 654.24, "公司披露了营业收入的相关变化情况，具体内容请结合年报上下文核对。", x1=441.92)
    table_row = _block(63.83, 636.21, "营业收入 100,000 净利润 20,000 同比 18.2%", x1=441.92)
    table_context = build_reading_context(
        [table_row, citation], 1, pdf_page=9, page_width=595.0, source_sha256="b" * 64
    )
    assert table_context["text"] == citation[4].strip()
    assert table_context["expanded"] is False
    assert table_context["limited"] is True
    assert table_context["limit_reason"] == "unsafe_neighbor"

    other_column = _block(320.0, 636.21, "另一栏记载了完全不同的一段正文内容，不能与当前段落合并。", x1=520.0)
    column_context = build_reading_context(
        [other_column, citation], 1, pdf_page=9, page_width=595.0, source_sha256="c" * 64
    )
    assert column_context["text"] == citation[4].strip()
    assert column_context["source_blocks"][0]["block_index"] == 1
    assert column_context["limited"] is True


def test_context_marks_block_and_raw_character_limits() -> None:
    prose = "这是一段用于检查相邻年报正文安全拼接范围的中文说明文字，其中没有句末标点"
    blocks = [_block(63.83, 100 + 18 * index, prose) for index in range(9)]
    bounded = build_reading_context(
        blocks, 4, pdf_page=1, page_width=595.0, source_sha256="d" * 64
    )
    assert len(bounded["source_blocks"]) == 5
    assert bounded["limited"] is True
    assert bounded["limit_reason"] == "block_limit"

    padded_neighbor = _block(63.83, 100, "这是一段有效的年报正文内容" + " " * 720)
    target = _block(63.83, 118, prose)
    char_bounded = build_reading_context(
        [padded_neighbor, target], 1, pdf_page=1, page_width=595.0, source_sha256="e" * 64
    )
    assert char_bounded["source_blocks"][0]["block_index"] == 1
    assert char_bounded["limited"] is True
    assert char_bounded["limit_reason"] == "character_limit"


def test_pdf_line_breaks_preserve_english_sentence_spacing() -> None:
    block = _block(
        63.83,
        100,
        "The first sentence ends here.\nSecond sentence keeps its English word spacing across a PDF line break.",
    )
    context = build_reading_context(
        [block], 0, pdf_page=1, page_width=595.0, source_sha256="f" * 64
    )
    assert context["text"] == "The first sentence ends here. Second sentence keeps its English word spacing across a PDF line break."
    assert context["expanded"] is False
    assert context["limited"] is False


def test_adjacent_english_paragraphs_do_not_join_after_a_period() -> None:
    blocks = [
        _block(63.83, 100, "The preceding paragraph describes a completed annual disclosure."),
        _block(63.83, 118, "The cited paragraph starts here and should remain separate from the previous one."),
    ]
    context = build_reading_context(
        blocks, 1, pdf_page=1, page_width=595.0, source_sha256="1" * 64
    )
    assert context["text"] == blocks[1][4]
    assert context["expanded"] is False
    assert context["limited"] is False
