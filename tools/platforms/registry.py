"""平台适配器注册表。"""
from __future__ import annotations
import importlib
from pathlib import Path
from typing import List, Optional

from .base import PlatformAdapter, ClipResult

# 自动发现并注册所有平台适配器
_platforms: List[PlatformAdapter] = []
_loaded = False


def _load_all():
    global _loaded
    if _loaded:
        return

    adapters_dir = Path(__file__).parent
    for f in sorted(adapters_dir.glob("*.py")):
        if f.stem.startswith("_") or f.stem in ("base", "registry"):
            continue
        mod = importlib.import_module(f".{f.stem}", package="platforms")
        # 找所有继承自 PlatformAdapter 且不是基类的类
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, PlatformAdapter)
                and attr is not PlatformAdapter
                and getattr(attr, "name", "base") != "base"
            ):
                _platforms.append(attr)

    # 按优先级排序
    _platforms.sort(key=lambda p: getattr(p, "priority", 100))
    _loaded = True


def get_platform(url: str) -> Optional[PlatformAdapter]:
    """找到匹配 URL 的平台适配器。"""
    _load_all()
    for p in _platforms:
        if p.match(url):
            return p
    return None


def extract_url(url: str) -> ClipResult:
    """自动匹配合适的平台并抓取。"""
    platform = get_platform(url)
    if platform is None:
        raise ValueError(f"未找到匹配的平台适配器: {url}")
    return platform.extract(url)


def list_platforms() -> List[str]:
    """列出所有已注册的平台。"""
    _load_all()
    return [p.name for p in _platforms]
