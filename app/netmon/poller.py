"""
Sondeo SNMP de un equipo.

Cada ciclo hace UN GET con: sysName, sysUpTime y, para cada ifIndex que
aparece en los enlaces del inventario: ifName, ifAdminStatus, ifOperStatus,
ifInOctets/ifOutOctets (32 bits) e ifHCInOctets/ifHCOutOctets (64 bits).

Tasas:
  - base de tiempo = diferencia de sysUpTime (reloj del agente)
  - contador de 64 bits si avanzo; si no, el de 32 bits (Aruba OVA: HC = 0)
  - delta = (nuevo - anterior) mod 2^N (tolera el desborde)
  - si sysUpTime retrocede (reinicio), se descarta la muestra anterior
"""
import asyncio
import time

from . import resources
from .snmp import SnmpClient, SnmpError

SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
IF_COLUMNS = {
    "ifName": "1.3.6.1.2.1.31.1.1.1.1",
    "ifAdminStatus": "1.3.6.1.2.1.2.2.1.7",
    "ifOperStatus": "1.3.6.1.2.1.2.2.1.8",
    "ifInOctets": "1.3.6.1.2.1.2.2.1.10",
    "ifOutOctets": "1.3.6.1.2.1.2.2.1.16",
    "ifHCInOctets": "1.3.6.1.2.1.31.1.1.1.6",
    "ifHCOutOctets": "1.3.6.1.2.1.31.1.1.1.10",
}
IF_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown",
             5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}

# Fallos consecutivos para considerar el equipo inalcanzable
FAILURES_TO_UNREACHABLE = 2


def _as_int(value):
    return None if value is None else int(value)


def _delta(old, new, bits):
    return (new - old) % (2 ** bits)


class DevicePoller:
    def __init__(self, device, community, min_rate_interval_s=5):
        self.device = device
        # Intervalo minimo para calcular tasas: los contadores del Aruba OVA no se
        # actualizan en cada consulta, y un intervalo muy corto daria 0 b/s falsos.
        self.min_rate_interval_s = min_rate_interval_s
        self.client = SnmpClient(device.ip, community,
                                 device.snmp.timeout_s, device.snmp.retries)
        self.indexes = sorted(device.ifindexes)
        self.prev = None          # muestra anterior: (uptime_ticks, {idx: valores})
        self.failures = 0
        self.lock = asyncio.Lock()   # evita dos sondeos simultaneos (ciclo + trap)
        # Estado publico (lo lee el motor de estado)
        self.state = {
            "status": "unknown",   # unknown | up | stale | unreachable
            "sys_name": None,
            "uptime_s": None,
            "last_ok": None,       # epoch del ultimo sondeo correcto
            "last_error": None,
            "poll_ms": None,
            "interfaces": {},      # idx -> dict
            "resources": None,     # CPU / memoria (ver resources.py)
        }
        self._last_resources = 0.0

    async def open(self):
        await self.client.open()

    def close(self):
        self.client.close()

    def _oids(self):
        oids = [SYS_NAME, SYS_UPTIME]
        for idx in self.indexes:
            oids += [f"{base}.{idx}" for base in IF_COLUMNS.values()]
        return oids

    async def poll(self, resources_every_s=None):
        async with self.lock:
            await self._poll()
            # CPU/memoria con menor frecuencia y solo si el equipo responde
            due = resources_every_s is not None and \
                time.monotonic() - self._last_resources >= resources_every_s
            if due and self.state["status"] == "up":
                self._last_resources = time.monotonic()
                self.state["resources"] = await resources.collect(self.device.vendor, self.client)

    def apply_trap(self, ifindex, admin=None, oper=None):
        """
        Aplica el estado informado por un trap hasta que el proximo sondeo lo confirme.
        Devuelve el estado previo (admin, oper) segun el ultimo sondeo, o (None, None).
        """
        iface = self.state["interfaces"].get(ifindex)
        if iface is None:
            return None, None
        prev = (iface["admin"], iface["oper"])
        if admin:
            iface["admin"] = admin
        if oper:
            iface["oper"] = oper
        iface["source"] = "trap"
        if oper and oper != "up":
            iface["in_bps"] = iface["out_bps"] = None   # sin enlace, la tasa vieja no vale
        return prev

    async def _poll(self):
        t0 = time.monotonic()
        try:
            values = await self.client.get(self._oids())
        except SnmpError as exc:
            self.failures += 1
            self.state["last_error"] = str(exc)
            self.state["status"] = ("unreachable" if self.failures >= FAILURES_TO_UNREACHABLE
                                    else "stale")
            if self.state["status"] == "unreachable":
                # Sin datos validos: no se muestran tasas viejas como actuales
                for iface in self.state["interfaces"].values():
                    iface["in_bps"] = iface["out_bps"] = None
            return
        self.state["poll_ms"] = round((time.monotonic() - t0) * 1000)
        self.failures = 0
        self.state["last_error"] = None

        uptime = _as_int(values.get(SYS_UPTIME))
        sample = {}
        for idx in self.indexes:
            row = {}
            for name, base in IF_COLUMNS.items():
                value = values.get(f"{base}.{idx}")
                if name == "ifName":
                    row[name] = None if value is None else str(value)
                else:
                    row[name] = _as_int(value)
            sample[idx] = row

        dt = None
        if self.prev and uptime is not None and self.prev[0] is not None:
            if uptime >= self.prev[0]:
                dt = (uptime - self.prev[0]) / 100   # sysUpTime: centesimas de segundo
            # si retrocedio: reinicio -> dt queda None y no se calculan tasas

        # Muestra demasiado cercana a la anterior: se actualizan los estados, se
        # conservan las ultimas tasas y NO se reemplaza la muestra base.
        short = dt is not None and dt < self.min_rate_interval_s
        old_ifaces = self.state["interfaces"]

        interfaces = {}
        for idx, cur in sample.items():
            iface = {
                "ifindex": idx,
                "name": cur["ifName"],
                "exists": cur["ifOperStatus"] is not None,
                "admin": IF_STATUS.get(cur["ifAdminStatus"]),
                "oper": IF_STATUS.get(cur["ifOperStatus"]),
                "in_bps": None,
                "out_bps": None,
                "counter": None,
                "source": "poll",       # poll = confirmado por sondeo; trap = informado por trap
            }
            if short and idx in old_ifaces:
                for key in ("in_bps", "out_bps", "counter"):
                    iface[key] = old_ifaces[idx][key]
            elif dt and dt > 0:
                old = self.prev[1].get(idx)
                if old:
                    for direction, key in (("In", "in_bps"), ("Out", "out_bps")):
                        rate, bits = self._rate(old, cur, direction, dt)
                        iface[key] = rate
                        if bits:
                            iface["counter"] = max(iface["counter"] or 0, bits)
            interfaces[idx] = iface

        if not short:
            self.prev = (uptime, sample)
        self.state.update({
            "status": "up",
            "sys_name": str(values.get(SYS_NAME)) if values.get(SYS_NAME) is not None else None,
            "uptime_s": None if uptime is None else uptime // 100,
            "last_ok": time.time(),
            "interfaces": interfaces,
        })

    @staticmethod
    def _rate(old, cur, direction, dt):
        """Devuelve (bit/s, bits del contador usado) o (None, None)."""
        hc, lo = f"ifHC{direction}Octets", f"if{direction}Octets"
        if old[hc] is not None and cur[hc] is not None and cur[hc] != old[hc]:
            return _delta(old[hc], cur[hc], 64) * 8 / dt, 64
        if old[lo] is not None and cur[lo] is not None:
            bits = 32 if cur[lo] != old[lo] else None
            return _delta(old[lo], cur[lo], 32) * 8 / dt, bits
        return None, None
