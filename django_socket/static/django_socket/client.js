/*!
 * django_socket - cliente con reconexion automatica.
 *
 *   const sock = djangoSocket("/chat/general/");
 *   sock.on("mensaje", (datos) => pintar(datos.texto));
 *   sock.send({type: "mensaje", texto: "hola"});
 *
 * Lo que aporta frente a `new WebSocket(...)` a pelo:
 *   - reconecta con backoff exponencial y jitter
 *   - NO reconecta cuando el servidor cierra a proposito (4401 falta login,
 *     4404 ruta inexistente...): ahi reintentar es un bucle infinito
 *   - encola lo que envies mientras esta caido y lo suelta al volver
 *   - JSON en los dos sentidos, y enrutado por `type` como el Events de Python
 *   - deja de reintentar si el navegador esta sin red (y opcionalmente
 *     si la pestaña esta oculta: {pauseWhenHidden: true})
 */
(function (global) {
  "use strict";

  // Cierres que significan "no lo vuelvas a intentar".
  //   1000 el servidor cerro limpiamente        1008 violacion de politica
  //   4000-4999 decisiones de tu aplicacion (login, ruta, datos invalidos)
  function esDefinitivo(code) {
    return code === 1000 || code === 1008 || (code >= 4000 && code <= 4999);
  }

  function urlAbsoluta(ruta) {
    if (/^wss?:\/\//.test(ruta)) return ruta;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + location.host + (ruta[0] === "/" ? ruta : "/" + ruta);
  }

  function djangoSocket(ruta, opciones) {
    var o = Object.assign(
      {
        key: "type",             // el campo que enruta, igual que Events(key=)
        reconnect: true,
        minDelay: 500,           // primer reintento, en ms
        maxDelay: 15000,         // tope del backoff
        maxRetries: Infinity,
        queue: true,             // guardar los envios mientras no hay conexion
        maxQueue: 100,
        pauseWhenHidden: false,  // ver la nota en esperarAEstarListo()
        protocols: undefined,
        onOpen: null,            // (esReconexion) => {}
        onMessage: null,         // (datos, evento) => {}  para lo que no case
        onClose: null,           // (evento, vaAReintentar) => {}
        onError: null,
        onRetry: null,           // (intento, esperaMs) => {}
        shouldReconnect: null,   // (evento) => bool   para decidirlo tu
      },
      opciones || {}
    );

    var url = urlAbsoluta(ruta);
    var ws = null;
    var handlers = {};
    var pendientes = [];
    var intentos = 0;
    var temporizador = null;
    var cerradoPorNosotros = false;
    var haAbiertoAlgunaVez = false;

    var api = {
      get readyState() {
        return ws ? ws.readyState : WebSocket.CLOSED;
      },
      get connected() {
        return !!ws && ws.readyState === WebSocket.OPEN;
      },
      get url() {
        return url;
      },
      get pending() {
        return pendientes.length;
      },
      on: on,
      off: off,
      send: send,
      close: close,
      reconnect: forzarReconexion,
    };

    // ------------------------------------------------------------- handlers

    function on(tipo, fn) {
      (handlers[tipo] = handlers[tipo] || []).push(fn);
      return api;                       // encadenable: .on(..).on(..)
    }

    function off(tipo, fn) {
      if (!handlers[tipo]) return api;
      if (!fn) delete handlers[tipo];
      else handlers[tipo] = handlers[tipo].filter(function (f) { return f !== fn; });
      return api;
    }

    function despachar(datos, evento) {
      var tipo = datos && typeof datos === "object" ? datos[o.key] : undefined;
      // "*" es un respaldo, no un espia: solo corre cuando nadie mas cogio el
      // mensaje. Es la misma semantica que el Events de Python, y tenerlas
      // distintas a cada lado del mismo protocolo es pedir un bug.
      var lista = handlers[tipo] && handlers[tipo].length
        ? handlers[tipo]
        : handlers["*"] || [];
      if (lista.length) {
        lista.slice().forEach(function (fn) { fn(datos, evento); });
      } else if (o.onMessage) {
        o.onMessage(datos, evento);
      }
    }

    // --------------------------------------------------------------- enviar

    function send(datos) {
      var cuerpo =
        typeof datos === "string" || datos instanceof Blob || datos instanceof ArrayBuffer
          ? datos
          : JSON.stringify(datos);

      if (api.connected) {
        ws.send(cuerpo);
        return true;
      }
      if (o.queue) {
        if (pendientes.length >= o.maxQueue) pendientes.shift();   // tira el mas viejo
        pendientes.push(cuerpo);
      }
      return false;
    }

    function vaciarCola() {
      var cola = pendientes;
      pendientes = [];
      cola.forEach(function (cuerpo) { ws.send(cuerpo); });
    }

    // ---------------------------------------------------------- ciclo de vida

    function conectar() {
      clearTimeout(temporizador);
      // Ojo: aqui NO se toca `cerradoPorNosotros`. Resetearlo al conectar
      // resucitaba sockets que la aplicacion habia cerrado a proposito: si el
      // cierre ocurria mientras esperabamos al evento "online", al volver la
      // red el listener llamaba a conectar() y el flag se limpiaba solo.
      // Quien decide reconectar es forzarReconexion(); ahi si se limpia.
      try {
        ws = o.protocols ? new WebSocket(url, o.protocols) : new WebSocket(url);
      } catch (err) {
        if (o.onError) o.onError(err);
        programarReintento({ code: 1006, reason: String(err) });
        return;
      }

      ws.onopen = function () {
        var esReconexion = haAbiertoAlgunaVez;
        haAbiertoAlgunaVez = true;
        intentos = 0;
        vaciarCola();
        if (o.onOpen) o.onOpen(esReconexion);
      };

      ws.onmessage = function (evento) {
        var datos = evento.data;
        if (typeof datos === "string") {
          try {
            datos = JSON.parse(datos);
          } catch (e) {
            /* no era JSON: se pasa el texto tal cual */
          }
        }
        despachar(datos, evento);
      };

      ws.onerror = function (evento) {
        if (o.onError) o.onError(evento);
      };

      ws.onclose = function (evento) {
        var reintentara = decideReintentar(evento);
        if (o.onClose) o.onClose(evento, reintentara);
        if (reintentara) programarReintento(evento);
      };
    }

    function decideReintentar(evento) {
      if (cerradoPorNosotros || !o.reconnect) return false;
      if (intentos >= o.maxRetries) return false;
      if (o.shouldReconnect) return !!o.shouldReconnect(evento);
      return !esDefinitivo(evento.code);
    }

    function programarReintento(evento) {
      // Backoff exponencial con jitter: sin el, N clientes que se caen a la vez
      // vuelven todos en el mismo instante y tumban el servidor otra vez.
      var base = Math.min(o.minDelay * Math.pow(2, intentos), o.maxDelay);
      var espera = Math.round(base * (0.5 + Math.random() * 0.5));
      intentos += 1;
      if (o.onRetry) o.onRetry(intentos, espera);

      temporizador = setTimeout(function () {
        if (debeEsperar()) {
          esperarAEstarListo();
          return;
        }
        conectar();
      }, espera);
    }

    function debeEsperar() {
      // Sin red no hay nada que intentar: espera al evento "online".
      if (navigator.onLine === false) return true;
      // Pausar por estar la pestaña oculta NO es el defecto a proposito. Un
      // chat en segundo plano que deja de reconectar en silencio esta roto:
      // vuelves y te has perdido todo sin un solo aviso. Actívalo tu si tu
      // caso tolera quedarse atras: {pauseWhenHidden: true}.
      return o.pauseWhenHidden && document.visibilityState === "hidden";
    }

    var esperando = false;
    var dejarDeEsperar = null;          // desmonta los listeners de la espera

    function esperarAEstarListo() {
      if (esperando) return;            // no acumules parejas de listeners
      esperando = true;

      function quitar() {
        esperando = false;
        dejarDeEsperar = null;
        window.removeEventListener("online", despertar);
        document.removeEventListener("visibilitychange", alVerse);
      }
      function despertar() {
        if (!esperando) return;         // otro evento nos desperto primero
        quitar();
        if (cerradoPorNosotros) return; // nos cerraron mientras esperabamos
        intentos = 0;                   // volver es buena señal: reintenta ya
        conectar();
      }
      function alVerse() {
        if (!debeEsperar()) despertar();
      }
      dejarDeEsperar = quitar;
      window.addEventListener("online", despertar);
      document.addEventListener("visibilitychange", alVerse);
    }

    function forzarReconexion() {
      intentos = 0;
      cerradoPorNosotros = false;       // reconectar a mano cancela el close()
      if (dejarDeEsperar) dejarDeEsperar();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.close(1000);
      }
      conectar();
      return api;
    }

    function close(code, reason) {
      cerradoPorNosotros = true;
      clearTimeout(temporizador);
      // Si estabamos aparcados esperando a que vuelva la red, hay que
      // desmontar esos listeners: si no, el "online" de dentro de un rato
      // reconectaria un socket que ya diste por cerrado.
      if (dejarDeEsperar) dejarDeEsperar();
      if (ws) ws.close(code || 1000, reason || "");
      return api;
    }

    conectar();
    return api;
  }

  djangoSocket.esDefinitivo = esDefinitivo;

  global.djangoSocket = djangoSocket;
  if (typeof module === "object" && module.exports) module.exports = djangoSocket;
})(typeof window !== "undefined" ? window : this);
