"""
Prueba del motor de sondeo por consola (sin web).

Uso:
  python -m netmon.cli --cycles 6
"""
import argparse
import asyncio
import time

from .config import load_config
from .engine import Engine

COLOR = {"ok": "\033[32m", "warning": "\033[33m", "critical": "\033[31m",
         "down": "\033[31m", "admin_down": "\033[90m", "unknown": "\033[90m"}
RESET = "\033[0m"
START = time.monotonic()   # reloj monotono: no lo afectan los ajustes de hora (NTP)


def fmt_bps(v):
    if v is None:
        return "-"
    if v >= 1e6:
        return f"{v / 1e6:.2f} Mb/s"
    if v >= 1e3:
        return f"{v / 1e3:.1f} kb/s"
    return f"{v:.0f} b/s"


def print_snapshot(snap, devices_by_id):
    print(f"\n===== {time.strftime('%H:%M:%S')}  (t+{time.monotonic() - START:.1f} s) =====")
    print(f"{'Equipo':<18}{'Estado':<13}{'sysName':<18}{'Uptime':>9}{'GET':>8}  Error")
    for d in snap["devices"]:
        if not d["snmp"]:
            continue
        up = "-" if d["uptime_s"] is None else f"{d['uptime_s']}s"
        ms = "-" if d["poll_ms"] is None else f"{d['poll_ms']}ms"
        print(f"{d['name']:<18}{d['status']:<13}{str(d['sys_name']):<18}{up:>9}{ms:>8}  {d['last_error'] or ''}")

    print(f"\n{'Enlace':<11}{'Estado':<12}{'Util.':>7}   Extremos (ifName admin/oper  entrada/salida  contador)")
    for link in snap["links"]:
        util = "-" if link["utilization_pct"] is None else f"{link['utilization_pct']}%"
        color = COLOR.get(link["status"], "")
        parts = []
        for end in (link["a"], link["b"]):
            name = devices_by_id[end["device"]].name
            iface = end["iface"]
            if end["ifindex"] is None:
                parts.append(f"{name}")
            elif iface is None:
                parts.append(f"{name}:? (sin datos)")
            elif not iface["exists"]:
                parts.append(f"{name}: ifIndex {end['ifindex']} NO EXISTE en el equipo")
            else:
                cnt = f"{iface['counter']}b" if iface["counter"] else "-"
                parts.append(f"{name}:{iface['name']} {iface['admin']}/{iface['oper']} "
                             f"{fmt_bps(iface['in_bps'])}/{fmt_bps(iface['out_bps'])} {cnt}")
        print(f"{link['id']:<11}{color}{link['status']:<12}{RESET}{util:>7}   " + "  <->  ".join(parts))


async def main():
    parser = argparse.ArgumentParser(description="Prueba del motor de sondeo netmon")
    parser.add_argument("--inventory", help="ruta del inventory.yaml")
    parser.add_argument("--cycles", type=int, default=6)
    args = parser.parse_args()

    cfg = load_config(args.inventory)
    engine = Engine(cfg)
    engine.listeners.append(lambda snap: print_snapshot(snap, cfg.devices))
    await engine.start()
    try:
        print(f"Sondeando {len(engine.pollers)} equipos cada {cfg.interval_s} s, {args.cycles} ciclos")
        await engine.run_publisher(args.cycles)
    finally:
        await engine.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
