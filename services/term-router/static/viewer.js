/* term.ald3.com — file viewer / editor (right-bottom pane).
 *
 * Public surface (window.Viewer):
 *   - open(path) → Promise<void>     — load and render a file
 *   - close()                         — empty state
 *
 * Renderer dispatch is by extension first, with a fallback to
 * /api/files/read returning 415 (binary) for unknown extensions —
 * in which case we switch to /api/files/raw for inline render or
 * an explicit "binary, download" prompt when no native browser
 * renderer applies (docx/xlsx/etc.).
 *
 * Save: only the text editor exposes save. POST /api/files/save with
 * {path, content}. Cmd/Ctrl+S triggers it. The sidebar is refreshed
 * on success so size/mtime update.
 */
(function () {
  "use strict";

  var head = document.getElementById("viewer-head");
  var titleEl = document.getElementById("viewer-title");
  var statusEl = document.getElementById("viewer-status");
  var saveBtn = document.getElementById("viewer-save");
  var downloadLink = document.getElementById("viewer-download");
  var closeBtn = document.getElementById("viewer-close");
  var body = document.getElementById("viewer-body");
  var vresizer = document.getElementById("vresizer");
  var rightpane = document.getElementById("rightpane");
  var viewerpane = document.getElementById("viewerpane");

  // Extension → renderer dispatch. Anything not matched here gets sent
  // to the text editor via /api/files/read; the server returns 415 for
  // binary content (UTF-8 decode failure), which the open() flow
  // catches and turns into a download-fallback render.
  var IMAGE_EXTS = ["png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "ico"];
  var VIDEO_EXTS = ["mp4", "webm", "mov", "ogv", "mkv"];
  var AUDIO_EXTS = ["mp3", "wav", "ogg", "flac", "aac", "m4a", "opus"];
  var PDF_EXTS = ["pdf"];

  var state = {
    path: null,
    mode: null,        // "text" | "image" | "video" | "audio" | "pdf" | "csv" | "binary"
    serverContent: "", // last value from server (for dirty-detection)
    textarea: null,
    editor: null,      // CodeMirror instance when in text mode
    editorRO: null,    // ResizeObserver tied to the active editor host
  };

  // Pick a CodeMirror mode for the file. Tries the extension first via
  // CM's mode-meta lookup (handles js/ts/py/etc.); then a few special
  // basenames (Dockerfile, Makefile, etc.); falls back to no mode (which
  // gives us a plain editor with line numbers and active-line highlight).
  function pickCmMode(path) {
    if (typeof CodeMirror === "undefined") return null;
    var ext = extOf(path);
    if (ext && CodeMirror.findModeByExtension) {
      var byExt = CodeMirror.findModeByExtension(ext);
      if (byExt) return byExt.mime || byExt.mode;
    }
    var bn = basename(path).toLowerCase();
    if (bn === "dockerfile" || bn.indexOf("dockerfile.") === 0 || bn.indexOf(".dockerfile") > 0) {
      return "dockerfile";
    }
    if (bn === ".env" || bn.indexOf(".env.") === 0 || /\.(properties|ini|conf)$/.test(bn)) {
      return "text/x-properties";
    }
    return null;
  }

  function teardownEditor() {
    if (state.editorRO) {
      try { state.editorRO.disconnect(); } catch (e) { /* ignore */ }
      state.editorRO = null;
    }
    state.editor = null;
    state.textarea = null;
  }

  function extOf(p) {
    var i = p.lastIndexOf(".");
    if (i === -1) return "";
    return p.slice(i + 1).toLowerCase();
  }

  function basename(p) {
    var i = p.lastIndexOf("/");
    return i === -1 ? p : p.slice(i + 1);
  }

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " K";
    if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + " M";
    return (n / 1073741824).toFixed(2) + " G";
  }

  function setStatus(msg, kind) {
    statusEl.textContent = msg || "";
    statusEl.className = "viewer-status " + (kind || "");
  }

  function setTitle(path, modified) {
    titleEl.textContent = path || "Open a file from the sidebar to view or edit";
    titleEl.title = path || "";
    if (modified) titleEl.classList.add("modified");
    else titleEl.classList.remove("modified");
  }

  function clearBody() {
    body.innerHTML = "";
  }

  function showLoading(path) {
    clearBody();
    var d = document.createElement("div");
    d.className = "viewer-loading";
    d.textContent = "Loading " + basename(path) + "…";
    body.appendChild(d);
  }

  function showError(msg) {
    clearBody();
    var d = document.createElement("div");
    d.className = "viewer-error";
    d.textContent = msg;
    body.appendChild(d);
  }

  function rawUrl(path) {
    return "/api/files/raw?path=" + encodeURIComponent(path);
  }

  function downloadUrl(path) {
    return "/api/files/download?path=" + encodeURIComponent(path);
  }

  // ---- Renderers ----------------------------------------------------

  function renderText(path, content, truncated, maxBytes) {
    state.mode = "text";
    state.serverContent = content;
    teardownEditor();
    clearBody();

    // Stack: optional truncation warning on top, editor below — fills
    // the viewer body via flex column so the editor gets all remaining
    // height regardless of whether the warning is present.
    var stack = document.createElement("div");
    stack.className = "viewer-text-stack";
    body.appendChild(stack);

    if (truncated) {
      var warn = document.createElement("div");
      warn.className = "viewer-truncated";
      warn.textContent =
        "File is larger than " + fmtSize(maxBytes) +
        ". Showing the first " + fmtSize(maxBytes) +
        ". Save will overwrite with what's shown — DO NOT save unless you intend to truncate.";
      stack.appendChild(warn);
    }

    // Prefer the CodeMirror editor (PyCharm-like: syntax highlighting,
    // line numbers, active-line, bracket matching, Darcula theme). Fall
    // back to a plain textarea only if the CM bundle failed to load.
    if (typeof CodeMirror !== "undefined") {
      var host = document.createElement("div");
      host.className = "viewer-cm-host";
      stack.appendChild(host);

      var modeSpec = pickCmMode(path);
      var cm = CodeMirror(host, {
        value: content,
        mode: modeSpec,
        theme: "darcula",
        lineNumbers: true,
        indentUnit: 4,
        tabSize: 4,
        indentWithTabs: false,
        smartIndent: true,
        matchBrackets: true,
        autoCloseBrackets: true,
        matchTags: { bothTags: true },
        styleActiveLine: true,
        showTrailingSpace: true,
        lineWrapping: false,
        // viewportMargin: Infinity renders the whole document. The
        // server-side max-read cap (~256 KB) keeps that bounded; this
        // avoids virtual-scroll glitches inside our flex layout.
        viewportMargin: Infinity,
        extraKeys: {
          "Cmd-S": function () { save(); return false; },
          "Ctrl-S": function () { save(); return false; },
          "Tab": function (cmInst) {
            // Insert spaces (indentUnit) instead of a tab, even when no
            // selection — matches PyCharm's default Python behavior.
            if (cmInst.somethingSelected()) {
              cmInst.indentSelection("add");
            } else {
              cmInst.replaceSelection(
                Array(cmInst.getOption("indentUnit") + 1).join(" "),
                "end", "+input"
              );
            }
          },
        },
      });
      cm.on("change", function () {
        setTitle(path, cm.getValue() !== state.serverContent);
      });
      state.editor = cm;

      // Reflow the editor when the viewer pane is resized (the user
      // drags #vresizer or #resizer). CodeMirror needs an explicit
      // refresh() to recompute its gutter/line widths.
      if (typeof ResizeObserver !== "undefined") {
        state.editorRO = new ResizeObserver(function () {
          if (state.editor) state.editor.refresh();
        });
        state.editorRO.observe(host);
      }

      saveBtn.hidden = false;
      saveBtn.disabled = truncated;
      setStatus(fmtSize(content.length) + (truncated ? " (truncated)" : ""), "");
      requestAnimationFrame(function () {
        cm.refresh();
        cm.focus();
      });
      return;
    }

    // Fallback: plain textarea (only if CodeMirror bundle missing).
    var ta = document.createElement("textarea");
    ta.className = "viewer-textarea";
    ta.spellcheck = false;
    ta.value = content;
    ta.addEventListener("input", function () {
      setTitle(path, ta.value !== state.serverContent);
    });
    ta.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        save();
      }
    });
    state.textarea = ta;
    stack.appendChild(ta);
    saveBtn.hidden = false;
    saveBtn.disabled = truncated;
    setStatus(fmtSize(content.length) + (truncated ? " (truncated)" : ""), "");
    requestAnimationFrame(function () { ta.focus(); });
  }

  function renderImage(path) {
    state.mode = "image";
    clearBody();
    var wrap = document.createElement("div");
    wrap.className = "viewer-image-wrap";
    var img = document.createElement("img");
    img.alt = basename(path);
    img.src = rawUrl(path);
    img.addEventListener("error", function () {
      showError("Failed to load image.");
    });
    wrap.appendChild(img);
    body.appendChild(wrap);
    saveBtn.hidden = true;
    setStatus("");
  }

  function renderVideo(path) {
    state.mode = "video";
    clearBody();
    var v = document.createElement("video");
    v.className = "viewer-video";
    v.controls = true;
    v.preload = "metadata";
    v.src = rawUrl(path);
    body.appendChild(v);
    saveBtn.hidden = true;
    setStatus("");
  }

  function renderAudio(path) {
    state.mode = "audio";
    clearBody();
    var wrap = document.createElement("div");
    wrap.style.padding = "20px";
    wrap.style.display = "flex";
    wrap.style.alignItems = "center";
    wrap.style.justifyContent = "center";
    wrap.style.height = "100%";
    var a = document.createElement("audio");
    a.className = "viewer-audio";
    a.controls = true;
    a.preload = "metadata";
    a.src = rawUrl(path);
    a.style.width = "min(560px, 100%)";
    wrap.appendChild(a);
    body.appendChild(wrap);
    saveBtn.hidden = true;
    setStatus("");
  }

  function renderPdf(path) {
    state.mode = "pdf";
    clearBody();
    var f = document.createElement("iframe");
    f.className = "viewer-pdf";
    f.src = rawUrl(path) + "#view=FitH";
    body.appendChild(f);
    saveBtn.hidden = true;
    setStatus("");
  }

  // Tiny CSV parser — handles quoted fields with embedded quotes / commas /
  // newlines (RFC 4180-ish). Caps at 5000 rows so a 100MB CSV doesn't
  // freeze the page; user can fall back to text-edit mode for the rest.
  function parseCsv(text, maxRows) {
    var rows = [];
    var row = [];
    var cur = "";
    var inQuotes = false;
    var i = 0;
    var n = text.length;
    while (i < n && rows.length < maxRows) {
      var c = text[i];
      if (inQuotes) {
        if (c === '"') {
          if (text[i + 1] === '"') { cur += '"'; i += 2; continue; }
          inQuotes = false;
          i++;
        } else {
          cur += c;
          i++;
        }
      } else {
        if (c === '"') { inQuotes = true; i++; }
        else if (c === ",") { row.push(cur); cur = ""; i++; }
        else if (c === "\r") { i++; }
        else if (c === "\n") {
          row.push(cur);
          rows.push(row);
          row = [];
          cur = "";
          i++;
        } else { cur += c; i++; }
      }
    }
    if (cur !== "" || row.length) {
      row.push(cur);
      rows.push(row);
    }
    return rows;
  }

  function renderCsv(path, content, truncated, maxBytes) {
    state.mode = "csv";
    state.serverContent = content;
    clearBody();
    if (truncated) {
      var warn = document.createElement("div");
      warn.className = "viewer-truncated";
      warn.textContent = "File larger than " + fmtSize(maxBytes) + " — table preview is truncated.";
      body.appendChild(warn);
    }
    var rows = parseCsv(content, 5000);
    if (!rows.length) {
      var d = document.createElement("div");
      d.className = "viewer-empty";
      d.textContent = "(empty CSV)";
      body.appendChild(d);
      saveBtn.hidden = true;
      setStatus("");
      return;
    }
    var wrap = document.createElement("div");
    wrap.className = "viewer-table-wrap";
    var tbl = document.createElement("table");
    tbl.className = "viewer-table";
    var thead = document.createElement("thead");
    var theadRow = document.createElement("tr");
    rows[0].forEach(function (h) {
      var th = document.createElement("th");
      th.textContent = h;
      theadRow.appendChild(th);
    });
    thead.appendChild(theadRow);
    tbl.appendChild(thead);
    var tbody = document.createElement("tbody");
    for (var r = 1; r < rows.length; r++) {
      var tr = document.createElement("tr");
      for (var k = 0; k < rows[r].length; k++) {
        var td = document.createElement("td");
        td.textContent = rows[r][k];
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    tbl.appendChild(tbody);
    wrap.appendChild(tbl);
    body.appendChild(wrap);
    // CSV is read-only in this view (the data table doesn't round-trip
    // back to text safely without the user choosing). Edit-as-text is a
    // separate "view as text" affordance — defer for now; user can also
    // open the file in the terminal with their editor of choice.
    saveBtn.hidden = true;
    setStatus(rows.length - 1 + " rows × " + rows[0].length + " cols", "");
  }

  function renderBinary(path, mime) {
    state.mode = "binary";
    clearBody();
    var d = document.createElement("div");
    d.className = "viewer-binary";
    var hint = document.createElement("p");
    hint.style.margin = "0 0 10px";
    var ext = extOf(path);
    if (ext === "docx" || ext === "doc" || ext === "odt") {
      hint.textContent = "Word document — no inline preview yet. Download to view, or open via the terminal (e.g. `pandoc " + basename(path) + " -t plain`).";
    } else if (ext === "xlsx" || ext === "xls" || ext === "ods") {
      hint.textContent = "Spreadsheet — no inline preview yet. Download to view, or convert via terminal (`xlsx2csv " + basename(path) + "`).";
    } else if (ext === "pptx" || ext === "ppt" || ext === "odp") {
      hint.textContent = "Slide deck — no inline preview yet. Download to view.";
    } else {
      hint.textContent = "Binary file (" + (mime || "unknown type") + ") — no native preview available.";
    }
    var dl = document.createElement("a");
    dl.href = downloadUrl(path);
    dl.textContent = "Download " + basename(path);
    dl.setAttribute("download", basename(path));
    d.appendChild(hint);
    d.appendChild(dl);
    body.appendChild(d);
    saveBtn.hidden = true;
    setStatus("");
  }

  // ---- Open dispatcher ----------------------------------------------

  function open(path) {
    if (!path) return Promise.resolve();
    state.path = path;
    setTitle(path, false);
    showLoading(path);
    saveBtn.hidden = true;
    closeBtn.hidden = false;
    downloadLink.hidden = false;
    downloadLink.href = downloadUrl(path);
    downloadLink.setAttribute("download", basename(path));

    var ext = extOf(path);

    // Image / video / audio / pdf are always binary-stream rendered — no
    // need to touch /api/files/read first. SVG falls through to the
    // text editor branch (TEXT_EXTS contains it) so users can edit
    // SVG markup; pure-image renders if they prefer can use the
    // download link.
    if (IMAGE_EXTS.indexOf(ext) >= 0) {
      renderImage(path);
      return Promise.resolve();
    }
    if (VIDEO_EXTS.indexOf(ext) >= 0) {
      renderVideo(path);
      return Promise.resolve();
    }
    if (AUDIO_EXTS.indexOf(ext) >= 0) {
      renderAudio(path);
      return Promise.resolve();
    }
    if (PDF_EXTS.indexOf(ext) >= 0) {
      renderPdf(path);
      return Promise.resolve();
    }

    // Try the text editor for everything else. The server returns 415 if
    // the file is non-UTF-8 (true binary); in that case fall back to a
    // download link.
    return fetch(
      "/api/files/read?path=" + encodeURIComponent(path),
      { credentials: "same-origin" }
    ).then(function (r) {
      if (r.status === 415) {
        renderBinary(path, "binary");
        return null;
      }
      if (r.status === 413) {
        showError("File too large to open in editor (server cap).");
        return null;
      }
      if (r.status === 401) {
        showError("Not authorized — refresh page.");
        return null;
      }
      if (!r.ok) {
        return r.text().then(function (t) {
          showError("Failed to open: " + (t || r.status));
          return null;
        });
      }
      return r.json();
    }).then(function (j) {
      if (!j) return;
      // CSV gets a structured table view; everything else flat text.
      if (ext === "csv" || ext === "tsv") {
        renderCsv(path, j.content, j.truncated, j.max_bytes);
      } else {
        renderText(path, j.content, j.truncated, j.max_bytes);
      }
    }).catch(function (err) {
      showError(String(err.message || err));
    });
  }

  function close() {
    state.path = null;
    state.mode = null;
    state.serverContent = "";
    teardownEditor();
    setTitle(null, false);
    setStatus("");
    saveBtn.hidden = true;
    closeBtn.hidden = true;
    downloadLink.hidden = true;
    clearBody();
    var d = document.createElement("div");
    d.className = "viewer-empty";
    d.textContent = "No file open.";
    body.appendChild(d);
  }

  // ---- Save ----------------------------------------------------------

  function save() {
    if (state.mode !== "text" || !state.path) return;
    var content;
    if (state.editor) content = state.editor.getValue();
    else if (state.textarea) content = state.textarea.value;
    else return;
    if (content === state.serverContent) {
      setStatus("no changes", "");
      return;
    }
    setStatus("saving…", "");
    saveBtn.disabled = true;
    fetch("/api/files/save", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.path, content: content }),
    }).then(function (r) {
      saveBtn.disabled = false;
      if (!r.ok) {
        return r.text().then(function (t) {
          setStatus("save failed: " + (t || r.status), "error");
        });
      }
      state.serverContent = content;
      setTitle(state.path, false);
      setStatus("saved " + new Date().toLocaleTimeString(), "ok");
      // Refresh the sidebar listing so size/mtime update immediately.
      if (window.Files && typeof window.Files.refresh === "function") {
        window.Files.refresh();
      }
    }).catch(function (err) {
      saveBtn.disabled = false;
      setStatus("save failed: " + err.message, "error");
    });
  }

  saveBtn.addEventListener("click", save);
  closeBtn.addEventListener("click", close);

  // Cmd/Ctrl+S anywhere in the viewer pane — useful when the textarea
  // hasn't taken focus yet (e.g. user clicked the save button area).
  viewerpane.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === "s") {
      e.preventDefault();
      save();
    }
  });

  // ---- Vertical resizer (between terminal and viewer) ---------------
  // Drag #vresizer to grow/shrink the bottom pane. Min/max keep both
  // visible. Persisted to localStorage so the layout sticks across
  // page reloads.
  (function () {
    var STORAGE_KEY = "term-viewer-h";
    var saved = parseInt(localStorage.getItem(STORAGE_KEY) || "", 10);
    if (saved && saved > 80) {
      rightpane.style.setProperty("--viewer-h", saved + "px");
    }

    var dragging = false;
    var startY = 0;
    var startH = 0;
    vresizer.addEventListener("mousedown", function (e) {
      dragging = true;
      vresizer.classList.add("dragging");
      startY = e.clientY;
      var v = getComputedStyle(rightpane).getPropertyValue("--viewer-h") || "280px";
      startH = parseInt(v, 10) || 280;
      e.preventDefault();
    });
    document.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      // Negative dy grows the bottom (vresizer moves up).
      var dy = startY - e.clientY;
      var h = Math.max(80, Math.min(window.innerHeight - 200, startH + dy));
      rightpane.style.setProperty("--viewer-h", h + "px");
    });
    document.addEventListener("mouseup", function () {
      if (!dragging) return;
      dragging = false;
      vresizer.classList.remove("dragging");
      var v = getComputedStyle(rightpane).getPropertyValue("--viewer-h") || "280px";
      var h = parseInt(v, 10);
      if (h) localStorage.setItem(STORAGE_KEY, String(h));
    });
  })();

  // ---- Public surface -----------------------------------------------

  window.Viewer = {
    open: open,
    close: close,
  };
})();
