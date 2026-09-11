"""Semantic layer implementations behind the SemanticLayer protocol.

`make_semantic_layer` picks the backend from the tenant's config type. The
where and grain translation both backends share lives in `where.py`.
"""

from __future__ import annotations

from pathlib import Path

from understory.protocols import SemanticLayer
from understory.tenant import DbtCloudConfig, MetricFlowLocalConfig

__all__ = ["make_semantic_layer"]


def make_semantic_layer(
    config: MetricFlowLocalConfig | DbtCloudConfig, manifest_path: Path
) -> SemanticLayer:
    if isinstance(config, MetricFlowLocalConfig):
        from understory.semantic.metricflow_local import MetricFlowLocal

        return MetricFlowLocal(config, Path(manifest_path))
    if isinstance(config, DbtCloudConfig):
        from understory.semantic.dbt_cloud import DbtCloud

        return DbtCloud(config, Path(manifest_path))
    raise TypeError(f"unknown semantic layer config: {type(config).__name__}")
