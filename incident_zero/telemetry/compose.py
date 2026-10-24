"""The call graph from the demo's own compose files: which service talks to which, read without running anything.

`depends_on` alone misses most dependencies: services reach a cache or another service through an environment
variable such as `VALKEY_ADDR=valkey-cart:6379` or `PRODUCT_CATALOG_ADDR=product-catalog:8080`. So besides
`depends_on` this scans environment variables for hostnames that resolve to other services (or their aliases).
Secrets are never read as hosts. backends.load_topology merges the compose layers and calls this.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

HOST_KEY_RE = re.compile(r"(_HOSTNAME|_HOST|_URI|_URL|_ADDR|_ADDRESS|_ENDPOINT|_SEED_NODES)$")
SECRET_KEY_RE = re.compile(r"PASSWORD|SECRET|TOKEN", re.IGNORECASE)
URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
DATASTORE_IMAGE_RE = re.compile(r"mongo|postgres|postgis|timescale|redis|valkey|keydb|mysql|mariadb|percona|cassandra"
                                r"|scylla", re.I)
EVIDENCE_VALUE_MAX = 60


@dataclass
class ServiceNode:
    name: str
    image: str = ""
    role: str = "app"                # datastore | app | proxy | ui
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: str                        # depends_on | env
    evidence: str = ""


# ---------------------------------------------------------------- loading / small parsers
def load_compose(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: compose file does not contain a mapping")
    return data


def parse_env(raw: Any) -> dict[str, str]:
    """Normalise compose `environment` (list of "K=V" / "K" or a mapping) into {str: str}."""
    out: dict[str, str] = {}
    if not raw:
        return out
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = "" if v is None else str(v)
        return out
    for item in raw:
        if item is None:
            continue
        if isinstance(item, dict):  # odd but seen in the wild
            out.update(parse_env(item))
            continue
        k, sep, v = str(item).partition("=")
        out[k.strip()] = v.strip() if sep else ""
    return out


def parse_aliases(networks: Any) -> list[str]:
    """`networks` may be a list of names (no aliases) or a mapping {net: {aliases: [...]}}."""
    if not isinstance(networks, dict):
        return []
    out: list[str] = []
    for _net, cfg in networks.items():
        if isinstance(cfg, dict):
            for a in cfg.get("aliases") or []:
                if a and str(a) not in out:
                    out.append(str(a))
    return out


def _guess_role(name: str, image: str) -> str:
    if DATASTORE_IMAGE_RE.search(f"{name} {image}"):
        return "datastore"
    if re.search(r"nginx|traefik|haproxy|envoy", image.lower()) or name.lower() == "nginx":
        return "proxy"
    if name.lower().endswith(("-ui", "_ui")):
        return "ui"
    return "app"


# ---------------------------------------------------------------- nodes
def service_nodes(compose: dict) -> dict[str, ServiceNode]:
    nodes: dict[str, ServiceNode] = {}
    for name, svc in (compose.get("services") or {}).items():
        svc = svc or {}
        image = str(svc.get("image") or "")
        nodes[name] = ServiceNode(name=name, image=image, role=_guess_role(name, image),
                                  aliases=parse_aliases(svc.get("networks")))
    return nodes


# ---------------------------------------------------------------- edges
def _alias_index(nodes: dict[str, ServiceNode]) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    for n in nodes.values():
        idx.setdefault(n.name, []).append(n.name)
        for a in n.aliases:
            idx.setdefault(a, [])
            if n.name not in idx[a]:
                idx[a].append(n.name)
    return idx


def _extract_hosts(value: str) -> list[str]:
    """Pull hostname candidates out of an env value: bare hosts, host:port, URIs, comma lists."""
    hosts: list[str] = []
    for token in re.split(r"[,\s;]+", value.strip()):
        if not token:
            continue
        if URI_RE.match(token):
            token = URI_RE.sub("", token, count=1)
        token = token.split("/", 1)[0]  # drop path
        token = token.rsplit("@", 1)[-1]  # drop user:pass@
        token = token.split("?", 1)[0]
        if token.startswith("["):  # [ipv6]:port
            token = token[1:].split("]", 1)[0]
        else:
            token = token.split(":", 1)[0]  # drop port
        token = token.strip()
        if token and token not in hosts:
            hosts.append(token)
    return hosts


def _dedupe(edges: Iterable[Edge]) -> list[Edge]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Edge] = []
    for e in edges:
        if e.src == e.dst:
            continue
        key = (e.src, e.dst, e.kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _depends_on_names(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, dict):
        return [str(k) for k in raw.keys()]
    return [str(x) for x in raw]


def static_edges(compose: dict, nodes: dict[str, ServiceNode]) -> list[Edge]:
    edges: list[Edge] = []
    services = compose.get("services") or {}
    alias_idx = _alias_index(nodes)

    # 1. depends_on (list or dict form)
    for name, svc in services.items():
        for dep in _depends_on_names((svc or {}).get("depends_on")):
            if dep in nodes:
                edges.append(Edge(src=name, dst=dep, kind="depends_on", evidence="depends_on"))

    # 2. environment scan: hostnames that resolve to another service
    for name, svc in services.items():
        for key, value in parse_env((svc or {}).get("environment")).items():
            if SECRET_KEY_RE.search(key) or not value:
                continue
            hosts = _extract_hosts(value)
            targets: list[str] = []
            if HOST_KEY_RE.search(key):
                for h in hosts:
                    targets.extend(alias_idx.get(h, []))
            elif value in alias_idx:
                # without a host-ish key be strict: the whole value (or URI host) must name a service
                targets.extend(alias_idx[value])
            elif URI_RE.match(value) and len(hosts) == 1:
                targets.extend(alias_idx.get(hosts[0], []))
            shown = value if len(value) <= EVIDENCE_VALUE_MAX else value[: EVIDENCE_VALUE_MAX - 1] + "…"
            for dst in targets:
                if dst != name:
                    edges.append(Edge(src=name, dst=dst, kind="env", evidence=f"env {key}={shown}"))

    return _dedupe(edges)
