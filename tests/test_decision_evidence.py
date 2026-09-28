"""决策节点的证据可见性与截断口径（0.5.38）。

对应审查命中：
- **Portfolio Manager（终裁）**此前只看到 RM 计划 / Trader 方案 / 数据质量门控 / 风控辩论史，
  7 份分析师**原始报告从未进入"给最终评级"这一步** —— 而提示词却要求
  "ground every conclusion in specific evidence from the analysts"；
- **Trader** 的截断上限散着 1500 / 2000 / 1200 三个硬编码数字，且截断标注不说是哪一份报告。

本文件守住两条不变量：① 终裁能看到原始报告（或显式说明没有）；② 截断一律带上限与来源标注，
且上限可集中配置（`evidence_clip_chars`）。
"""
import pytest

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.trader.trader import create_trader
from tradingagents.agents.utils.prompt_clip import clip_evidence, evidence_clip_limit


class _CapturingLLM:
    """不支持结构化输出（→ 走自由文本通道）并记下每次 prompt。"""

    def __init__(self, content="**Rating**: Hold\n\n理由。"):
        self.content = content
        self.prompts = []

    def with_structured_output(self, schema, **kw):
        raise NotImplementedError("test double: structured output unsupported")

    def invoke(self, prompt):
        self.prompts.append(prompt)

        class _Resp:
            pass

        resp = _Resp()
        resp.content = self.content
        return resp


def _flatten(prompts) -> str:
    """把 str / message-list 形态的 prompt 统一成可断言的文本。"""
    parts = []
    for p in prompts:
        if isinstance(p, str):
            parts.append(p)
        elif isinstance(p, list):
            for m in p:
                parts.append(str(m.get("content", "") if isinstance(m, dict) else m))
        else:
            parts.append(str(p))
    return "\n".join(parts)


def _pm_state(**overrides):
    base = {
        "company_of_interest": "600519",
        "risk_debate_state": {
            "history": "风控辩论…", "aggressive_history": "", "conservative_history": "",
            "neutral_history": "", "latest_speaker": "", "current_aggressive_response": "",
            "current_conservative_response": "", "current_neutral_response": "", "count": 3,
        },
        "investment_plan": "研究经理计划",
        "trader_investment_plan": "交易员方案",
        "data_quality_summary": "门控结论",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# A1：终裁能看到分析师原始报告
# ---------------------------------------------------------------------------


def test_pm_prompt_includes_raw_analyst_reports():
    """终裁必须能看到**原始报告**，而不是只看"摘要的摘要"。"""
    llm = _CapturingLLM()
    create_portfolio_manager(llm)(_pm_state(
        market_report="技术面：MA20 上方运行，量能温和放大",
        fundamentals_report="基本面：PE 12、经营现金流为正",
    ))

    prompt = llm.prompts[0]
    assert "**Analyst Reports**" in prompt
    assert "技术面：MA20 上方运行，量能温和放大" in prompt
    assert "基本面：PE 12、经营现金流为正" in prompt
    assert "### Market / Technical Report" in prompt


def test_pm_prompt_says_so_when_no_reports():
    """没有任何报告时必须显式说明，不能留空白让人误以为"没什么可看"。"""
    llm = _CapturingLLM()
    create_portfolio_manager(llm)(_pm_state())

    assert "本次无分析师报告" in llm.prompts[0]


def test_pm_prompt_clips_long_reports_and_names_the_source():
    llm = _CapturingLLM()
    create_portfolio_manager(llm)(_pm_state(market_report="技术。" * 3000))

    prompt = llm.prompts[0]
    assert "已截断至" in prompt
    assert "Market / Technical Report" in prompt


def test_pm_prompt_keeps_quality_gate_and_debate():
    """回归：原有上下文（门控结论 / 风控辩论史 / 计划）不得因新增段落而丢失。"""
    llm = _CapturingLLM()
    create_portfolio_manager(llm)(_pm_state())

    prompt = llm.prompts[0]
    for expected in ("门控结论", "风控辩论…", "研究经理计划", "交易员方案"):
        assert expected in prompt


# ---------------------------------------------------------------------------
# A2：Trader 证据截断走统一口径
# ---------------------------------------------------------------------------


def test_trader_evidence_is_clipped_with_named_source():
    llm = _CapturingLLM(content="决策：Hold")
    # 节点由 `functools.partial(trader_node, name=...)` 返回 → 只传 state
    create_trader(llm)(
        {
            "company_of_interest": "600519",
            "investment_plan": "计划",
            "market_report": "技术。" * 3000,          # 远超上限
            "investment_debate_state": {"history": "辩论" * 2000},
            "data_quality_summary": "门控",
        }
    )

    text = _flatten(llm.prompts)
    assert "已截断至" in text
    assert "Market / Technical Report" in text, "截断标注必须点明是哪一份报告"
    assert "Bull/Bear Research Debate" in text


# ---------------------------------------------------------------------------
# clip_evidence 本身
# ---------------------------------------------------------------------------


def test_clip_evidence_short_text_untouched():
    assert clip_evidence("短文本") == "短文本"
    assert clip_evidence("") == ""


def test_clip_evidence_default_limit_and_source():
    limit = evidence_clip_limit()
    assert limit > 0, "上限必须为正（非法配置应回落默认）"

    clipped = clip_evidence("字" * (limit + 50), source="X Report")
    assert f"已截断至 {limit} 字" in clipped
    assert "X Report" in clipped
    assert clipped.startswith("字" * limit)


def test_clip_evidence_explicit_limit_wins():
    """辩论史 / 门控结论用各自尺度，显式 limit 必须优先。"""
    clipped = clip_evidence("字" * 300, source="Y", limit=100)

    assert clipped.startswith("字" * 100)
    assert "已截断至 100 字" in clipped


def test_clip_limit_reads_config(monkeypatch):
    from tradingagents.dataflows import config as df_config

    monkeypatch.setattr(df_config, "get_config", lambda: {"evidence_clip_chars": 42})

    assert evidence_clip_limit() == 42
    assert "已截断至 42 字" in clip_evidence("字" * 100)


@pytest.mark.parametrize("bad", ["abc", None, 0, -5])
def test_clip_limit_falls_back_on_bad_config(monkeypatch, bad):
    """配置非法/非正不得让决策链挂掉，也不得退化成"不截断"。"""
    from tradingagents.dataflows import config as df_config

    monkeypatch.setattr(df_config, "get_config", lambda: {"evidence_clip_chars": bad})

    assert evidence_clip_limit() > 0
