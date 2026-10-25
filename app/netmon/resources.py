"""
CPU y memoria por fabricante.

Cada fabricante expone estos datos con MIB distintas. Los OID se verificaron
contra los equipos del laboratorio el 2026-10-24 (consultas snmpget/snmpwalk):

  FortiGate (FortiOS 7.6.7) - FORTINET-FORTIGATE-MIB, escalares:
    fgSysCpuUsage     1.3.6.1.4.1.12356.101.4.1.3.0   Gauge32 %  (CPU)
    fgSysMemUsage     1.3.6.1.4.1.12356.101.4.1.4.0   Gauge32 %  (memoria)
    fgSysMemCapacity  1.3.6.1.4.1.12356.101.4.1.5.0   Gauge32 KB (RAM total)

  Aruba AOS-CX (10.15) - ARUBAWIRED-SYSTEMINFO-MIB, tabla indexada por
  (tipo de modulo, nombre de modulo) codificados como cadenas:
    arubaWiredSystemInfoCpu     ...47196.4.1.1.3.22.1.0.1.1.3.<indice>  % promedio de todas las CPU
    arubaWiredSystemInfoMemory  ...47196.4.1.1.3.22.1.0.1.1.4.<indice>  %
    (no informa la RAM total -> se muestra "no disponible")

  MikroTik (RouterOS 7.22.1) - HOST-RESOURCES-MIB (RFC 2790):
    hrProcessorLoad   1.3.6.1.2.1.25.3.3.1.2.<n>  % por CPU -> se promedia
    hrStorageTable    1.3.6.1.2.1.25.2.3.1        se busca la fila "main memory"
                      (.3 descr, .4 unidad en bytes, .5 tamano, .6 usado)

Si un dato no existe en un equipo queda en None y la interfaz muestra
"no disponible": nunca se reemplaza por un valor estimado.
"""
import time

FG_CPU = "1.3.6.1.4.1.12356.101.4.1.3.0"
FG_MEM = "1.3.6.1.4.1.12356.101.4.1.4.0"
FG_MEMCAP = "1.3.6.1.4.1.12356.101.4.1.5.0"

ARUBA_TABLE = "1.3.6.1.4.1.47196.4.1.1.3.22.1.0.1.1"
ARUBA_CPU_COL = ARUBA_TABLE + ".3"
ARUBA_MEM_COL = ARUBA_TABLE + ".4"

HR_PROC_LOAD = "1.3.6.1.2.1.25.3.3.1.2"
HR_MEMORY_SIZE = "1.3.6.1.2.1.25.2.2.0"     # hrMemorySize (KB), RFC 2790
HR_STORAGE = "1.3.6.1.2.1.25.2.3.1"


def empty(source):
    return {"cpu_pct": None, "cpu_count": None, "mem_pct": None, "mem_total_bytes": None,
            "mem_used_bytes": None, "source": source, "detail": None, "ts": None, "error": None}


def _decode_str_index(suffix):
    """Indice SNMP de cadenas: <largo>.<bytes>... -> lista de cadenas legibles."""
    parts = [int(x) for x in suffix.split(".") if x]
    out, i = [], 0
    while i < len(parts):
        n = parts[i]
        out.append("".join(chr(c) for c in parts[i + 1:i + 1 + n]))
        i += 1 + n
    return out


async def _fortinet(client):
    r = empty("FORTINET-FORTIGATE-MIB (fgSysCpuUsage, fgSysMemUsage, fgSysMemCapacity)")
    v = await client.get([FG_CPU, FG_MEM, FG_MEMCAP])
    r["cpu_pct"] = None if v.get(FG_CPU) is None else int(v[FG_CPU])
    r["mem_pct"] = None if v.get(FG_MEM) is None else int(v[FG_MEM])
    if v.get(FG_MEMCAP) is not None:
        r["mem_total_bytes"] = int(v[FG_MEMCAP]) * 1024
    return r


async def _aruba(client):
    r = empty("ARUBAWIRED-SYSTEMINFO-MIB (arubaWiredSystemInfoCpu, arubaWiredSystemInfoMemory)")
    cpu = await client.walk(ARUBA_CPU_COL)
    mem = await client.walk(ARUBA_MEM_COL)
    if cpu:
        suffix, value = cpu[0]                     # primer modulo (management_module 1/1)
        r["cpu_pct"] = int(value)
        r["detail"] = " ".join(_decode_str_index(suffix))
    if mem:
        r["mem_pct"] = int(mem[0][1])
    # La MIB de Aruba no trae la RAM total: se intenta hrMemorySize (estandar).
    # Si no responde, la interfaz usa el valor del inventario, marcado como tal.
    try:
        v = await client.get([HR_MEMORY_SIZE])
        if v.get(HR_MEMORY_SIZE) is not None and int(v[HR_MEMORY_SIZE]) > 0:
            r["mem_total_bytes"] = int(v[HR_MEMORY_SIZE]) * 1024
    except Exception:
        pass
    return r


async def _host_resources(client):
    r = empty("HOST-RESOURCES-MIB (hrProcessorLoad, hrStorage 'main memory')")
    loads = [int(v) for _, v in await client.walk(HR_PROC_LOAD)]
    if loads:
        r["cpu_pct"] = round(sum(loads) / len(loads))
        r["cpu_count"] = len(loads)
    descr = await client.walk(HR_STORAGE + ".3")
    idx = next((sfx for sfx, v in descr if str(v).lower() == "main memory"), None)
    if idx is not None:
        v = await client.get([f"{HR_STORAGE}.{c}.{idx}" for c in (4, 5, 6)])
        unit, size, used = (v.get(f"{HR_STORAGE}.{c}.{idx}") for c in (4, 5, 6))
        if None not in (unit, size, used) and int(size) > 0:
            r["mem_total_bytes"] = int(size) * int(unit)
            r["mem_used_bytes"] = int(used) * int(unit)
            r["mem_pct"] = round(int(used) / int(size) * 100)
    return r


PROFILES = {"fortinet": _fortinet, "aruba": _aruba, "mikrotik": _host_resources}


async def collect(vendor, client):
    """Devuelve el dict de recursos del equipo (con 'error' si la consulta fallo)."""
    fn = PROFILES.get(vendor)
    if fn is None:
        r = empty(None)
        r["error"] = f"sin perfil de CPU/memoria para el fabricante '{vendor}'"
        return r
    try:
        r = await fn(client)
    except Exception as exc:          # timeout, error SNMP: el estado de enlaces no se toca
        r = empty(None)
        r["error"] = str(exc)
    r["ts"] = time.time()
    return r
