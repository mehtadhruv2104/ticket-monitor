"""Load, save, and match plugins from the plugins/ directory."""

import importlib.util
import os
import re
import sys
from types import ModuleType
from typing import Callable, Dict, List, Optional, Tuple

from config import PLUGINS_DIR, log
from models import CheckResult


class Plugin:
    """A loaded plugin with its patterns and parse function."""

    def __init__(self, name: str, patterns: List[str], parse_fn: Callable):
        self.name = name
        self.patterns = patterns
        self.parse_fn = parse_fn
        self._compiled = [re.compile(p) for p in patterns]

    def matches_url(self, url: str) -> bool:
        return any(r.search(url) for r in self._compiled)


_cache: Dict[str, Plugin] = {}


def _load_module(path: str) -> Optional[ModuleType]:
    """Dynamically load a Python module from a file path."""
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(f"plugins.{name}", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    # Make models importable from within plugins
    if "models" not in sys.modules:
        import models
        sys.modules["models"] = models
    spec.loader.exec_module(mod)
    return mod


def load_plugin(name: str) -> Optional[Plugin]:
    """Load a single plugin by name from plugins/ directory."""
    if name in _cache:
        return _cache[name]

    path = os.path.join(PLUGINS_DIR, f"{name}.py")
    if not os.path.isfile(path):
        log.warning("Plugin file not found: %s", path)
        return None

    try:
        mod = _load_module(path)
        if mod is None:
            return None

        patterns = getattr(mod, "PLATFORM_PATTERNS", None)
        parse_fn = getattr(mod, "parse", None)

        if patterns is None or parse_fn is None:
            log.warning("Plugin %s missing PLATFORM_PATTERNS or parse()", name)
            return None

        plugin = Plugin(name, patterns, parse_fn)
        _cache[name] = plugin
        return plugin
    except Exception as exc:
        log.warning("Failed to load plugin %s: %s", name, exc)
        return None


def load_all_plugins() -> List[Plugin]:
    """Load all .py plugins from the plugins/ directory."""
    plugins = []
    os.makedirs(PLUGINS_DIR, exist_ok=True)

    for filename in sorted(os.listdir(PLUGINS_DIR)):
        if filename.startswith("_") or not filename.endswith(".py"):
            continue
        name = filename[:-3]
        plugin = load_plugin(name)
        if plugin:
            plugins.append(plugin)

    return plugins


def find_plugin_for_url(url: str) -> Optional[Plugin]:
    """Find an existing plugin whose patterns match the given URL."""
    for plugin in load_all_plugins():
        if plugin.matches_url(url):
            log.info("Matched existing plugin '%s' for %s", plugin.name, url)
            return plugin
    return None


def save_plugin(name: str, code: str) -> str:
    """Save plugin code to plugins/{name}.py. Returns the file path."""
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    path = os.path.join(PLUGINS_DIR, f"{name}.py")
    with open(path, "w") as f:
        f.write(code)
    # Invalidate cache so it reloads next time
    _cache.pop(name, None)
    log.info("Saved plugin to %s", path)
    return path


def reload_plugin(name: str) -> Optional[Plugin]:
    """Force-reload a plugin from disk."""
    _cache.pop(name, None)
    # Also remove from sys.modules so importlib reloads it
    mod_name = f"plugins.{name}"
    sys.modules.pop(mod_name, None)
    return load_plugin(name)
