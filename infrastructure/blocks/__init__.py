"""块级磁盘仓库与紧凑特征视图（方案 L01 两遍处理的第一遍产物）。"""
from infrastructure.blocks.store import (
    FEATURE_HEAD_CHARS,
    FEATURE_TAIL_CHARS,
    FEATURE_METADATA_KEYS,
    BlockFeature,
    BlockStore,
    FeatureView,
    OwnedBlocks,
    compact_block,
    feature_of,
)

__all__ = [
    "FEATURE_HEAD_CHARS",
    "FEATURE_TAIL_CHARS",
    "FEATURE_METADATA_KEYS",
    "BlockFeature",
    "BlockStore",
    "FeatureView",
    "OwnedBlocks",
    "compact_block",
    "feature_of",
]
