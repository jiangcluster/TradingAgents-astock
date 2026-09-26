"""TradingAgents-Astock.

`__version__` 与 `pyproject.toml` / `CHANGELOG.md` / `CLAUDE.md` 四处同步，
`tests/test_version_consistency.py` 会把它们锁在一起。

下游（a-share-deep-advisor）通过 headless JSON 的 `ta_version` 字段做版本握手：
缓存若不含版本，`git pull` 升级 TA 之后同一天仍会复用旧版本的结论而无人察觉。
"""

__version__ = "0.5.34"
