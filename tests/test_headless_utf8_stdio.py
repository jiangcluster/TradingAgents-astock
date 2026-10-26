"""headless 输出必须能在 Windows GBK 控制台/管道里写出去。

Windows 下 `sys.stdout` 默认 cp936；结果 JSON 里带 `⚠️` 时 `print` 抛
UnicodeEncodeError——而那时整轮分析已经跑完，结论直接丢失。`_force_utf8_stdio()`
必须在任何 print 之前把两个流切成 UTF-8。
"""

import types

from cli import headless


class _RecordingStream:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


def _fake_sys(stdout, stderr):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr)


def test_reconfigures_both_streams_to_utf8(monkeypatch):
    out, err = _RecordingStream("out"), _RecordingStream("err")
    monkeypatch.setattr(headless, "sys", _fake_sys(out, err))

    headless._force_utf8_stdio()

    assert out.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert err.calls == [{"encoding": "utf-8", "errors": "replace"}]


def test_stream_without_reconfigure_is_ignored(monkeypatch):
    """非常规流（测试替身、已关闭的流）不能把启动流程打断。"""
    monkeypatch.setattr(headless, "sys", _fake_sys(object(), object()))

    headless._force_utf8_stdio()  # 不抛即通过


def test_reconfigure_failure_is_swallowed(monkeypatch):
    class _Broken:
        def reconfigure(self, **kwargs):
            raise OSError("stream closed")

    monkeypatch.setattr(headless, "sys", _fake_sys(_Broken(), _Broken()))

    headless._force_utf8_stdio()  # 不抛即通过
