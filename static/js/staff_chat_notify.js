/* Site-wide staff chat notifications.
 *
 * Polls the same endpoint the chat desk uses. A new chat request shows a
 * toast with Accept / Decline / Minimize; Accept opens a small floating chat
 * window right on the current page instead of sending the admin to the desk.
 * State (which toasts were minimized, which chats this browser has open) is
 * kept in sessionStorage so it survives navigating between staff pages but
 * resets once the tab is closed.
 */
(function () {
  "use strict";

  var root = document.getElementById("staff-chat-notify");
  if (!root) return;

  var urls = {
    live: root.dataset.liveUrl,
    accept: root.dataset.acceptUrl,
    reject: root.dataset.rejectUrl,
    close: root.dataset.closeUrl,
    send: root.dataset.sendUrl
  };

  function csrf() {
    var field = root.querySelector("[name=csrfmiddlewaretoken]");
    return field ? field.value : "";
  }

  // Templated URLs look like "/chat/desk/0/accept/" -- the placeholder "0" is
  // a path segment in the middle, so match it as "/0/", not end-anchored.
  function urlFor(template, id) {
    return template.replace("/0/", "/" + id + "/");
  }

  function post(target, data) {
    return fetch(target, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": csrf(),
        "X-Requested-With": "XMLHttpRequest"
      },
      credentials: "same-origin",
      body: new URLSearchParams(data || {}).toString()
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok) throw new Error(payload.error || "Something went wrong.");
        return payload;
      });
    });
  }

  function loadIds(key) {
    try {
      return new Set(JSON.parse(sessionStorage.getItem(key) || "[]"));
    } catch (e) {
      return new Set();
    }
  }
  function saveIds(key, set) {
    try {
      sessionStorage.setItem(key, JSON.stringify(Array.from(set)));
    } catch (e) {
      /* storage unavailable (private mode, quota) -- state just resets */
    }
  }

  var MINIMIZED_KEY = "staffChatNotify.minimized";
  var OPEN_KEY = "staffChatNotify.open";
  var COLLAPSED_KEY = "staffChatNotify.collapsed";

  var minimized = loadIds(MINIMIZED_KEY);
  var openConversations = loadIds(OPEN_KEY);
  var collapsed = loadIds(COLLAPSED_KEY);
  var lastRendered = {}; // conversation id -> highest message id already shown

  var toasts = document.createElement("div");
  toasts.className = "staff-chat-toasts";
  var minis = document.createElement("div");
  minis.className = "staff-chat-minis";
  root.appendChild(toasts);
  root.appendChild(minis);

  function waitLabel(seconds) {
    if (seconds < 60) return seconds + "s";
    return Math.floor(seconds / 60) + " min";
  }

  function removeToast(id) {
    var el = toasts.querySelector('.chat-toast[data-id="' + id + '"]');
    if (el) el.remove();
  }

  function renderToast(item) {
    var el = toasts.querySelector('.chat-toast[data-id="' + item.id + '"]');
    if (!el) {
      el = document.createElement("div");
      el.className = "chat-toast";
      el.dataset.id = item.id;

      var head = document.createElement("div");
      head.className = "chat-toast-head";
      var name = document.createElement("strong");
      name.textContent = item.name;
      head.appendChild(name);
      var wait = document.createElement("span");
      wait.className = "chat-toast-wait";
      head.appendChild(wait);
      el.appendChild(head);

      if (item.opened_from) {
        var meta = document.createElement("div");
        meta.className = "chat-toast-meta";
        meta.textContent = "from " + item.opened_from;
        el.appendChild(meta);
      }
      if (item.last_message) {
        var quote = document.createElement("div");
        quote.className = "chat-toast-quote";
        quote.textContent = "“" + item.last_message + "”";
        el.appendChild(quote);
      }

      var actions = document.createElement("div");
      actions.className = "chat-toast-actions";

      var accept = document.createElement("button");
      accept.type = "button";
      accept.className = "btn btn-sm";
      accept.textContent = "Accept";
      accept.addEventListener("click", function () {
        accept.disabled = true;
        post(urlFor(urls.accept, item.id))
          .then(function () {
            openConversations.add(String(item.id));
            saveIds(OPEN_KEY, openConversations);
            removeToast(item.id);
            renderMini({ id: item.id, name: item.name, messages: item.messages || [] });
            refresh();
          })
          .catch(function () {
            accept.disabled = false;
            removeToast(item.id); // most likely someone else already answered it
          });
      });

      var decline = document.createElement("button");
      decline.type = "button";
      decline.className = "btn btn-sm btn-ghost";
      decline.textContent = "Decline";
      decline.addEventListener("click", function () {
        decline.disabled = true;
        post(urlFor(urls.reject, item.id))
          .then(function () { removeToast(item.id); })
          .catch(function () { removeToast(item.id); });
      });

      var minimize = document.createElement("button");
      minimize.type = "button";
      minimize.className = "btn btn-sm btn-ghost btn-min";
      minimize.textContent = "Minimize";
      minimize.addEventListener("click", function () {
        minimized.add(String(item.id));
        saveIds(MINIMIZED_KEY, minimized);
        removeToast(item.id);
      });

      actions.appendChild(accept);
      actions.appendChild(decline);
      actions.appendChild(minimize);
      el.appendChild(actions);
      toasts.appendChild(el);
    }
    el.querySelector(".chat-toast-wait").textContent = "waiting " + waitLabel(item.waiting_seconds);
  }

  function appendMessage(log, message) {
    var row = document.createElement("div");
    row.className = "chat-msg chat-msg-" + message.sender_type;
    if (message.sender_type !== "system" && message.sender_name) {
      var who = document.createElement("span");
      who.className = "chat-msg-who";
      who.textContent = message.sender_name;
      row.appendChild(who);
    }
    var text = document.createElement("div");
    text.className = "chat-msg-text";
    text.textContent = message.text; // textContent: never inject HTML from a chat
    row.appendChild(text);
    var time = document.createElement("time");
    time.className = "chat-msg-time";
    time.textContent = message.time || "";
    row.appendChild(time);
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
  }

  function removeMini(id) {
    var el = minis.querySelector('.chat-mini[data-id="' + id + '"]');
    if (el) el.remove();
    delete lastRendered[id];
    openConversations.delete(String(id));
    saveIds(OPEN_KEY, openConversations);
    collapsed.delete(String(id));
    saveIds(COLLAPSED_KEY, collapsed);
  }

  function renderMini(item) {
    var el = minis.querySelector('.chat-mini[data-id="' + item.id + '"]');
    if (!el) {
      el = document.createElement("div");
      el.className = "chat-mini";
      if (collapsed.has(String(item.id))) el.classList.add("is-collapsed");
      el.dataset.id = item.id;

      var head = document.createElement("div");
      head.className = "chat-mini-head";
      var name = document.createElement("strong");
      name.textContent = item.name;
      head.appendChild(name);

      var unread = document.createElement("span");
      unread.className = "chat-mini-unread";
      unread.hidden = true;
      unread.textContent = "0";
      head.appendChild(unread);

      var actionsWrap = document.createElement("div");
      actionsWrap.className = "chat-mini-actions";
      var end = document.createElement("button");
      end.type = "button";
      end.title = "End chat";
      end.textContent = "✕";
      end.addEventListener("click", function (event) {
        event.stopPropagation();
        post(urlFor(urls.close, item.id)).then(function () {
          removeMini(item.id);
        });
      });
      actionsWrap.appendChild(end);
      head.appendChild(actionsWrap);

      head.addEventListener("click", function () {
        el.classList.toggle("is-collapsed");
        if (el.classList.contains("is-collapsed")) {
          collapsed.add(String(item.id));
        } else {
          collapsed.delete(String(item.id));
          unread.hidden = true;
          unread.textContent = "0";
        }
        saveIds(COLLAPSED_KEY, collapsed);
      });

      el.appendChild(head);

      var body = document.createElement("div");
      body.className = "chat-mini-body";
      var log = document.createElement("div");
      log.className = "chat-log";
      body.appendChild(log);

      var form = document.createElement("form");
      form.className = "chat-composer";
      var input = document.createElement("textarea");
      input.className = "field-input";
      input.rows = 1;
      input.placeholder = "Reply…";
      var sendBtn = document.createElement("button");
      sendBtn.type = "submit";
      sendBtn.className = "btn btn-sm";
      sendBtn.textContent = "Send";
      form.appendChild(input);
      form.appendChild(sendBtn);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var text = input.value.trim();
        if (!text) return;
        input.value = "";
        post(urlFor(urls.send, item.id), { text: text })
          .then(function (payload) {
            lastRendered[item.id] = Math.max(lastRendered[item.id] || 0, payload.message.id);
            appendMessage(log, payload.message);
          })
          .catch(function () { input.value = text; }); // hand the message back rather than losing it
      });
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          form.requestSubmit();
        }
      });
      body.appendChild(form);
      el.appendChild(body);

      minis.appendChild(el);
      lastRendered[item.id] = 0;
    }

    var log = el.querySelector(".chat-log");
    var unreadEl = el.querySelector(".chat-mini-unread");
    var isCollapsed = el.classList.contains("is-collapsed");
    var newFromCustomer = 0;
    (item.messages || []).forEach(function (message) {
      if (message.id <= (lastRendered[item.id] || 0)) return;
      appendMessage(log, message);
      lastRendered[item.id] = message.id;
      if (isCollapsed && message.sender_type === "customer") newFromCustomer += 1;
    });
    if (newFromCustomer && unreadEl) {
      var current = parseInt(unreadEl.textContent, 10) || 0;
      unreadEl.textContent = String(current + newFromCustomer);
      unreadEl.hidden = false;
    }
  }

  var pollSeconds = 5;
  var timer = null;

  function refresh() {
    return fetch(urls.live, {
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" }
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        pollSeconds = data.poll_seconds || pollSeconds;

        var waitingIds = {};
        (data.waiting || []).forEach(function (item) {
          waitingIds[item.id] = true;
          if (!minimized.has(String(item.id))) renderToast(item);
        });
        // Drop toasts for requests no longer waiting -- accepted or declined
        // from the dashboard, the desk, or another staff member's browser.
        Array.prototype.forEach.call(toasts.querySelectorAll(".chat-toast"), function (el) {
          if (!waitingIds[el.dataset.id]) {
            el.remove();
            minimized.delete(el.dataset.id);
          }
        });
        saveIds(MINIMIZED_KEY, minimized);

        var liveIds = {};
        (data.live || []).forEach(function (item) {
          liveIds[item.id] = true;
          if (openConversations.has(String(item.id))) renderMini(item);
        });
        // Drop mini windows for chats this browser opened that are no longer
        // live (closed by any staff member).
        Array.prototype.forEach.call(minis.querySelectorAll(".chat-mini"), function (el) {
          if (!liveIds[el.dataset.id]) removeMini(el.dataset.id);
        });
      })
      .catch(function () { /* keep polling */ });
  }

  function schedule() {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(function () {
      refresh().then(schedule);
    }, Math.max(pollSeconds, 3) * 1000);
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      if (timer) window.clearTimeout(timer);
      refresh().then(schedule);
    }
  });

  refresh().then(schedule);
})();
