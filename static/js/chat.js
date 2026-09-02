/* Live chat widget.
 *
 * Polling rather than WebSockets, because PythonAnywhere does not offer them on
 * the plans a small salon uses. To keep request counts (and CPU seconds) low the
 * poll rate adapts: fast while the panel is open, slow while it is closed, and
 * it backs off after a while with nothing happening or when the tab is hidden.
 */
(function () {
  "use strict";

  var widget = document.getElementById("chat-widget");
  if (!widget) return;

  var urls = {
    widget: widget.dataset.widgetUrl,
    botMenu: widget.dataset.botMenuUrl,
    botCalendars: widget.dataset.botCalendarsUrl,
    start: widget.dataset.startUrl,
    send: widget.dataset.sendUrl,
    dismiss: widget.dataset.dismissUrl
  };

  // Templated URLs look like ".../bot/services/0/calendars/" -- the
  // placeholder "0" is a path segment in the middle, so match "/0/", not
  // end-anchored.
  function urlFor(template, id) {
    return template.replace("/0/", "/" + id + "/");
  }

  var fab = document.getElementById("chat-fab");
  var panel = document.getElementById("chat-panel");
  var badge = document.getElementById("chat-badge");
  var log = document.getElementById("chat-log");
  var statusLine = document.getElementById("chat-status");
  var errorBox = document.getElementById("chat-error");

  var state = null;
  var conversationId = null;
  var lastMessageId = 0;
  var isOpen = false;
  var timer = null;
  var pollSeconds = 5;
  var quietPolls = 0;
  var unread = 0;

  // Bot menu: browsing services/calendars before any live request exists.
  // Purely client-side -- nothing is persisted until "Request live chat".
  var botMode = null; // null | "menu" | "calendars" | "email"
  var customerName = "";
  var currentService = null;

  function csrf() {
    var field = widget.querySelector("[name=csrfmiddlewaretoken]");
    return field ? field.value : "";
  }

  function post(url, data) {
    var body = new URLSearchParams(data || {});
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": csrf(),
        "X-Requested-With": "XMLHttpRequest"
      },
      body: body.toString(),
      credentials: "same-origin"
    }).then(function (response) {
      return response.json().then(function (payload) {
        if (!response.ok) throw new Error(payload.error || "Something went wrong.");
        return payload;
      });
    });
  }

  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = !message;
  }

  // "pending" and "accepted" share one pane: the conversation is live from the
  // first automatic greeting; pending only means nobody has picked it up yet.
  function paneFor(state) {
    return state === "pending" ? "accepted" : state;
  }

  function setState(next) {
    if (state === next) return;
    state = next;
    var pane = paneFor(next);
    ["offline", "no_session", "rejected", "accepted"].forEach(function (name) {
      var el = document.getElementById("chat-state-" + name);
      if (el) el.hidden = name !== pane;
    });
    var waitingNote = document.getElementById("chat-waiting-note");
    if (waitingNote) waitingNote.hidden = next !== "pending";

    var labels = {
      offline: "Currently offline",
      no_session: "Ready when you are",
      pending: "Waiting for a stylist…",
      rejected: "",
      accepted: "Connected"
    };
    statusLine.textContent = labels[next] || "";
  }

  function appendMessage(message) {
    if (!log) return;
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
    time.textContent = message.time;
    row.appendChild(time);

    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
  }

  function renderMessages(messages) {
    if (!messages || !messages.length) return false;
    messages.forEach(function (message) {
      appendMessage(message);
      if (message.id > lastMessageId) lastMessageId = message.id;
      if (!isOpen && message.sender_type !== "customer") unread += 1;
    });
    if (unread > 0) {
      badge.textContent = unread > 9 ? "9+" : String(unread);
      badge.hidden = false;
    }
    return true;
  }

  function applyWording(data) {
    if (data.heading) document.getElementById("chat-heading").textContent = data.heading;
    if (data.welcome_text) document.getElementById("chat-welcome").textContent = data.welcome_text;
    if (data.offline_text) document.getElementById("chat-offline-text").textContent = data.offline_text;
    var nameField = document.getElementById("chat-name-field");
    if (nameField) nameField.hidden = data.require_name === false;
  }

  // ---- bot menu: services -> calendars, all before any live request ------
  function showBotMenuPane() {
    ["offline", "no_session", "rejected", "accepted"].forEach(function (name) {
      var el = document.getElementById("chat-state-" + name);
      if (el) el.hidden = true;
    });
    document.getElementById("chat-state-bot_menu").hidden = false;
    statusLine.textContent = "How can we help?";
  }

  function exitBotMenu() {
    botMode = null;
    document.getElementById("chat-state-bot_menu").hidden = true;
  }

  function renderBotTop(data) {
    botMode = "menu";
    document.getElementById("bot-greeting").textContent = data.greeting;

    var body = document.getElementById("bot-menu-body");
    body.innerHTML = "";

    if (data.services && data.services.length) {
      var note = document.createElement("p");
      note.className = "chat-note";
      note.textContent = "What can we do for you today?";
      body.appendChild(note);

      var list = document.createElement("div");
      list.className = "bot-btn-list";
      data.services.forEach(function (service) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-sm btn-ghost bot-btn";
        btn.textContent = service.name + " (" + service.duration_minutes + " min)";
        btn.addEventListener("click", function () { loadServiceCalendars(service); });
        list.appendChild(btn);
      });
      body.appendChild(list);
    }

    var liveBtn = document.createElement("button");
    liveBtn.type = "button";
    liveBtn.className = "btn btn-sm";
    liveBtn.style.width = "100%";
    liveBtn.textContent = "Request live chat";
    liveBtn.addEventListener("click", requestLiveChat);
    body.appendChild(liveBtn);
  }

  function renderBotCalendars(service, calendars) {
    botMode = "calendars";
    var body = document.getElementById("bot-menu-body");
    body.innerHTML = "";

    var back = document.createElement("button");
    back.type = "button";
    back.className = "btn btn-sm btn-ghost";
    back.textContent = "← Back";
    back.addEventListener("click", function () { enterBotMenu(); });
    body.appendChild(back);

    var note = document.createElement("p");
    note.className = "chat-note";
    note.textContent = calendars.length
      ? "Who would you like to see for " + service.name + "?"
      : "Nobody currently offers " + service.name + " online — try live chat instead.";
    body.appendChild(note);

    var list = document.createElement("div");
    list.className = "bot-btn-list";
    calendars.forEach(function (calendar) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-sm bot-btn";
      btn.textContent = calendar.name;
      btn.addEventListener("click", function () { renderBotEmailPrompt(calendar); });
      list.appendChild(btn);
    });
    body.appendChild(list);
  }

  // We already know the visitor's name from the start of the chat; ask for
  // an email too so the reservation form on the next page can be filled in
  // without making them type both again.
  function renderBotEmailPrompt(calendar) {
    botMode = "email";
    var body = document.getElementById("bot-menu-body");
    body.innerHTML = "";

    var back = document.createElement("button");
    back.type = "button";
    back.className = "btn btn-sm btn-ghost";
    back.textContent = "← Back";
    back.addEventListener("click", function () { loadServiceCalendars(currentService); });
    body.appendChild(back);

    var note = document.createElement("p");
    note.className = "chat-note";
    note.textContent = "What's your email? We'll use it to fill in your booking with " + calendar.name + ".";
    body.appendChild(note);

    var form = document.createElement("form");
    var field = document.createElement("div");
    field.className = "field";
    var label = document.createElement("label");
    label.setAttribute("for", "bot-email-input");
    label.textContent = "Your email";
    var input = document.createElement("input");
    input.type = "email";
    input.id = "bot-email-input";
    input.className = "field-input";
    input.placeholder = "you@example.com";
    input.required = true;
    field.appendChild(label);
    field.appendChild(input);
    form.appendChild(field);

    var submit = document.createElement("button");
    submit.type = "submit";
    submit.className = "btn btn-sm";
    submit.style.width = "100%";
    submit.style.marginTop = ".6rem";
    submit.textContent = "Continue to booking";
    form.appendChild(submit);

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var email = input.value.trim();
      if (!email) return;
      var separator = calendar.url.indexOf("?") === -1 ? "?" : "&";
      window.location.href =
        calendar.url + separator +
        "name=" + encodeURIComponent(customerName) +
        "&email=" + encodeURIComponent(email);
    });

    body.appendChild(form);
    window.setTimeout(function () { input.focus(); }, 0);
  }

  function loadServiceCalendars(service) {
    currentService = service;
    fetch(urlFor(urls.botCalendars, service.id), {
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" }
    })
      .then(function (r) { return r.json(); })
      .then(function (data) { renderBotCalendars(service, data.calendars || []); })
      .catch(function () { showError("Could not load that right now."); });
  }

  function enterBotMenu() {
    fetch(urls.botMenu + "?name=" + encodeURIComponent(customerName), {
      credentials: "same-origin",
      headers: { "X-Requested-With": "XMLHttpRequest" }
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        showBotMenuPane();
        renderBotTop(data);
      })
      .catch(function () { showError("Could not load the menu. Please try again."); });
  }

  function requestLiveChat() {
    exitBotMenu();
    post(urls.start, { name: customerName, page: window.location.pathname })
      .then(function () {
        setState("pending");
        quietPolls = 0;
        stop();
        poll();
      })
      .catch(function (error) {
        showError(error.message);
        enterBotMenu();
      });
  }

  function poll() {
    var url = urls.widget;
    if ((state === "accepted" || state === "pending") && lastMessageId) {
      url += "?since_id=" + lastMessageId;
    }

    fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        showError("");
        pollSeconds = data.poll_seconds || pollSeconds;

        if (data.state === "disabled") {
          widget.hidden = true;
          stop();
          return;
        }
        widget.hidden = false;
        applyWording(data);

        if (botMode) {
          if (data.state === "pending" || data.state === "accepted") {
            // A live request now exists (just requested, or started in
            // another tab) -- stop overriding the server-reported pane.
            botMode = null;
          } else {
            // Still browsing the menu: don't let the plain "no_session" pane
            // the server reports (nobody has asked for live chat yet) clobber it.
            schedule();
            return;
          }
        }

        var previous = state;
        conversationId = data.conversation_id || null;

        // A conversation that ends or restarts elsewhere resets the transcript.
        var wasLive = previous === "accepted" || previous === "pending";
        var isLive = data.state === "accepted" || data.state === "pending";
        if (wasLive && !isLive) {
          if (log) log.innerHTML = "";
          lastMessageId = 0;
        }
        if (data.state === "rejected") {
          var box = document.getElementById("chat-rejected-text");
          box.textContent = (data.messages && data.messages[0]) ? data.messages[0].text : "";
        }

        setState(data.state);
        var gotSomething = renderMessages(data.messages);

        // Back off when nothing is happening; snap back when it is.
        if (gotSomething || previous !== data.state) quietPolls = 0;
        else quietPolls += 1;

        schedule();
      })
      .catch(function () {
        quietPolls += 1;
        schedule();
      });
  }

  function currentInterval() {
    if (!isOpen) return (state === "pending" || state === "accepted") ? 15000 : 45000;
    if (document.hidden) return 20000;
    var base = pollSeconds * 1000;
    if (quietPolls > 40) return base * 4;
    if (quietPolls > 15) return base * 2;
    return base;
  }

  function schedule() {
    stop();
    timer = window.setTimeout(poll, currentInterval());
  }

  function stop() {
    if (timer) { window.clearTimeout(timer); timer = null; }
  }

  function openPanel() {
    isOpen = true;
    panel.hidden = false;
    fab.setAttribute("aria-expanded", "true");
    unread = 0;
    badge.hidden = true;
    quietPolls = 0;
    stop();
    poll();
    var input = document.getElementById("chat-input");
    if ((state === "accepted" || state === "pending") && input) input.focus();
  }

  function closePanel() {
    isOpen = false;
    panel.hidden = true;
    fab.setAttribute("aria-expanded", "false");
    schedule();
  }

  fab.addEventListener("click", function () { isOpen ? closePanel() : openPanel(); });
  document.getElementById("chat-minimise").addEventListener("click", closePanel);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isOpen) closePanel();
  });

  document.getElementById("chat-start-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var name = document.getElementById("chat-name").value.trim();
    var nameField = document.getElementById("chat-name-field");
    if (!nameField.hidden && !name) {
      showError("Please tell us your name first.");
      return;
    }
    customerName = name;
    showError("");
    enterBotMenu();
  });

  var sendForm = document.getElementById("chat-send-form");
  sendForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var input = document.getElementById("chat-input");
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    post(urls.send, { text: text })
      .then(function (payload) {
        renderMessages([payload.message]);
        quietPolls = 0;
      })
      .catch(function (error) {
        showError(error.message);
        input.value = text; // hand the message back rather than losing it
      });
  });

  // Enter sends, Shift+Enter makes a new line.
  document.getElementById("chat-input").addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendForm.requestSubmit();
    }
  });

  widget.addEventListener("click", function (event) {
    if (!event.target.matches("[data-chat-dismiss], [data-chat-cancel]")) return;
    post(urls.dismiss, {}).then(function () {
      if (log) log.innerHTML = "";
      lastMessageId = 0;
      setState("no_session");
      quietPolls = 0;
      poll();
    });
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && isOpen) { quietPolls = 0; stop(); poll(); }
  });

  poll();
})();
