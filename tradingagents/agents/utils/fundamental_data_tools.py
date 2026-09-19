from langchain_core.tools import tool
from typing import Annotated
from tradingagents.dataflows.interface import route_to_vendor


@tool
def get_fundamentals(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """
    Retrieve comprehensive fundamental data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing comprehensive fundamental data
    """
    return route_to_vendor("get_fundamentals", ticker, curr_date)


@tool
def get_balance_sheet(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name"],
    curr_date: Annotated[str, "Analysis date in YYYY-MM-DD format (required)"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
) -> str:
    """
    Retrieve balance sheet data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Analysis date, yyyy-mm-dd. **Required** — the data layer
            uses it to drop report periods published after that date, so a
            missing/empty value silently re-admits future filings.
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
    Returns:
        str: A formatted report containing balance sheet data
    """
    return route_to_vendor("get_balance_sheet", ticker, freq, curr_date)


@tool
def get_cashflow(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name"],
    curr_date: Annotated[str, "Analysis date in YYYY-MM-DD format (required)"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
) -> str:
    """
    Retrieve cash flow statement data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Analysis date, yyyy-mm-dd (required, see get_balance_sheet)
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
    Returns:
        str: A formatted report containing cash flow statement data
    """
    return route_to_vendor("get_cashflow", ticker, freq, curr_date)


@tool
def get_income_statement(
    ticker: Annotated[str, "6-digit A-stock code (e.g. 600379). Must be numeric, NOT company name"],
    curr_date: Annotated[str, "Analysis date in YYYY-MM-DD format (required)"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
) -> str:
    """
    Retrieve income statement data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Analysis date, yyyy-mm-dd (required, see get_balance_sheet)
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
    Returns:
        str: A formatted report containing income statement data
    """
    return route_to_vendor("get_income_statement", ticker, freq, curr_date)