"""Limite de mensajes entrantes por socket.

    DJANGO_SOCKET = {"RATE_LIMIT": "60/m"}      # para todas las rutas
    @ws("chat/", rate_limit="10/s")             # o solo para esta

Es un *token bucket*, no un contador por ventana, y la diferencia importa: un
contador rechaza el mensaje 11 aunque los 10 anteriores fueran de hace 59
segundos. El cubo se rellena de forma continua, asi que aguanta la rafaga
normal de alguien escribiendo rapido y solo corta cuando el ritmo *sostenido*
pasa del limite.

Al agotarse se cierra con **4429**. Para dejar pasar picos mas grandes, sube el
`burst`:

    @ws("cursor/", rate_limit="30/s", burst=100)
"""

from __future__ import annotations

import re
import time

UNIDADES = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_FORMATO = re.compile(r"^\s*(\d+)\s*/\s*(\d*)\s*([smhd])\s*$", re.I)

CLOSE_RATE_LIMIT = 4429


def parsear(spec: str) -> tuple[float, float]:
    """
    "60/m" -> (60 mensajes, 60 segundos).  Tambien "100/5m", "10/s".

    Devuelve (cantidad, periodo_en_segundos).
    """
    if not isinstance(spec, str):
        raise ValueError(f"rate_limit debe ser una cadena como '60/m', no {spec!r}")

    m = _FORMATO.match(spec)
    if not m:
        raise ValueError(
            f"rate_limit invalido: {spec!r}. El formato es "
            f"'<cantidad>/<periodo>', por ejemplo '60/m', '10/s' o '100/5m'."
        )

    cantidad, multiplo, unidad = m.groups()
    if int(cantidad) <= 0:
        raise ValueError(f"rate_limit invalido: {spec!r}. La cantidad debe ser > 0.")
    return float(cantidad), float(multiplo or 1) * UNIDADES[unidad.lower()]


class Cubo:
    """Token bucket. `consumir()` devuelve False cuando ya no queda margen."""

    __slots__ = ("capacidad", "por_segundo", "restante", "sello")

    def __init__(self, cantidad: float, periodo: float, burst: float | None = None):
        self.capacidad = float(burst if burst is not None else cantidad)
        self.por_segundo = cantidad / periodo
        self.restante = self.capacidad
        self.sello = time.monotonic()

    def consumir(self, coste: float = 1.0) -> bool:
        ahora = time.monotonic()
        self.restante = min(
            self.capacidad, self.restante + (ahora - self.sello) * self.por_segundo
        )
        self.sello = ahora
        if self.restante < coste:
            return False
        self.restante -= coste
        return True

    @property
    def espera(self) -> float:
        """Segundos hasta que vuelva a haber margen. Util para avisar al cliente."""
        if self.restante >= 1:
            return 0.0
        return (1 - self.restante) / self.por_segundo


def crear(spec=None, burst=None) -> Cubo | None:
    """Cubo para una ruta, o None si no hay limite configurado en ningun sitio."""
    if spec is None:
        from django.conf import settings

        conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
        spec = conf.get("RATE_LIMIT")
        burst = burst if burst is not None else conf.get("RATE_LIMIT_BURST")

    if not spec:
        return None
    cantidad, periodo = parsear(spec)
    return Cubo(cantidad, periodo, burst)
