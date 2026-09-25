// app/static/js/chat.js — in-app Chat page (/chat).
//
// Conversation list + message thread + composer. Streams replies over SSE from
// POST /api/chat/stream. Read-only assistant: tool calls are shown as compact
// status lines.
(function () {
  "use strict";

  var API = "/api/chat";

  var list = document.getElementById("chat-conv-list");
  var listEmpty = document.getElementById("chat-conv-empty");
  var search = document.getElementById("chat-search");
  var messages = document.getElementById("chat-messages");
  var empty = document.getElementById("chat-empty");
  var form = document.getElementById("chat-composer");
  var input = document.getElementById("chat-input");
  var sendBtn = document.getElementById("chat-send");
  var newBtn = document.getElementById("chat-new");
  if (!messages || !form || !input) return;

  var conversationId = null;
  var streaming = false;

  // ── helpers ──────────────────────────────────────────────────────────────

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function csrfToken() {
    var m = document.cookie.match(/(?:^|; )csrf_token=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function api(method, path, body) {
    var opts = { method: method, headers: { "X-CSRF-Token": csrfToken() } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(API + path, opts).then(function (r) {
      return r
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          if (!r.ok) throw new Error(data.message || data.error || data.detail || "Request failed (" + r.status + ")");
          return data;
        });
    });
  }

  function toast(msg) {
    if (window.Fluxito && window.Fluxito.toast) window.Fluxito.toast(msg, "error");
    else window.alert(msg);
  }

  // ── markdown (self-contained, XSS-safe: everything is escaped first) ─────

  function escHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function inline(s) {
    s = escHtml(s);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    s = s.replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/)[^)\s]*)\)/g, function (_, text, href) {
      return '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + text + "</a>";
    });
    return s.replace(/ {2}\n/g, "<br>");
  }

  function tableRow(line, head) {
    var tag = head ? "th" : "td";
    return (
      "<tr>" +
      line
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split("|")
        .map(function (c) {
          return "<" + tag + ">" + inline(c.trim()) + "</" + tag + ">";
        })
        .join("") +
      "</tr>"
    );
  }

  function markdown(src) {
    try {
      return renderBlocks(String(src || ""));
    } catch (e) {
      return "<pre>" + escHtml(src) + "</pre>";
    }
  }

  function renderBlocks(src) {
    var lines = src.replace(/\r\n?/g, "\n").split("\n");
    var out = [];
    var para = [];
    var i = 0;
    function flush() {
      if (para.length) out.push("<p>" + inline(para.join("\n")) + "</p>");
      para = [];
    }
    while (i < lines.length) {
      var line = lines[i];
      var fence = line.match(/^(`{3,}|~{3,})/);
      if (fence) {
        flush();
        var code = [];
        i++;
        while (i < lines.length && lines[i].indexOf(fence[1]) !== 0) code.push(escHtml(lines[i++]));
        i++;
        out.push("<pre><code>" + code.join("\n") + "</code></pre>");
        continue;
      }
      var h = line.match(/^(#{1,6})\s+(.*)/);
      if (h) {
        flush();
        var lvl = Math.min(h[1].length + 1, 4);
        out.push("<h" + lvl + ">" + inline(h[2].trim()) + "</h" + lvl + ">");
        i++;
        continue;
      }
      if (/^>\s?/.test(line)) {
        flush();
        var bq = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) bq.push(lines[i++].replace(/^>\s?/, ""));
        out.push("<blockquote>" + renderBlocks(bq.join("\n")) + "</blockquote>");
        continue;
      }
      if (/^\s*[-*]\s+/.test(line) || /^\s*\d+\.\s+/.test(line)) {
        flush();
        var ordered = /^\s*\d+\.\s+/.test(line);
        var re = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/;
        var items = [];
        while (i < lines.length && re.test(lines[i])) items.push("<li>" + inline(lines[i++].replace(re, "")) + "</li>");
        out.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>"));
        continue;
      }
      if (line.indexOf("|") !== -1 && i + 1 < lines.length && /^\|?[\s\-|:]+\|/.test(lines[i + 1])) {
        flush();
        var rows = [tableRow(line, true)];
        i += 2;
        while (i < lines.length && lines[i].indexOf("|") !== -1) rows.push(tableRow(lines[i++], false));
        out.push('<div class="chat-table-wrap"><table>' + rows.join("") + "</table></div>");
        continue;
      }
      if (!line.trim()) {
        flush();
        i++;
        continue;
      }
      para.push(line);
      i++;
    }
    flush();
    return out.join("\n");
  }

  // ── thread rendering ─────────────────────────────────────────────────────

  function addMessage(role) {
    if (empty) empty.hidden = true;
    var row = el("div", "chat-msg is-" + role);
    var body = el("div", "chat-msg-body");
    row.appendChild(body);
    messages.appendChild(row);
    return body;
  }

  function scrollBottom() {
    messages.scrollTop = messages.scrollHeight;
  }

  function toolLabel(name, args) {
    var detail = args && typeof args.action === "string" ? " · " + args.action : "";
    return "→ " + name + detail;
  }

  function clearThread() {
    Array.prototype.slice.call(messages.querySelectorAll(".chat-msg")).forEach(function (n) {
      n.remove();
    });
    if (empty) empty.hidden = false;
  }

  function renderStored(data) {
    clearThread();
    var body = null;
    var tools = null;
    var toolEls = {};
    (data.messages || []).forEach(function (m) {
      if (m.role === "user") {
        body = null;
        var ub = addMessage("user");
        (m.content || []).forEach(function (b) {
          if (b.type === "text") ub.appendChild(el("div", "chat-md")).innerHTML = markdown(b.text);
        });
        return;
      }
      if (!body) {
        body = addMessage("assistant");
        tools = null;
      }
      (m.content || []).forEach(function (b) {
        if (b.type === "text" && b.text) {
          tools = null;
          body.appendChild(el("div", "chat-md")).innerHTML = markdown(b.text);
        } else if (b.type === "tool_use") {
          if (!tools) tools = body.appendChild(el("div", "chat-tools"));
          toolEls[b.id] = tools.appendChild(el("div", "chat-tool is-done", toolLabel(b.name, b.input)));
        } else if (b.type === "tool_result" && b.is_error && toolEls[b.tool_use_id]) {
          toolEls[b.tool_use_id].className = "chat-tool is-error";
        }
      });
    });
    scrollBottom();
  }

  // ── conversation list ────────────────────────────────────────────────────

  var searchTimer = null;

  function loadList() {
    if (!list) return Promise.resolve();
    var q = search ? search.value.trim() : "";
    return api("GET", "/conversations" + (q ? "?q=" + encodeURIComponent(q) : ""))
      .then(function (data) {
        var convs = data.conversations || [];
        list.innerHTML = "";
        convs.forEach(function (c) {
          list.appendChild(listItem(c));
        });
        if (listEmpty) {
          listEmpty.textContent = q ? "No matching conversations." : "No conversations yet.";
          listEmpty.hidden = convs.length > 0;
        }
      })
      .catch(function () {});
  }

  function listItem(c) {
    var li = el("li", "chat-conv-item" + (c.id === conversationId ? " is-active" : ""));
    li.dataset.id = c.id;
    var open = el("button", "chat-conv-open", c.title || "New chat");
    open.type = "button";
    open.title = c.title || "New chat";
    open.addEventListener("click", function () {
      openConversation(c.id);
    });
    var rename = el("button", "chat-conv-act", "Rename");
    rename.type = "button";
    rename.setAttribute("aria-label", "Rename conversation");
    rename.addEventListener("click", function () {
      var title = window.prompt("Rename conversation", c.title || "");
      if (title == null || !title.trim()) return;
      api("PATCH", "/conversations/" + c.id, { title: title })
        .then(loadList)
        .catch(function (e) {
          toast(e.message);
        });
    });
    var del = el("button", "chat-conv-act", "Delete");
    del.type = "button";
    del.setAttribute("aria-label", "Delete conversation");
    del.addEventListener("click", function () {
      if (!window.confirm("Delete this conversation?")) return;
      api("DELETE", "/conversations/" + c.id)
        .then(function () {
          if (c.id === conversationId) newChat();
          return loadList();
        })
        .catch(function (e) {
          toast(e.message);
        });
    });
    li.appendChild(open);
    li.appendChild(rename);
    li.appendChild(del);
    return li;
  }

  function markActive() {
    if (!list) return;
    Array.prototype.forEach.call(list.children, function (li) {
      li.classList.toggle("is-active", li.dataset.id === conversationId);
    });
  }

  function setUrl(id) {
    var url = id ? "/chat?c=" + encodeURIComponent(id) : "/chat";
    window.history.replaceState(null, "", url);
  }

  function openConversation(id) {
    if (streaming) return;
    api("GET", "/conversations/" + encodeURIComponent(id))
      .then(function (data) {
        conversationId = data.id;
        setUrl(conversationId);
        renderStored(data);
        markActive();
        input.focus();
      })
      .catch(function () {
        newChat();
      });
  }

  function newChat() {
    if (streaming) return;
    conversationId = null;
    setUrl(null);
    clearThread();
    markActive();
    input.focus();
  }

  // ── sending + streaming ──────────────────────────────────────────────────

  function send(text) {
    text = (text || "").trim();
    if (!text || streaming) return;
    streaming = true;
    sendBtn.disabled = true;

    addMessage("user").appendChild(el("div", "chat-md")).innerHTML = markdown(text);
    var body = addMessage("assistant");
    var thinking = body.appendChild(el("div", "chat-thinking"));
    for (var d = 0; d < 3; d++) thinking.appendChild(el("span"));
    scrollBottom();

    var textEl = null;
    var textSrc = "";
    var tools = null;
    var toolEls = {};
    var toolArgs = {};

    function unthink() {
      if (thinking) {
        thinking.remove();
        thinking = null;
      }
    }
    function finish() {
      unthink();
      Object.keys(toolEls).forEach(function (k) {
        if (/is-pending/.test(toolEls[k].className)) toolEls[k].className = "chat-tool is-done";
      });
      streaming = false;
      sendBtn.disabled = false;
      loadList();
    }
    function showError(msg) {
      unthink();
      textEl = null;
      body.appendChild(el("div", "chat-error", msg));
      scrollBottom();
    }

    function handle(p) {
      if (p.type === "conversation") {
        conversationId = p.conversation_id;
        setUrl(conversationId);
      } else if (p.type === "text_delta") {
        unthink();
        if (!textEl) {
          tools = null;
          textEl = body.appendChild(el("div", "chat-md"));
          textSrc = "";
        }
        textSrc += p.text || "";
        textEl.innerHTML = markdown(textSrc);
      } else if (p.type === "tool_call_start") {
        textEl = null;
        if (!tools) tools = body.appendChild(el("div", "chat-tools"));
        toolArgs[p.tool_id] = "";
        toolEls[p.tool_id] = tools.appendChild(el("div", "chat-tool is-pending", toolLabel(p.tool_name || "tool")));
        toolEls[p.tool_id].dataset.name = p.tool_name || "tool";
      } else if (p.type === "tool_result") {
        var line = toolEls[p.tool_id];
        if (line) line.className = "chat-tool " + (p.is_error ? "is-error" : "is-done");
      } else if (p.type === "notice") {
        body.appendChild(el("div", "chat-notice", p.text || ""));
      } else if (p.type === "error") {
        showError(p.error || "Something went wrong.");
      }
      scrollBottom();
    }

    fetch(API + "/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify({ message: text, conversation_id: conversationId }),
    })
      .then(function (resp) {
        if (!resp.ok) {
          return resp
            .json()
            .catch(function () {
              return {};
            })
            .then(function (j) {
              showError(j.message || j.detail || "Error " + resp.status);
              finish();
            });
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buf = "";
        function pump() {
          return reader.read().then(function (r) {
            if (r.done) return finish();
            buf += decoder.decode(r.value, { stream: true });
            var idx;
            while ((idx = buf.indexOf("\n\n")) >= 0) {
              var frame = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              var data = frame.split("\n").filter(function (l) {
                return l.indexOf("data:") === 0;
              })[0];
              if (!data) continue;
              try {
                handle(JSON.parse(data.slice(5).trim()));
              } catch (e) {
                /* ignore malformed frame */
              }
            }
            return pump();
          });
        }
        return pump();
      })
      .catch(function () {
        showError("Network error — the reply was interrupted.");
        finish();
      });
  }

  // ── wiring ───────────────────────────────────────────────────────────────

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }

  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit"));
    }
  });
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (input.disabled) return;
    var text = input.value;
    if (!text.trim() || streaming) return;
    input.value = "";
    autosize();
    send(text);
  });
  if (newBtn) newBtn.addEventListener("click", newChat);
  if (search) {
    search.addEventListener("input", function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(loadList, 250);
    });
  }

  var initial = new URLSearchParams(window.location.search).get("c");
  loadList().then(function () {
    if (initial) openConversation(initial);
  });
})();
