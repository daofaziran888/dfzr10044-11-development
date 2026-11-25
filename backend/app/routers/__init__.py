"""业务路由汇总：通用资源路由 + 结构版本化质量校验专用路由。"""
from __future__ import annotations

from app.routers import quality_versioning, resource

ROUTERS = [quality_versioning, resource]
