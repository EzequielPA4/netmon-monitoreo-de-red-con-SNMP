"""
Linea de tiempo de eventos (solo en memoria) y clasificacion de traps.

Cada evento registra: hora de recepcion, equipo e IP de origen, tipo, OID del
trap, interfaz (ifIndex/ifName), estado previo (ultimo sondeo) y estado nuevo
(informado por el trap), enlace del mapa, varbinds recibidos y duplicados.
"""
import itertools
import time
from collections import deque

IF_INDEX = "1.3.6.1.2.1.2.2.1.1."
IF_DESCR = "1.3.6.1.2.1.2.2.1.2."
IF_ADMIN = "1.3.6.1.2.1.2.2.1.7."
IF_OPER = "1.3.6.1.2.1.2.2.1.8."
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1."
IF_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown",
             5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}

# OID de notificacion -> (tipo, descripcion). Fuentes: SNMPv2-MIB (RFC 3418),
# IF-MIB (RFC 2863) y ARUBAWIRED-CONFIG-MIB (wndFeatures 20 -> notificacion 0.1).
TRAP_TYPES = {
    "1.3.6.1.6.3.1.1.5.1": ("coldStart", "Reinicio del agente (coldStart)"),
    "1.3.6.1.6.3.1.1.5.2": ("warmStart", "Reinicio del agente (warmStart)"),
    "1.3.6.1.6.3.1.1.5.3": ("linkDown", "Interfaz caida"),
    "1.3.6.1.6.3.1.1.5.4": ("linkUp", "Interfaz levantada"),
    "1.3.6.1.6.3.1.1.5.5": ("authFailure", "Fallo de autenticacion SNMP"),
    "1.3.6.1.4.1.47196.4.1.1.3.20.0.1": ("configChange", "Cambio de configuracion (Aruba)"),
}
# Tipos que justifican un sondeo inmediato del equipo para confirmar el estado
TRIGGERS_POLL = {"linkDown", "linkUp", "coldStart", "warmStart", "configChange"}

DEDUP_WINDOW_S = 2.0
MAX_EVENTS = 200


def parse_interface(varbinds):
    """Extrae ifIndex, ifName/ifDescr y estados admin/oper de los varbinds del trap."""
    info = {"ifindex": None, "ifname": None, "admin": None, "oper": None}
    for oid, value in varbinds:
        for prefix, key in ((IF_INDEX, "ifindex"), (IF_ADMIN, "admin"),
                            (IF_OPER, "oper"), (IF_NAME, "ifname"), (IF_DESCR, "ifdescr")):
            if oid.startswith(prefix):
                suffix = oid[len(prefix):]
                if suffix.isdigit() and info["ifindex"] is None:
                    info["ifindex"] = int(suffix)       # el indice va en el OID
                if key == "ifindex" and isinstance(value, int):
                    info["ifindex"] = value
                elif key in ("admin", "oper") and isinstance(value, int):
                    info[key] = IF_STATUS.get(value, str(value))
                elif key == "ifname":
                    info["ifname"] = str(value)
                elif key == "ifdescr" and not info["ifname"]:
                    info["ifname"] = str(value)
    return info


class EventLog:
    def __init__(self, maxlen=MAX_EVENTS):
        self.events = deque(maxlen=maxlen)
        self._ids = itertools.count(1)
        self._last = {}                 # clave de deduplicacion -> evento
        self._last_uptime = {}          # equipo -> sysUpTime del ultimo trap
        self.stats = {"received": 0, "malformed": 0, "not_trap": 0, "bad_community": 0,
                      "unknown_source": 0, "duplicates": 0, "events": 0}

    def add(self, event):
        """
        Agrega un evento. Si es duplicado (mismo equipo, interfaz y tipo dentro de
        DEDUP_WINDOW_S), incrementa el contador del original y devuelve (evento, False).
        """
        now = time.monotonic()
        key = (event["device"] or event["source_ip"], event["ifindex"], event["type"])
        prev = self._last.get(key)
        if prev and now - prev["_mono"] <= DEDUP_WINDOW_S:
            prev["duplicates"] += 1
            self.stats["duplicates"] += 1
            return prev, False

        # Fuera de orden: el sysUpTime del agente es menor que el del trap anterior
        # (salvo un reinicio, que vuelve a empezar desde cero).
        dev_key = event["device"] or event["source_ip"]
        up = event.get("agent_uptime")
        last_up = self._last_uptime.get(dev_key)
        event["out_of_order"] = bool(up is not None and last_up is not None and up < last_up
                                     and event["type"] not in ("coldStart", "warmStart"))
        if up is not None and not event["out_of_order"]:
            self._last_uptime[dev_key] = up

        event["id"] = next(self._ids)
        event["duplicates"] = 0
        event["_mono"] = now
        self._last[key] = event
        self.events.appendleft(event)
        self.stats["events"] += 1
        return event, True

    def clear(self):
        """Vacia la linea de tiempo y reinicia los contadores (accion manual desde la web)."""
        self.events.clear()
        self._last.clear()
        self._last_uptime.clear()
        for key in self.stats:
            self.stats[key] = 0

    def public(self, event):
        return {k: v for k, v in event.items() if not k.startswith("_")}

    def recent(self, n=MAX_EVENTS):
        return [self.public(e) for e in list(self.events)[:n]]
