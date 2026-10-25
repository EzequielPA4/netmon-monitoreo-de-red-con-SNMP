"""Cliente SNMPv2c minimo sobre pysnmp 7.1 (asyncio)."""
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    bulk_cmd,
    get_cmd,
)

# Maximo de objetos por GET (evita PDUs demasiado grandes)
MAX_VARBINDS = 40
NO_VALUE = {"NoSuchObject", "NoSuchInstance", "EndOfMibView"}


class SnmpError(Exception):
    """Timeout o error devuelto por el agente."""


def oid_text(oid):
    for attr in ("get_oid", "getOid"):
        if hasattr(oid, attr):
            oid = getattr(oid, attr)()
            break
    return str(oid)


def is_empty(value):
    return value is None or value.__class__.__name__ in NO_VALUE


class SnmpClient:
    def __init__(self, host, community, timeout=3, retries=1):
        self.host = host
        self.timeout = timeout
        self.retries = retries
        self.engine = SnmpEngine()
        self.auth = CommunityData(community, mpModel=1)  # mpModel=1 -> SNMPv2c
        self.target = None

    async def open(self):
        self.target = await UdpTransportTarget.create(
            (self.host, 161), timeout=self.timeout, retries=self.retries
        )

    def _check(self, err_ind, err_stat, err_idx):
        if err_ind:
            raise SnmpError(str(err_ind))
        if err_stat:
            raise SnmpError(f"{err_stat.prettyPrint()} (indice {err_idx})")

    async def get(self, oids):
        """GET de una lista de OID. Devuelve {oid: valor} (valor None si no existe)."""
        result = {}
        for start in range(0, len(oids), MAX_VARBINDS):
            chunk = oids[start:start + MAX_VARBINDS]
            err_ind, err_stat, err_idx, var_binds = await get_cmd(
                self.engine, self.auth, self.target, ContextData(),
                *[ObjectType(ObjectIdentity(o)) for o in chunk],
                lookupMib=False,
            )
            self._check(err_ind, err_stat, err_idx)
            for vb in var_binds:
                result[oid_text(vb[0])] = None if is_empty(vb[1]) else vb[1]
        return result

    async def walk(self, base, max_rep=25):
        """Recorre un subarbol con GETBULK. Devuelve [(sufijo_del_oid, valor)]."""
        result = []
        current = base
        prefix = base + "."
        while True:
            err_ind, err_stat, err_idx, table = await bulk_cmd(
                self.engine, self.auth, self.target, ContextData(),
                0, max_rep, ObjectType(ObjectIdentity(current)),
                lookupMib=False,
            )
            self._check(err_ind, err_stat, err_idx)
            if not table:
                return result
            for vb in table:
                oid, value = oid_text(vb[0]), vb[1]
                if not oid.startswith(prefix) or is_empty(value):
                    return result
                result.append((oid[len(prefix):], value))
                current = oid

    async def column(self, base, max_rep=25):
        """Recorre una columna con indice numerico simple. Devuelve {indice: valor}."""
        return {int(sfx.split(".")[0]): v for sfx, v in await self.walk(base, max_rep)}

    def close(self):
        self.engine.close_dispatcher()
