/*!
 * django_socket - WebSocket client with automatic reconnection.
 *
 *   const sock = djangoSocket("/chat/general/");
 *   sock.on("message", (data) => render(data.text));
 *   sock.send({type: "message", text: "hi"});
 *
 * What it adds over a bare `new WebSocket(...)`:
 *   - reconnects with exponential backoff and jitter
 *   - does NOT reconnect when the server closed on purpose (4401 login
 *     required, 4404 no such route...): retrying there is an infinite loop
 *   - queues what you send while it is down and flushes it on return
 *   - JSON both ways, routed by `type` like the Events class in Python
 *   - stops retrying while the browser is offline (and optionally while the
 *     tab is hidden: {pauseWhenHidden: true})
 */
(function (global) {
  "use strict";

  // Closes that mean "do not try again".
  //   1000 the server closed cleanly            1008 policy violation
  //   4000-4999 decisions of your own app (login, route, invalid data)
  function isFinal(code) {
    return code === 1000 || code === 1008 || (code >= 4000 && code <= 4999);
  }

  function absoluteUrl(path) {
    if (/^wss?:\/\//.test(path)) return path;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + location.host + (path[0] === "/" ? path : "/" + path);
  }

  function djangoSocket(path, options) {
    var o = Object.assign(
      {
        key: "type",             // the field that routes, same as Events(key=)
        reconnect: true,
        minDelay: 500,           // first retry, in ms
        maxDelay: 15000,         // backoff ceiling
        maxRetries: Infinity,
        queue: true,             // hold sends while there is no connection
        maxQueue: 100,
        pauseWhenHidden: false,  // see the note in waitUntilReady()
        protocols: undefined,
        onOpen: null,            // (isReconnection) => {}
        onMessage: null,         // (data, event) => {}  for whatever did not match
        onClose: null,           // (event, willRetry) => {}
        onError: null,
        onRetry: null,           // (attempt, delayMs) => {}
        shouldReconnect: null,   // (event) => bool   to decide it yourself
      },
      options || {}
    );

    var url = absoluteUrl(path);
    var ws = null;
    var handlers = {};
    var queued = [];
    var attempts = 0;
    var timer = null;
    var closedByUs = false;
    var hasEverOpened = false;

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
        return queued.length;
      },
      on: on,
      off: off,
      send: send,
      close: close,
      reconnect: forceReconnect,
    };

    // ------------------------------------------------------------- handlers

    function on(type, fn) {
      (handlers[type] = handlers[type] || []).push(fn);
      return api;                       // chainable: .on(..).on(..)
    }

    function off(type, fn) {
      if (!handlers[type]) return api;
      if (!fn) delete handlers[type];
      else handlers[type] = handlers[type].filter(function (f) { return f !== fn; });
      return api;
    }

    function dispatch(data, event) {
      var type = data && typeof data === "object" ? data[o.key] : undefined;
      // "*" is a fallback, not a spy: it only runs when nobody else took the
      // message. That is the same semantics as the Events class in Python, and
      // having them differ on each side of the same protocol invites a bug.
      var list = handlers[type] && handlers[type].length
        ? handlers[type]
        : handlers["*"] || [];
      if (list.length) {
        list.slice().forEach(function (fn) { fn(data, event); });
      } else if (o.onMessage) {
        o.onMessage(data, event);
      }
    }

    // ----------------------------------------------------------------- send

    function send(data) {
      var body =
        typeof data === "string" || data instanceof Blob || data instanceof ArrayBuffer
          ? data
          : JSON.stringify(data);

      if (api.connected) {
        ws.send(body);
        return true;
      }
      if (o.queue) {
        if (queued.length >= o.maxQueue) queued.shift();   // drop the oldest
        queued.push(body);
      }
      return false;
    }

    function flushQueue() {
      var pending = queued;
      queued = [];
      pending.forEach(function (body) { ws.send(body); });
    }

    // ----------------------------------------------------------- life cycle

    function connect() {
      clearTimeout(timer);
      // Careful: `closedByUs` is NOT touched here. Resetting it on connect
      // revived sockets the application had closed on purpose: if the close
      // happened while we were waiting for the "online" event, the listener
      // called connect() when the network came back and the flag cleared
      // itself. forceReconnect() is what decides to reconnect; it clears it.
      try {
        ws = o.protocols ? new WebSocket(url, o.protocols) : new WebSocket(url);
      } catch (err) {
        if (o.onError) o.onError(err);
        scheduleRetry({ code: 1006, reason: String(err) });
        return;
      }

      ws.onopen = function () {
        var isReconnection = hasEverOpened;
        hasEverOpened = true;
        attempts = 0;
        flushQueue();
        if (o.onOpen) o.onOpen(isReconnection);
      };

      ws.onmessage = function (event) {
        var data = event.data;
        if (typeof data === "string") {
          try {
            data = JSON.parse(data);
          } catch (e) {
            /* not JSON: hand the text through as it came */
          }
        }
        dispatch(data, event);
      };

      ws.onerror = function (event) {
        if (o.onError) o.onError(event);
      };

      ws.onclose = function (event) {
        var willRetry = shouldRetry(event);
        if (o.onClose) o.onClose(event, willRetry);
        if (willRetry) scheduleRetry(event);
      };
    }

    function shouldRetry(event) {
      if (closedByUs || !o.reconnect) return false;
      if (attempts >= o.maxRetries) return false;
      if (o.shouldReconnect) return !!o.shouldReconnect(event);
      return !isFinal(event.code);
    }

    function scheduleRetry(event) {
      // Exponential backoff with jitter: without it, N clients that drop at
      // once all come back at the same instant and take the server down again.
      var base = Math.min(o.minDelay * Math.pow(2, attempts), o.maxDelay);
      var delay = Math.round(base * (0.5 + Math.random() * 0.5));
      attempts += 1;
      if (o.onRetry) o.onRetry(attempts, delay);

      timer = setTimeout(function () {
        if (mustWait()) {
          waitUntilReady();
          return;
        }
        connect();
      }, delay);
    }

    function mustWait() {
      // Offline there is nothing to try: wait for the "online" event.
      if (navigator.onLine === false) return true;
      // Pausing because the tab is hidden is NOT the default, deliberately. A
      // background chat that silently stops reconnecting is broken: you come
      // back and you have missed everything without a single warning. Turn it
      // on if your case tolerates falling behind: {pauseWhenHidden: true}.
      return o.pauseWhenHidden && document.visibilityState === "hidden";
    }

    var waiting = false;
    var stopWaiting = null;             // tears down the waiting listeners

    function waitUntilReady() {
      if (waiting) return;              // do not stack pairs of listeners
      waiting = true;

      function teardown() {
        waiting = false;
        stopWaiting = null;
        window.removeEventListener("online", wake);
        document.removeEventListener("visibilitychange", onVisible);
      }
      function wake() {
        if (!waiting) return;           // another event woke us first
        teardown();
        if (closedByUs) return;         // we were closed while waiting
        attempts = 0;                   // coming back is good news: retry now
        connect();
      }
      function onVisible() {
        if (!mustWait()) wake();
      }
      stopWaiting = teardown;
      window.addEventListener("online", wake);
      document.addEventListener("visibilitychange", onVisible);
    }

    function forceReconnect() {
      attempts = 0;
      closedByUs = false;               // reconnecting by hand cancels close()
      if (stopWaiting) stopWaiting();
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.close(1000);
      }
      connect();
      return api;
    }

    function close(code, reason) {
      closedByUs = true;
      clearTimeout(timer);
      // If we were parked waiting for the network to come back, those
      // listeners have to go: otherwise the "online" event a while from now
      // would reconnect a socket you already considered closed.
      if (stopWaiting) stopWaiting();
      if (ws) ws.close(code || 1000, reason || "");
      return api;
    }

    connect();
    return api;
  }

  djangoSocket.isFinal = isFinal;
  // 0.2.x name, kept so an upgrade breaks nobody. Goes away at 1.0.
  djangoSocket.esDefinitivo = isFinal;

  global.djangoSocket = djangoSocket;
  if (typeof module === "object" && module.exports) module.exports = djangoSocket;
})(typeof window !== "undefined" ? window : this);
