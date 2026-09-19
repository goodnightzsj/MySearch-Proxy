"""研究渲染层：将 research 报告组装与渲染逻辑从 clients.py 中抽出。

Phase 1 提取的是**纯数据转换函数** —— 输入为 dict/list，输出为 dict/str，
不依赖任何 provider 调用、网络请求或 self 状态。
"""
from __future__ import annotations

from mysearch.research.render import render_research_report

__all__ = ["render_research_report"]
