"""Docker writes: the only code in Incident Zero that changes a container. Used by the mock Action API
(mock_server.py) and the practice Simulator, never by the agents or the telemetry layer (a test parses their imports).

Every lookup is scoped to one compose project label, so nothing outside the demo can be touched. Helper containers
(Pumba, tc) carry the `incident_zero.helper` label so they can be found and removed.
"""
from __future__ import annotations

import time
from typing import Any

import docker
from docker.errors import APIError, NotFound

HELPER_LABEL = "incident_zero.helper"


class DockerController:
    def __init__(self, project: str, network: str | None = None):
        self.project = project
        self.network_name = network or f"{project}_default"
        self.client = docker.from_env()

    # ------------------------------------------------------------ lookup
    def containers(self) -> dict[str, Any]:
        out = {}
        for c in self.client.containers.list(all=True, filters={"label": f"com.docker.compose.project={self.project}"}):
            svc = c.labels.get("com.docker.compose.service")
            if svc:
                out[svc] = c
        return out

    def container(self, service: str):
        c = self.containers().get(service)
        if c is None:
            raise RuntimeError(f"no container for service '{service}' in project '{self.project}'")
        c.reload()
        return c

    def is_running(self, service: str) -> bool:
        try:
            return self.container(service).attrs.get("State", {}).get("Status") == "running"
        except RuntimeError:
            return False

    # ------------------------------------------------------------ lifecycle
    def kill(self, service: str, signal: str = "SIGKILL") -> None:
        self.container(service).kill(signal=signal)

    def stop(self, service: str, timeout: int = 10) -> None:
        self.container(service).stop(timeout=timeout)

    def start(self, service: str) -> None:
        c = self.container(service)
        if c.attrs["State"].get("Paused"):
            c.unpause()
        elif c.status != "running":
            c.start()

    def pause(self, service: str) -> None:
        self.container(service).pause()

    def unpause(self, service: str) -> None:
        c = self.container(service)
        if c.attrs["State"].get("Paused"):
            c.unpause()

    def restart(self, service: str, timeout: int = 10) -> None:
        self.container(service).restart(timeout=timeout)

    def wait_running(self, service: str, timeout_s: float = 60) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.is_running(service):
                return True
            time.sleep(1)
        return False

    # ------------------------------------------------------------ network
    def network(self):
        return self.client.networks.get(self.network_name)

    def network_aliases(self, service: str) -> list[str]:
        c = self.container(service)
        nets = c.attrs.get("NetworkSettings", {}).get("Networks", {})
        info = nets.get(self.network_name) or {}
        aliases = list(info.get("Aliases") or [])
        if service not in aliases:
            aliases.append(service)
        return [a for a in aliases if not c.id.startswith(a)]

    def disconnect(self, service: str) -> None:
        self.network().disconnect(self.container(service), force=True)

    def connect(self, service: str, aliases: list[str]) -> None:
        self.network().connect(self.container(service), aliases=aliases)

    def is_connected(self, service: str) -> bool:
        c = self.container(service)
        return self.network_name in c.attrs.get("NetworkSettings", {}).get("Networks", {})

    # ------------------------------------------------------------ helpers (pumba, tc cleanup)
    def pull(self, image: str) -> bool:
        try:
            self.client.images.pull(image)
            return True
        except APIError:
            return False

    def has_image(self, image: str) -> bool:
        try:
            self.client.images.get(image)
            return True
        except (NotFound, APIError):
            return False

    def run_helper(self, image: str, command: list[str], name: str, **kwargs):
        return self.client.containers.run(image, command, name=name, detach=True, labels={HELPER_LABEL: "1"}, **kwargs)

    def remove_helper(self, container) -> None:
        try:
            container.reload()
            if container.status == "running":
                container.kill(signal="SIGTERM")
                try:
                    container.wait(timeout=8)
                except Exception:
                    container.kill()
            container.remove(force=True)
        except (NotFound, APIError):
            pass


def clear_qdisc(docker_ctl: DockerController, tc_image: str, container_name: str) -> bool:
    """Remove any netem qdisc from a container's eth0 via the nettools image (its entrypoint is not tc)."""
    try:
        docker_ctl.client.containers.run(tc_image, ["qdisc", "del", "dev", "eth0", "root"], entrypoint="tc",
                                         network_mode=f"container:{container_name}", cap_add=["NET_ADMIN"], remove=True)
        return True
    except Exception:
        return False
