/**
 * Navegador de mentira para probar client.js sin dependencias.
 *
 * Node trae test runner y timers falsos desde la 20, asi que no hace falta
 * ni `npm install`: `node --test tests/js/` y ya.
 */

class FakeWS {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instancias = [];

  static get ultima() {
    return FakeWS.instancias[FakeWS.instancias.length - 1];
  }

  static reset() {
    FakeWS.instancias = [];
  }

  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = FakeWS.CONNECTING;
    this.enviados = [];
    this.cerradoCon = null;
    FakeWS.instancias.push(this);
  }

  send(cuerpo) {
    this.enviados.push(cuerpo);
  }

  close(code, reason) {
    this.cerradoCon = { code: code || 1000, reason: reason || "" };
    this.caer(code || 1000, reason || "");
  }

  // ---- lo que dirige el test ----

  abrir() {
    this.readyState = FakeWS.OPEN;
    if (this.onopen) this.onopen({});
  }

  recibir(data) {
    if (this.onmessage) this.onmessage({ data });
  }

  caer(code = 1006, reason = "") {
    if (this.readyState === FakeWS.CLOSED) return;
    this.readyState = FakeWS.CLOSED;
    if (this.onclose) this.onclose({ code, reason });
  }
}

class Oyentes {
  constructor() {
    this.mapa = new Map();
  }
  addEventListener(tipo, fn) {
    if (!this.mapa.has(tipo)) this.mapa.set(tipo, new Set());
    this.mapa.get(tipo).add(fn);
  }
  removeEventListener(tipo, fn) {
    const s = this.mapa.get(tipo);
    if (s) s.delete(fn);
  }
  disparar(tipo) {
    for (const fn of [...(this.mapa.get(tipo) || [])]) fn({ type: tipo });
  }
  get cuantos() {
    return [...this.mapa.values()].reduce((n, s) => n + s.size, 0);
  }
}

/** Deja el entorno limpio y devuelve los mandos. */
function montar({ protocolo = "http:", host = "ejemplo.com:8000" } = {}) {
  FakeWS.reset();

  const ventana = new Oyentes();
  const documento = new Oyentes();
  documento.visibilityState = "visible";

  globalThis.WebSocket = FakeWS;
  globalThis.location = { protocol: protocolo, host };
  globalThis.navigator = { onLine: true };
  globalThis.window = ventana;
  globalThis.document = documento;

  // Jitter determinista: factor 0.5 + 0.5*0.5 = 0.75 sobre la base.
  Math.random = () => 0.5;

  return { ventana, documento, FakeWS };
}

const JITTER = 0.75;

module.exports = { FakeWS, Oyentes, montar, JITTER };
