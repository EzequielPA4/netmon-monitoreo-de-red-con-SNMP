"""Carga y validacion del inventario (inventory.yaml) y de las credenciales."""
import os
from dataclasses import dataclass, field

import yaml

DEFAULT_INVENTORY = "/opt/netmon/config/inventory.yaml"
DEFAULT_ENV_FILE = "/etc/netmon/netmon.env"


@dataclass
class SnmpOptions:
    timeout_s: float = 3
    retries: int = 1


@dataclass
class Device:
    id: str
    name: str
    vendor: str
    ip: str | None
    snmp: SnmpOptions | None          # None = equipo sin SNMP
    pos: dict | None = None           # posicion en el mapa {x, y}
    mem_total_mb: int | None = None   # RAM asignada (inventario) si el equipo no la informa por SNMP
    ifindexes: set[int] = field(default_factory=set)  # se completa con los enlaces


@dataclass
class Endpoint:
    device: str
    ifindex: int | None


@dataclass
class Link:
    id: str
    a: Endpoint
    b: Endpoint
    capacity_bps: int


@dataclass
class Config:
    interval_s: int
    warning_pct: float
    critical_pct: float
    community: str
    trap_port: int                    # 0 = receptor de traps deshabilitado
    resources_interval_s: int         # cada cuanto se consulta CPU/memoria
    devices: dict[str, Device]
    links: list[Link]


def _read_env_file(path):
    """Lee un archivo KEY=valor (formato EnvironmentFile de systemd)."""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(inventory_path=None):
    inventory_path = inventory_path or os.environ.get("NETMON_INVENTORY", DEFAULT_INVENTORY)
    with open(inventory_path) as fh:
        raw = yaml.safe_load(fh)

    # La community se toma del entorno o del archivo .env (nunca del inventario)
    community = os.environ.get("NETMON_COMMUNITY")
    if not community:
        env_file = os.environ.get("NETMON_ENV_FILE", DEFAULT_ENV_FILE)
        community = _read_env_file(env_file).get("NETMON_COMMUNITY")
    if not community:
        raise SystemExit("ERROR: falta NETMON_COMMUNITY (variable de entorno o /etc/netmon/netmon.env)")

    polling = raw.get("polling", {})
    thresholds = raw.get("thresholds", {})
    default_capacity = int(thresholds.get("default_capacity_bps", 3_000_000))

    devices = {}
    for d in raw["devices"]:
        snmp = SnmpOptions(**d["snmp"]) if d.get("snmp") else None
        devices[d["id"]] = Device(d["id"], d["name"], d["vendor"], d.get("ip"), snmp, d.get("pos"),
                                  d.get("mem_total_mb"))

    links = []
    for item in raw["links"]:
        a = Endpoint(item["a"]["device"], item["a"].get("ifindex"))
        b = Endpoint(item["b"]["device"], item["b"].get("ifindex"))
        for ep in (a, b):
            if ep.device not in devices:
                raise SystemExit(f"ERROR: el enlace {item['id']} usa un equipo inexistente: {ep.device}")
            if ep.ifindex is not None:
                if devices[ep.device].snmp is None:
                    raise SystemExit(f"ERROR: {ep.device} no tiene SNMP pero el enlace {item['id']} declara ifindex")
                devices[ep.device].ifindexes.add(int(ep.ifindex))
        links.append(Link(item["id"], a, b, int(item.get("capacity_bps", default_capacity))))

    return Config(
        interval_s=int(polling.get("interval_s", 10)),
        warning_pct=float(thresholds.get("warning_pct", 50)),
        critical_pct=float(thresholds.get("critical_pct", 80)),
        community=community,
        trap_port=int((raw.get("traps") or {}).get("port", 162)),
        resources_interval_s=int(polling.get("resources_interval_s", 30)),
        devices=devices,
        links=links,
    )
