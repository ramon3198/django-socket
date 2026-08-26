"""Un servidor RESP minimo con SUBSCRIBE/PUBLISH, para probar RedisLayer sin Redis.

No es un Redis: no persiste, no tiene claves, no tiene mas comandos que los que
usa la capa. Lo que si es real es todo lo demas -- el cliente `redis-py`, el
socket TCP, el parseo del protocolo y el reparto entre conexiones distintas.
Sirve para validar la logica de RedisLayer de punta a punta; no sustituye a una
prueba contra un Redis de verdad.
"""

from __future__ import annotations

import asyncio

CRLF = b"\r\n"


class MiniRedis:
    def __init__(self):
        self._canales: dict[bytes, set[asyncio.StreamWriter]] = {}
        self._server: asyncio.AbstractServer | None = None
        self.port: int | None = None
        self.publicados: list[tuple[bytes, bytes]] = []

    @property
    def url(self) -> str:
        return f"redis://127.0.0.1:{self.port}/0"

    async def start(self, host: str = "127.0.0.1") -> "MiniRedis":
        self._server = await asyncio.start_server(self._cliente, host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # ----------------------------------------------------------- protocolo

    async def _cliente(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                partes = await self._leer_comando(reader)
                if partes is None:
                    break
                await self._ejecutar(partes, writer)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        finally:
            for suscriptores in self._canales.values():
                suscriptores.discard(writer)
            writer.close()

    async def _leer_comando(self, reader) -> list[bytes] | None:
        linea = await reader.readline()
        if not linea:
            return None
        if not linea.startswith(b"*"):                  # comando inline
            return linea.strip().split()
        partes = []
        for _ in range(int(linea[1:].strip())):
            cabecera = await reader.readline()          # $<len>
            largo = int(cabecera[1:].strip())
            partes.append(await reader.readexactly(largo))
            await reader.readexactly(2)                 # CRLF
        return partes

    async def _ejecutar(self, partes: list[bytes], writer) -> None:
        cmd = partes[0].upper()

        if cmd == b"SUBSCRIBE":
            for i, canal in enumerate(partes[1:], start=1):
                self._canales.setdefault(canal, set()).add(writer)
                writer.write(self._array([b"subscribe", canal, i]))

        elif cmd == b"UNSUBSCRIBE":
            canales = partes[1:] or list(self._canales)
            for canal in canales:
                self._canales.get(canal, set()).discard(writer)
                writer.write(self._array([b"unsubscribe", canal, 0]))

        elif cmd == b"PUBLISH":
            canal, carga = partes[1], partes[2]
            self.publicados.append((canal, carga))
            destinos = list(self._canales.get(canal, ()))
            for destino in destinos:
                destino.write(self._array([b"message", canal, carga]))
                await destino.drain()
            writer.write(b":%d%s" % (len(destinos), CRLF))

        elif cmd == b"PING":
            writer.write(b"+PONG" + CRLF)

        else:
            # HELLO, CLIENT SETINFO y demas cortesias del cliente: no importan.
            writer.write(b"+OK" + CRLF)

    @staticmethod
    def _array(items) -> bytes:
        salida = b"*%d%s" % (len(items), CRLF)
        for item in items:
            if isinstance(item, int):
                salida += b":%d%s" % (item, CRLF)
            else:
                salida += b"$%d%s%s%s" % (len(item), CRLF, item, CRLF)
        return salida
