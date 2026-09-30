"""Regression tests for PDF export."""

from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="web extra not installed")

from fpdf.enums import WrapMode

from web.pdf_export import (
    PDFExportError,
    _ReportPDF,
    _compact_inline_text,
    _discover_cjk_fonts,
    _find_cjk_fonts,
    _format_table_cells,
    _missing_data_warning,
    generate_markdown,
    generate_pdf,
)


def test_generate_pdf_with_chinese_markdown_when_cjk_font_available():
    try:
        _find_cjk_fonts()
    except PDFExportError as exc:
        pytest.skip(str(exc))

    state = {
        "market_report": "# 技术分析\n- 趋势: 偏强\n- 结论: 中文字体测试通过。",
        "news_report": "| 项目 | 结论 |\n|---|---|\n| 中文 | 可嵌入 |",
        "final_trade_decision": "最终建议: HOLD 观望。",
    }

    pdf_bytes = generate_pdf(state, "600519", "2026-05-30", "HOLD")

    assert pdf_bytes.startswith(b"%PDF-")
    assert len(pdf_bytes) > 1000


def test_pdf_text_helpers_compact_alignment_whitespace():
    assert _compact_inline_text("Neutral        Analyst:        我会把") == "Neutral Analyst: 我会把"
    assert _format_table_cells(["表面看是放量反弹", "但对连续跌停后的", "ST"]) == "表面看是放量反弹 | 但对连续跌停后的 | ST"


def test_pdf_multicell_defaults_to_left_alignment():
    pdf = object.__new__(_ReportPDF)
    calls = []

    pdf.l_margin = 10
    pdf.r_margin = 10
    pdf.w = 210
    pdf.set_x = lambda x: None
    pdf.multi_cell = lambda *args, **kwargs: calls.append((args, kwargs))

    pdf._write_multicell(5.5, "近 5 日均量明显高于近 20 日均量。")

    assert calls[0][1]["align"] == "L"
    assert calls[0][1]["wrapmode"] == WrapMode.CHAR


def test_wqy_discovery_reuses_same_font_for_bold(monkeypatch, tmp_path):
    wqy = tmp_path / "wqy-microhei.ttc"
    noto_bold = tmp_path / "NotoSansCJK-Bold.ttc"
    wqy.write_bytes(b"font")
    noto_bold.write_bytes(b"font")

    def fake_find_font_file(pattern: str) -> Path | None:
        return {
            "wqy-microhei.ttc": wqy,
            "NotoSansCJK-Bold.ttc": noto_bold,
        }.get(pattern)

    monkeypatch.setattr("web.pdf_export._find_font_file", fake_find_font_file)

    assert _discover_cjk_fonts() == (wqy, wqy)


# ---------------------------------------------------------------------------
# 数据不完整警告（T7 / B4，源仓库 ac4de23）
# ---------------------------------------------------------------------------


def test_missing_data_warning_reports_active_gap_count():
    """有活跃缺口时必须报数；`status` 缺省视为 active（与 missing_data 索引一致）。"""
    state = {
        "missing_data_tasks": [
            {"status": "active"},
            {"status": "resolved"},
            {"id": "no-status-means-active"},
        ]
    }

    msg = _missing_data_warning(state)

    assert msg is not None
    assert "2 个取数缺口" in msg


def test_missing_data_warning_after_backfill_asks_reanalysis():
    """缺口已补齐但没重跑时，文案要说明「仍基于补数前的分析结果」。"""
    msg = _missing_data_warning(
        {"missing_data_tasks": [{"status": "resolved"}],
         "missing_data_requires_reanalysis": True}
    )

    assert msg is not None
    assert "重新分析" in msg


def test_missing_data_warning_is_silent_when_complete_or_absent():
    """阴性对照：无缺口 / 缺键 / 非法类型都不得报警，也不得抛错。"""
    assert _missing_data_warning({}) is None
    assert _missing_data_warning({"missing_data_tasks": []}) is None
    assert _missing_data_warning({"missing_data_tasks": [{"status": "resolved"}]}) is None
    # 旧引擎/坏状态：类型不对时按「无缺口」处理（宁可少报，不可崩在渲染层）
    assert _missing_data_warning({"missing_data_tasks": "oops"}) is None
    assert _missing_data_warning({"missing_data_tasks": None}) is None


def test_markdown_export_includes_missing_data_warning():
    """Markdown 是 PDF 字体缺失时的兜底交付物，同样必须带警告。"""
    md = generate_markdown(
        {"missing_data_tasks": [{"status": "active"}], "market_report": "x"},
        "600519", "2026-09-30", "HOLD",
    )

    assert "数据不完整" in md and "1 个取数缺口" in md
