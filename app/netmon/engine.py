"""
Motor de monitoreo.

- Sondeo: cada equipo SNMP tiene su PROPIO bucle; un equipo lento o caido no
  demora a los demas. Un bucle publicador arma la foto del estado a ritmo fijo.
- Traps: un trap actualiza el mapa al instante (estado "informado por trap") y
  dispara un sondeo inmediato del equipo para CONFIRMAR el estado real, porque
  un trap puede perderse, duplicarse o llegar fuera de orden.
"""
import asyncio
import logging
import time

from .events import TRAP_TYPES, TRIGGERS_POLL, EventLog, parse_interface
from .poller import DevicePoller
from .state import snapshot
from .traps import start_listener

log = logging.getLogger("netmon.engine")
CONFIRM_DELAY_S = 1.0     # espera antes del sondeo de confirmacion tras un trap


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pollers = {
            d.id: DevicePoller(d, cfg.community, min_rate_interval_s=cfg.interval_s / 2)
            for d in cfg.devices.values() if d.snmp is not None
        }
        self.last_snapshot = None
        self.listeners = []          # callbacks que reciben cada snapshot
        self.event_listeners = []    # callbacks que reciben cada evento (trap)
        self.events = EventLog()
        self.by_ip = {d.ip: d for d in cfg.devices.values() if d.ip}
        self.link_by_end = {(ep.device, ep.ifindex): l.id
                            for l in cfg.links for ep in (l.a, l.b) if ep.ifindex is not None}
        self._tasks = []
        self._confirming = set()     # equipos con un sondeo de confirmacion pendiente
        self._to_confirm = {}        # equipo -> eventos de enlace esperando confirmacion
        self._trap_transport = None

    async def start(self, trap_port=None):
        for p in self.pollers.values():
            await p.open()
        self._tasks = [asyncio.create_task(self._device_loop(p)) for p in self.pollers.values()]
        if trap_port:
            self._trap_transport = await start_listener(self.handle_trap, self.events.stats,
                                                        port=trap_port)

    async def stop(self):
        if self._trap_transport:
            self._trap_transport.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for p in self.pollers.values():
            p.close()

    async def _device_loop(self, poller):
        """
        Sondea un equipo cada interval_s. Si un sondeo se atrasa (timeouts), el
        siguiente se agenda un intervalo completo despues: no se acumulan ciclos
        ni se hacen consultas seguidas.
        """
        interval = self.cfg.interval_s
        next_at = time.monotonic()
        while True:
            try:
                await poller.poll(resources_every_s=self.cfg.resources_interval_s)
            except Exception:                       # un error inesperado no debe matar el bucle
                log.exception("Error sondeando %s", poller.device.id)
            next_at += interval
            now = time.monotonic()
            if next_at < now:                       # se atraso (timeout largo): realinear
                next_at = now + interval
            await asyncio.sleep(next_at - now)

    def publish(self):
        snap = snapshot(self.cfg, self.pollers)
        snap["traps"] = {"enabled": self._trap_transport is not None, **self.events.stats}
        self.last_snapshot = snap
        for callback in self.listeners:
            callback(snap)
        return snap

    async def run_publisher(self, cycles=None):
        """Publica la foto del estado cada interval_s (desfasado medio intervalo del sondeo)."""
        n = 0
        await asyncio.sleep(self.cfg.interval_s / 2)
        next_at = time.monotonic()
        while cycles is None or n < cycles:
            self.publish()
            n += 1
            next_at += self.cfg.interval_s
            await asyncio.sleep(max(0, next_at - time.monotonic()))

    # ------------------------------------------------------------------ traps
    def handle_trap(self, trap, src_ip):
        stats = self.events.stats
        if trap["community"] != self.cfg.community:
            stats["bad_community"] += 1
            log.warning("Trap descartado: community incorrecta (origen %s)", src_ip)
            return

        device = self.by_ip.get(src_ip)
        if device is None:
            stats["unknown_source"] += 1
        poller = self.pollers.get(device.id) if device else None
        ttype, description = TRAP_TYPES.get(trap["trap_oid"], ("other", "Otro trap"))
        iface = parse_interface(trap["varbinds"])
        ifindex = iface["ifindex"]

        ifname = iface["ifname"]
        if not ifname and poller and ifindex is not None:
            current = poller.state["interfaces"].get(ifindex)
            ifname = current["name"] if current else None

        new_oper = iface["oper"] or {"linkDown": "down", "linkUp": "up"}.get(ttype)
        prev_admin = prev_oper = None
        if poller and ifindex is not None and ttype in ("linkDown", "linkUp"):
            prev_admin, prev_oper = poller.apply_trap(ifindex, iface["admin"], new_oper)

        event = {
            "ts": time.time(),
            "device": device.id if device else None,
            "device_name": device.name if device else None,
            "source_ip": src_ip,
            "type": ttype,
            "description": description,
            "trap_oid": trap["trap_oid"],
            "version": trap["version"],
            "agent_uptime": trap["agent_uptime"],
            "ifindex": ifindex,
            "ifname": ifname,
            "link": self.link_by_end.get((device.id, ifindex)) if device else None,
            "prev_admin": prev_admin,
            "prev_oper": prev_oper,
            "new_admin": iface["admin"],
            "new_oper": new_oper if ttype in ("linkDown", "linkUp") else None,
            "varbinds": [[o, v] for o, v in trap["varbinds"]],
            "confirmation": None,    # coincide | difiere | sin_respuesta | no_monitoreada
            "confirmed_oper": None,
        }
        event, is_new = self.events.add(event)
        public = self.events.public(event)
        for callback in self.event_listeners:
            callback(public)
        if is_new:
            log.info("Trap %s de %s (%s) ifIndex=%s", ttype, event["device_name"] or "?", src_ip, ifindex)
            self.publish()                                   # el mapa cambia sin esperar el ciclo
            if poller and ttype in TRIGGERS_POLL:
                if ttype in ("linkDown", "linkUp"):
                    self._to_confirm.setdefault(poller.device.id, []).append(event)
                self._confirm(poller)

    def _confirm(self, poller):
        """Sondeo inmediato para confirmar el estado real tras un trap (uno por equipo a la vez)."""
        if poller.device.id in self._confirming:
            return
        self._confirming.add(poller.device.id)

        async def run():
            try:
                await asyncio.sleep(CONFIRM_DELAY_S)
                await poller.poll()
                self._record_confirmation(poller)
                self.publish()
            except Exception:
                log.exception("Error en sondeo de confirmacion de %s", poller.device.id)
            finally:
                self._confirming.discard(poller.device.id)
                if self._to_confirm.get(poller.device.id):   # llegaron traps durante el sondeo
                    self._confirm(poller)

        task = asyncio.create_task(run())
        self._tasks.append(task)
        task.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    def _record_confirmation(self, poller):
        """Compara el estado que informo cada trap con el que devuelve el sondeo."""
        reachable = poller.state["status"] == "up"
        for event in self._to_confirm.pop(poller.device.id, []):
            iface = poller.state["interfaces"].get(event["ifindex"])
            if not reachable:
                event["confirmation"] = "sin_respuesta"
            elif iface is None:
                event["confirmation"] = "no_monitoreada"
            else:
                event["confirmed_oper"] = iface["oper"]
                event["confirmation"] = "coincide" if iface["oper"] == event["new_oper"] else "difiere"
            public = self.events.public(event)
            for callback in self.event_listeners:
                callback(public)

