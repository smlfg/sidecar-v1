#!/usr/bin/env python3
"""PluginLoader — Discovers and loads RulePlugin subclasses from a directory.

Supports hot-reload: call load() again after SIGHUP to re-import all plugins.
"""

import importlib
import importlib.util
import os
import sys
import time

# Import from sibling module
try:
    from .rule_plugin import RulePlugin
except ImportError:
    from rule_plugin import RulePlugin


def _log(msg: str) -> None:
    """Minimal logger — same pattern as sidecar.py."""
    try:
        with open("/tmp/claude-sidecar.log", "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [plugin_loader] {msg}\n")
    except Exception:
        pass


class PluginLoader:
    """Loads RulePlugin subclasses from a directory of .py files."""

    def __init__(self, rules_dir: str):
        self.rules_dir = rules_dir
        self.plugins: list[RulePlugin] = []

    def load(self) -> list[RulePlugin]:
        """(Re)load all plugins from rules_dir. Returns list of instances."""
        self.plugins = []

        if not os.path.isdir(self.rules_dir):
            _log(f"Rules directory not found: {self.rules_dir}")
            return self.plugins

        for filename in sorted(os.listdir(self.rules_dir)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue

            filepath = os.path.join(self.rules_dir, filename)
            module_name = f"sidecar_rules_{filename[:-3]}"

            try:
                # Force re-import for hot-reload
                if module_name in sys.modules:
                    del sys.modules[module_name]

                spec = importlib.util.spec_from_file_location(module_name, filepath)
                if spec is None or spec.loader is None:
                    continue

                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                # Find all RulePlugin subclasses in the module
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, RulePlugin)
                        and attr is not RulePlugin
                    ):
                        instance = attr()
                        self.plugins.append(instance)
                        _log(f"Loaded plugin: {instance.name} from {filename}")

            except Exception as e:
                _log(f"Error loading plugin {filename}: {e}")

        _log(f"Total plugins loaded: {len(self.plugins)}")
        return self.plugins

    def match_rules(self, prompt: str, session_state: dict) -> str:
        """Run all plugins, return combined injection text for matching rules."""
        injections = []
        for plugin in self.plugins:
            try:
                if plugin.match(prompt, session_state):
                    text = plugin.inject()
                    if text:
                        injections.append(text)
                        _log(f"Rule fired: {plugin.name}")
            except Exception as e:
                _log(f"Error in plugin {plugin.name}.match/inject: {e}")

        return "\n\n".join(injections) if injections else ""
