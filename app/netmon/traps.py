"""
Receptor de traps SNMP (v1 y v2c) sobre UDP, sin depender de snmptrapd.

Decodifica cada datagrama con pysnmp/pyasn1 y entrega un dict normalizado:
  {version, community, trap_oid, agent_uptime, varbinds: [(oid, valor)], ...}

La identificacion del equipo se hace por la IP ORIGEN del datagrama UDP (no por
snmpTrapAddress, que el MikroTik envia mal codificado segun lo observado).
"""
import asyncio
import logging

from pyasn1.codec.ber import decoder
from pysnmp.proto import api

log = logging.getLogger("netmon.traps")

SYS_UPTIME_0 = "1.3.6.1.2.1.1.3.0"
SNMP_TRAP_OID_0 = "1.3.6.1.6.3.1.1.4.1.0"

# Traps genericos de SNMPv1 -> OID SNMPv2 equivalente (RFC 3584, seccion 3.1)
V1_GENERIC = {
    0: "1.3.6.1.6.3.1.1.5.1",   # coldStart
    1: "1.3.6.1.6.3.1.1.5.2",   # warmStart
    2: "1.3.6.1.6.3.1.1.5.3",   # linkDown
    3: "1.3.6.1.6.3.1.1.5.4",   # linkUp
    4: "1.3.6.1.6.3.1.1.5.5",   # authenticationFailure
}


def _value(v):
    """Convierte un valor ASN.1 a algo serializable (int o str)."""
    name = v.__class__.__name__
    if name in ("Integer", "Integer32", "Counter32", "Counter64", "Gauge32",
                "Unsigned32", "TimeTicks"):
        return int(v)
    if name == "ObjectIdentifier":
        return str(v)
    if name == "OctetString":
        raw = bytes(v)
        try:
            text = raw.decode("utf-8")
            if text.isprintable():
                return text
        except UnicodeDecodeError:
            pass
        return "0x" + raw.hex()
    return v.prettyPrint()


def decode(data):
    """Decodifica un mensaje SNMP. Devuelve el dict normalizado o None si no es un trap."""
    version = int(api.decodeMessageVersion(data))
    if version not in api.PROTOCOL_MODULES:
        raise ValueError(f"version SNMP no soportada: {version}")
    p = api.PROTOCOL_MODULES[version]
    msg, _ = decoder.decode(data, asn1Spec=p.Message())
    community = str(p.apiMessage.get_community(msg))
    pdu = p.apiMessage.get_pdu(msg)
    if not pdu.isSameTypeWith(p.TrapPDU()):
        return None                       # GET, RESPONSE, INFORM, etc.: no se procesan

    varbinds = [(str(o), _value(v)) for o, v in p.apiTrapPDU.get_varbinds(pdu)]
    if version == api.SNMP_VERSION_1:
        generic = int(p.apiTrapPDU.get_generic_trap(pdu))
        enterprise = str(p.apiTrapPDU.get_enterprise(pdu))
        if generic == 6:                  # enterpriseSpecific
            trap_oid = f"{enterprise}.0.{int(p.apiTrapPDU.get_specific_trap(pdu))}"
        else:
            trap_oid = V1_GENERIC.get(generic, f"v1-generic-{generic}")
        uptime = int(p.apiTrapPDU.get_timestamp(pdu))
        ver = "v1"
    else:
        values = dict(varbinds)
        trap_oid = str(values.get(SNMP_TRAP_OID_0, ""))
        uptime = values.get(SYS_UPTIME_0)
        uptime = int(uptime) if isinstance(uptime, int) else None
        ver = "v2c"

    return {"version": ver, "community": community, "trap_oid": trap_oid,
            "agent_uptime": uptime, "varbinds": varbinds}


class TrapProtocol(asyncio.DatagramProtocol):
    """Recibe datagramas en el puerto de traps y los pasa al manejador."""

    def __init__(self, handler, stats):
        self.handler = handler
        self.stats = stats

    def datagram_received(self, data, addr):
        self.stats["received"] += 1
        try:
            trap = decode(data)
        except Exception as exc:          # datagrama corrupto o no SNMP
            self.stats["malformed"] += 1
            log.warning("Datagrama no decodificable desde %s: %s", addr[0], exc)
            return
        if trap is None:
            self.stats["not_trap"] += 1
            return
        try:
            self.handler(trap, addr[0])
        except Exception:
            log.exception("Error procesando trap de %s", addr[0])


async def start_listener(handler, stats, host="0.0.0.0", port=162):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: TrapProtocol(handler, stats), local_addr=(host, port))
    log.info("Receptor de traps escuchando en %s:%d/udp", host, port)
    return transport
