"""Detect and interrupt repeated-action loops (private, not re-exported at top level)."""

from harness.vendor.loop_detection._capability import (
    LoopDetected,
    LoopDetectedError,
    LoopDetection,
    LoopTier,
    OnLoop,
)

__all__ = [
    'LoopDetected',
    'LoopDetectedError',
    'LoopDetection',
    'LoopTier',
    'OnLoop',
]
