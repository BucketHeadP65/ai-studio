"""The thinking levels a model is built at, with no dependency beyond the standard library."""

from typing import Literal

Thinking = Literal["low", "medium", "high", "xhigh"]
"""The thinking levels a model is built at, sent with every request as pydantic-ai's `thinking` setting.

Each route maps a level onto its own scale. A Responses API model reads it as its reasoning effort, `xhigh` being the top.
"""
