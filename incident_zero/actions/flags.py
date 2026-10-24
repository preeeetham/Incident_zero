"""The demo's flagd file, as seen by the Action API mock (toggle_feature_flag) and the practice Simulator only.

The agents, the telemetry layer and the knowledge layer must never import this module (REQUIREMENTS R1); a test
enforces it. flagd watches the file and picks a change up within seconds.
"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Optional

FLAG_FILE = Path("src") / "flagd" / "demo.flagd.json"


_lock = threading.Lock()


class FlagFile:
    def __init__(self, demo_dir: str | Path):
        self.path = Path(demo_dir).expanduser() / FLAG_FILE

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    def _write(self, data: dict) -> None:
        # In place, not write-temp-and-rename: flagd watches this file through a Docker Desktop bind mount, and after
        # a rename replaced the file it stopped seeing changes (found live: the file said adHighCpu=on, flagd kept
        # serving off). Writing the same file keeps the watch; flagd reloads on the final write.
        with open(self.path, "r+") as f:
            f.seek(0)
            f.write(json.dumps(data, indent=2) + "\n")
            f.truncate()

    @staticmethod
    def _rule(flag: dict) -> Optional[list]:
        """The demo's targeting shape: {"if": [condition, variant_when_matched, variant_otherwise]}."""
        rule = (flag.get("targeting") or {}).get("if")
        return rule if isinstance(rule, list) and len(rule) == 3 else None

    def variants(self) -> dict[str, str]:
        """The effective variant per flag. flagd applies targeting before defaultVariant, so a targeted flag
        (productCatalogFailure: only product OLJCESPC7Z) is on when its matched branch is on."""
        out = {}
        for k, v in self._read().get("flags", {}).items():
            rule = self._rule(v)
            out[k] = rule[1] if rule and rule[1] != "off" else v.get("defaultVariant")
        return out

    def set(self, values: dict[str, str]) -> dict[str, dict]:
        """Set each flag to a variant; returns the flags' previous definitions (the undo, for `restore`).
        A targeted flag is switched on through its rule's matched branch (as the demo's flag UI does) and off by
        setting every branch and the default off; anything else through defaultVariant."""
        with _lock:
            data = self._read()
            prev: dict[str, dict] = {}
            for k, variant in values.items():
                flag = data.get("flags", {}).get(k)
                if flag is None:
                    raise KeyError(f"unknown flag {k}")
                if variant not in (flag.get("variants") or {}):
                    raise ValueError(f"flag {k} has no variant {variant!r}")
                prev[k] = copy.deepcopy(flag)
                off = self._off(flag)
                rule = self._rule(flag)
                if rule is None:
                    flag["defaultVariant"] = variant
                elif variant == off:
                    rule[1], rule[2], flag["defaultVariant"] = off, off, off
                else:
                    rule[1] = variant
            self._write(data)
            return prev

    def restore(self, prev: dict) -> None:
        """Undo a `set`: put the previous flag definitions back (older undo records hold variant names)."""
        if all(isinstance(v, str) for v in prev.values()):
            self.set(prev)
            return
        with _lock:
            data = self._read()
            for k, flag in prev.items():
                data["flags"][k] = flag
            self._write(data)

    @staticmethod
    def _off(flag: dict) -> str:
        variants = flag.get("variants") or {}
        for name in ("off", "false", "disabled"):
            if name in variants:
                return name
        for name, value in variants.items():
            if value in (False, 0, "off"):
                return name
        return next(iter(variants))

    def off_variant(self, flag: str) -> str:
        return self._off(self._read()["flags"][flag])

    def on_variant(self, flag: str) -> str:
        variants = (self._read()["flags"][flag].get("variants") or {})
        off = self.off_variant(flag)
        if "on" in variants:
            return "on"
        if "100%" in variants:
            return "100%"
        return next((n for n in variants if n != off), off)
