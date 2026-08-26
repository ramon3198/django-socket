/**
 * Tests del cliente JS.  Ejecutar:  node --test tests/js/
 *
 * Sin dependencias: test runner y timers falsos vienen con Node.
 */

const { test, beforeEach, mock } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { FakeWS, montar, JITTER } = require("./entorno.js");

const RUTA = path.join(
  __dirname, "..", "..", "django_socket", "static", "django_socket", "client.js"
);

let djangoSocket;
let mandos;

beforeEach(() => {
  mandos = montar();
  delete require.cache[RUTA];
  djangoSocket = require(RUTA);
});

// --------------------------------------------------------------------- URL

test("compone ws:// desde una pagina http", () => {
  const s = djangoSocket("/chat/uno/");
  assert.equal(s.url, "ws://ejemplo.com:8000/chat/uno/");
});

test("compone wss:// desde una pagina https", () => {
  montar({ protocolo: "https:", host: "seguro.com" });
  delete require.cache[RUTA];
  const s = require(RUTA)("/chat/");
  assert.equal(s.url, "wss://seguro.com/chat/");
});

test("una URL absoluta se respeta", () => {
  const s = djangoSocket("wss://otro.host/x/");
  assert.equal(s.url, "wss://otro.host/x/");
});

test("añade la barra inicial si falta", () => {
  assert.equal(djangoSocket("chat/").url, "ws://ejemplo.com:8000/chat/");
});

// ------------------------------------------------------------------ enrutado

test("despacha por el campo type", () => {
  const vistos = [];
  const s = djangoSocket("/x/");
  s.on("mensaje", (d) => vistos.push(d.texto));
  FakeWS.ultima.abrir();

  FakeWS.ultima.recibir(JSON.stringify({ type: "mensaje", texto: "hola" }));
  assert.deepEqual(vistos, ["hola"]);
});

test("el comodin recoge lo que no case", () => {
  const vistos = [];
  const s = djangoSocket("/x/");
  s.on("conocido", () => vistos.push("conocido"));
  s.on("*", (d) => vistos.push("resto:" + d.type));
  FakeWS.ultima.abrir();

  FakeWS.ultima.recibir(JSON.stringify({ type: "conocido" }));
  FakeWS.ultima.recibir(JSON.stringify({ type: "raro" }));
  assert.deepEqual(vistos, ["conocido", "resto:raro"]);
});

test("onMessage recoge lo que ningun handler quiso", () => {
  const vistos = [];
  djangoSocket("/x/", { onMessage: (d) => vistos.push(d) });
  FakeWS.ultima.abrir();
  FakeWS.ultima.recibir(JSON.stringify({ type: "nadie" }));
  assert.deepEqual(vistos, [{ type: "nadie" }]);
});

test("el campo que enruta se puede cambiar", () => {
  const vistos = [];
  const s = djangoSocket("/x/", { key: "accion" });
  s.on("borrar", (d) => vistos.push(d.id));
  FakeWS.ultima.abrir();
  FakeWS.ultima.recibir(JSON.stringify({ accion: "borrar", id: 7 }));
  assert.deepEqual(vistos, [7]);
});

test("lo que no es JSON llega tal cual", () => {
  const vistos = [];
  djangoSocket("/x/", { onMessage: (d) => vistos.push(d) });
  FakeWS.ultima.abrir();
  FakeWS.ultima.recibir("texto plano");
  assert.deepEqual(vistos, ["texto plano"]);
});

test("off quita un handler", () => {
  const vistos = [];
  const fn = () => vistos.push(1);
  const s = djangoSocket("/x/");
  s.on("t", fn);
  FakeWS.ultima.abrir();
  FakeWS.ultima.recibir(JSON.stringify({ type: "t" }));
  s.off("t", fn);
  FakeWS.ultima.recibir(JSON.stringify({ type: "t" }));
  assert.equal(vistos.length, 1);
});

// -------------------------------------------------------------------- envio

test("un objeto se manda como JSON y un string tal cual", () => {
  const s = djangoSocket("/x/");
  FakeWS.ultima.abrir();
  s.send({ type: "m", a: 1 });
  s.send("crudo");
  assert.deepEqual(FakeWS.ultima.enviados, ['{"type":"m","a":1}', "crudo"]);
});

test("send devuelve true si salio y false si se encolo", () => {
  const s = djangoSocket("/x/");
  assert.equal(s.send("antes de abrir"), false);
  assert.equal(s.pending, 1);
  FakeWS.ultima.abrir();
  assert.equal(s.send("ya abierto"), true);
});

test("la cola se vacia al conectar, en orden", () => {
  const s = djangoSocket("/x/");
  s.send("uno");
  s.send("dos");
  FakeWS.ultima.abrir();
  assert.deepEqual(FakeWS.ultima.enviados, ["uno", "dos"]);
  assert.equal(s.pending, 0);
});

test("maxQueue tira lo mas viejo", () => {
  const s = djangoSocket("/x/", { maxQueue: 2 });
  s.send("uno");
  s.send("dos");
  s.send("tres");
  FakeWS.ultima.abrir();
  assert.deepEqual(FakeWS.ultima.enviados, ["dos", "tres"]);
});

test("con queue:false no se guarda nada", () => {
  const s = djangoSocket("/x/", { queue: false });
  s.send("se pierde");
  FakeWS.ultima.abrir();
  assert.deepEqual(FakeWS.ultima.enviados, []);
});

// ------------------------------------------------- no reintentar sin sentido

test("un cierre 4xxx de la aplicacion NO se reintenta", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const cierres = [];
  djangoSocket("/x/", { minDelay: 10, onClose: (e, r) => cierres.push(r) });
  FakeWS.ultima.abrir();

  FakeWS.ultima.caer(4401, "Authentication required");
  mock.timers.tick(100000);

  assert.deepEqual(cierres, [false]);
  assert.equal(FakeWS.instancias.length, 1, "no debe haber reconectado");
  mock.timers.reset();
});

test("1000 y 1008 tampoco", () => {
  for (const code of [1000, 1008]) {
    montar();
    delete require.cache[RUTA];
    const ds = require(RUTA);
    mock.timers.enable({ apis: ["setTimeout"] });
    ds("/x/", { minDelay: 10 });
    FakeWS.ultima.abrir();
    FakeWS.ultima.caer(code);
    mock.timers.tick(100000);
    assert.equal(FakeWS.instancias.length, 1, `reconecto con ${code}`);
    mock.timers.reset();
  }
});

test("un corte de red (1006) si se reintenta", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  djangoSocket("/x/", { minDelay: 100 });
  FakeWS.ultima.abrir();
  FakeWS.ultima.caer(1006);

  mock.timers.tick(100 * JITTER);
  assert.equal(FakeWS.instancias.length, 2);
  mock.timers.reset();
});

test("esDefinitivo clasifica los codigos", () => {
  for (const c of [1000, 1008, 4000, 4401, 4999]) {
    assert.equal(djangoSocket.esDefinitivo(c), true, `${c} deberia ser definitivo`);
  }
  for (const c of [1001, 1006, 1011, 1012, 3000, 5000]) {
    assert.equal(djangoSocket.esDefinitivo(c), false, `${c} deberia reintentarse`);
  }
});

test("shouldReconnect manda sobre el criterio por defecto", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  djangoSocket("/x/", { minDelay: 10, shouldReconnect: () => true });
  FakeWS.ultima.abrir();
  FakeWS.ultima.caer(4401);            // definitivo, pero lo forzamos
  mock.timers.tick(1000);
  assert.equal(FakeWS.instancias.length, 2);
  mock.timers.reset();
});

// ------------------------------------------------------------------ backoff

test("el backoff crece exponencialmente y topa en maxDelay", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const esperas = [];
  djangoSocket("/x/", {
    minDelay: 100, maxDelay: 1000,
    onRetry: (n, ms) => esperas.push(ms),
  });

  for (let i = 0; i < 6; i++) {
    FakeWS.ultima.caer(1006);
    mock.timers.tick(100000);
  }

  // base = min(100 * 2^n, 1000), por el jitter fijo de 0.75
  assert.deepEqual(esperas, [75, 150, 300, 600, 750, 750]);
  mock.timers.reset();
});

test("el backoff se reinicia al conectar de nuevo", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const esperas = [];
  djangoSocket("/x/", { minDelay: 100, onRetry: (n, ms) => esperas.push(ms) });

  FakeWS.ultima.caer(1006);
  mock.timers.tick(100000);
  FakeWS.ultima.caer(1006);
  mock.timers.tick(100000);
  assert.deepEqual(esperas, [75, 150]);

  FakeWS.ultima.abrir();               // reconectado
  FakeWS.ultima.caer(1006);
  mock.timers.tick(100000);
  assert.equal(esperas[2], 75, "no volvio al principio");
  mock.timers.reset();
});

test("maxRetries corta", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  djangoSocket("/x/", { minDelay: 10, maxRetries: 2 });
  for (let i = 0; i < 5; i++) {
    FakeWS.ultima.caer(1006);
    mock.timers.tick(100000);
  }
  assert.equal(FakeWS.instancias.length, 3, "1 inicial + 2 reintentos");
  mock.timers.reset();
});

test("reconnect:false no reintenta nunca", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  djangoSocket("/x/", { reconnect: false, minDelay: 10 });
  FakeWS.ultima.caer(1006);
  mock.timers.tick(100000);
  assert.equal(FakeWS.instancias.length, 1);
  mock.timers.reset();
});

test("cerrar a mano no dispara reconexion", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const s = djangoSocket("/x/", { minDelay: 10 });
  FakeWS.ultima.abrir();
  s.close();
  mock.timers.tick(100000);
  assert.equal(FakeWS.instancias.length, 1);
  mock.timers.reset();
});

// ------------------------------------------------------------ red y visibilidad

test("sin red no se gasta el intento; vuelve con el evento online", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  djangoSocket("/x/", { minDelay: 100 });
  FakeWS.ultima.abrir();

  globalThis.navigator.onLine = false;
  FakeWS.ultima.caer(1006);
  mock.timers.tick(100000);
  assert.equal(FakeWS.instancias.length, 1, "reintento estando sin red");

  globalThis.navigator.onLine = true;
  mandos.ventana.disparar("online");
  assert.equal(FakeWS.instancias.length, 2, "no volvio al recuperar la red");
  mock.timers.reset();
});

test("la pestaña oculta NO frena los reintentos por defecto", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  djangoSocket("/x/", { minDelay: 100 });
  FakeWS.ultima.abrir();

  mandos.documento.visibilityState = "hidden";
  FakeWS.ultima.caer(1006);
  mock.timers.tick(100 * JITTER);

  assert.equal(FakeWS.instancias.length, 2,
    "un chat en segundo plano debe seguir reconectando");
  mock.timers.reset();
});

test("pauseWhenHidden si la frena, y despierta al volver a verse", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  djangoSocket("/x/", { minDelay: 100, pauseWhenHidden: true });
  FakeWS.ultima.abrir();

  mandos.documento.visibilityState = "hidden";
  FakeWS.ultima.caer(1006);
  mock.timers.tick(100000);
  assert.equal(FakeWS.instancias.length, 1, "reintento con la pestaña oculta");

  mandos.documento.visibilityState = "visible";
  mandos.documento.disparar("visibilitychange");
  assert.equal(FakeWS.instancias.length, 2);
  mock.timers.reset();
});

test("esperar no acumula listeners", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  djangoSocket("/x/", { minDelay: 10 });
  globalThis.navigator.onLine = false;

  for (let i = 0; i < 5; i++) {
    FakeWS.ultima.caer(1006);
    mock.timers.tick(100000);
  }

  assert.ok(mandos.ventana.cuantos <= 1, `quedaron ${mandos.ventana.cuantos} listeners`);
  assert.ok(mandos.documento.cuantos <= 1);
  mock.timers.reset();
});

// ------------------------------------------------------------------ estado

test("onOpen distingue la primera vez de una reconexion", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const aperturas = [];
  djangoSocket("/x/", { minDelay: 10, onOpen: (r) => aperturas.push(r) });

  FakeWS.ultima.abrir();
  FakeWS.ultima.caer(1006);
  mock.timers.tick(100000);
  FakeWS.ultima.abrir();

  assert.deepEqual(aperturas, [false, true]);
  mock.timers.reset();
});

test("lo escrito durante el corte sale al reconectar", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  const s = djangoSocket("/x/", { minDelay: 100 });
  FakeWS.ultima.abrir();
  FakeWS.ultima.caer(1006);

  s.send("durante el corte");
  assert.equal(s.pending, 1);

  mock.timers.tick(100 * JITTER);
  FakeWS.ultima.abrir();

  assert.deepEqual(FakeWS.ultima.enviados, ["durante el corte"]);
  assert.equal(s.pending, 0);
  mock.timers.reset();
});

test("connected refleja el estado real", () => {
  const s = djangoSocket("/x/");
  assert.equal(s.connected, false);
  FakeWS.ultima.abrir();
  assert.equal(s.connected, true);
  FakeWS.ultima.caer(1000);
  assert.equal(s.connected, false);
});

test("reconnect() fuerza una conexion nueva", () => {
  const s = djangoSocket("/x/");
  FakeWS.ultima.abrir();
  s.reconnect();
  assert.ok(FakeWS.instancias.length >= 2);
});

test("los subprotocolos se pasan al WebSocket", () => {
  djangoSocket("/x/", { protocols: ["graphql-ws"] });
  assert.deepEqual(FakeWS.ultima.protocols, ["graphql-ws"]);
});

test("on() encadena", () => {
  const s = djangoSocket("/x/");
  assert.equal(s.on("a", () => {}).on("b", () => {}), s);
});
