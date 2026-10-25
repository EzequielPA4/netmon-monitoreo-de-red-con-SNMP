"""
Motor de estado: combina el inventario con el ultimo sondeo y arma una
"foto" (snapshot) del estado actual de equipos y enlaces. Solo en memoria.

Estado de un enlace (por prioridad):
  unknown    -> ningun extremo medido tiene datos (equipo sin respuesta)
  admin_down -> algun extremo apagado administrativamente (shutdown)
  down       -> algun extremo con ifOperStatus distinto de up
  (blocked)  -> reservado para STP (modulo posterior)
  critical / warning / ok -> segun utilizacion vs. umbrales
Utilizacion = max(entrada, salida) de ambos extremos / capacidad de referencia.
"""
import time


def device_view(device, poller):
    if poller is None:          # equipo sin SNMP (servidores, Internet)
        return {"id": device.id, "name": device.name, "vendor": device.vendor,
                "ip": device.ip, "pos": device.pos, "snmp": False, "status": "no_snmp"}
    s = poller.state
    return {
        "id": device.id, "name": device.name, "vendor": device.vendor, "ip": device.ip,
        "pos": device.pos, "snmp": True, "status": s["status"], "sys_name": s["sys_name"],
        "uptime_s": s["uptime_s"], "last_ok": s["last_ok"], "last_error": s["last_error"],
        "poll_ms": s["poll_ms"],
        "resources": s.get("resources"),
        "mem_total_mb_inventory": device.mem_total_mb,
        "interfaces": [s["interfaces"][i] for i in sorted(s["interfaces"])],
    }


def link_view(link, pollers, cfg):
    ends = []
    for ep in (link.a, link.b):
        end = {"device": ep.device, "ifindex": ep.ifindex, "iface": None, "device_status": None}
        poller = pollers.get(ep.device)
        if ep.ifindex is not None and poller is not None:
            end["device_status"] = poller.state["status"]
            if poller.state["status"] in ("up", "stale"):
                end["iface"] = poller.state["interfaces"].get(ep.ifindex)
        ends.append(end)

    measured = [e for e in ends if e["iface"] is not None and e["iface"]["exists"]]
    util = None
    rates = [r for e in measured for r in (e["iface"]["in_bps"], e["iface"]["out_bps"]) if r is not None]
    if rates:
        util = max(rates) / link.capacity_bps * 100

    if not measured:
        status = "unknown"
    elif any(e["iface"]["admin"] != "up" for e in measured):
        status = "admin_down"
    elif any(e["iface"]["oper"] != "up" for e in measured):
        status = "down"
    elif util is None:
        status = "ok"            # arriba pero todavia sin tasa (primer ciclo)
    elif util >= cfg.critical_pct:
        status = "critical"
    elif util >= cfg.warning_pct:
        status = "warning"
    else:
        status = "ok"

    return {
        "id": link.id, "status": status, "capacity_bps": link.capacity_bps,
        "utilization_pct": None if util is None else round(util, 1),
        "a": ends[0], "b": ends[1],
    }


def snapshot(cfg, pollers):
    return {
        "ts": time.time(),
        "interval_s": cfg.interval_s,
        "thresholds": {"warning_pct": cfg.warning_pct, "critical_pct": cfg.critical_pct},
        "devices": [device_view(d, pollers.get(d.id)) for d in cfg.devices.values()],
        "links": [link_view(l, pollers, cfg) for l in cfg.links],
    }
