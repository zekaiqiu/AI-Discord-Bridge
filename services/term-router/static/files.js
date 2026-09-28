/* term.{ald3.com,wizerith.ai} — file explorer (left pane).
 *
 * Talks to /api/files/{list,search,download,upload,mkdir,delete,save} on
 * the same origin. JWT comes from the CF_Authorization cookie
 * automatically — we never need to read or attach it from JS.
 *
 * Rendering model: lazy-loaded TREE.
 *   - state.tree[path] = { entries, expanded, loaded }
 *   - state.path is the "root" of the visible tree (defaults to /workspace).
 *     The up-button reroots one level; the path input lets you jump anywhere.
 *   - Clicking a directory toggles expansion (fetches its entries on first
 *     expand, caches them). Clicking a file opens it in the viewer.
 *
 * Public surface (window.Files):
 *   - cwd()          → string  (current root path)
 *   - navigate(path) → Promise<void>
 *   - refresh()      → Promise<void>
 */
(function () {
  "use strict";

  // --- DOM refs -----------------------------------------------------

  var pathInput = document.getElementById("path");
  var upBtn = document.getElementById("up-btn");
  var newFileBtn = document.getElementById("new-file-btn");
  var newFolderBtn = document.getElementById("new-folder-btn");
  var newProjectBtn = document.getElementById("new-project-btn");
  var openProjectBtn = document.getElementById("open-project-btn");
  var refreshBtn = document.getElementById("refresh-btn");
  var uploadInput = document.getElementById("upload-input");
  var fileList = document.getElementById("filelist");
  var searchInput = document.getElementById("search");
  var searchClear = document.getElementById("search-clear");
  var searchResults = document.getElementById("search-results");
  var dropZone = document.getElementById("drop-zone");
  var sidebar = document.getElementById("sidebar");
  var progressEl = document.getElementById("upload-progress");
  var resizer = document.getElementById("resizer");

  var MAX_UPLOAD = 1024 * 1024 * 1024;

  var state = {
    path: "/workspace",
    /** @type {Object.<string, {entries: any[], expanded: boolean, loaded: boolean}>} */
    tree: Object.create(null),
    searchActive: false,
  };

  // --- Helpers ------------------------------------------------------

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " K";
    if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + " M";
    return (n / 1073741824).toFixed(2) + " G";
  }

  function fmtTime(epochSec) {
    if (!epochSec) return "";
    var d = new Date(epochSec * 1000);
    var now = new Date();
    if (d.getFullYear() === now.getFullYear()) {
      return d.toLocaleString(undefined, {
        month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
      });
    }
    return d.toLocaleDateString();
  }

  function joinPath(base, name) {
    if (base === "/") return "/" + name;
    return base.replace(/\/$/, "") + "/" + name;
  }

  function parentOf(p) {
    if (!p || p === "/") return "/";
    var trimmed = p.replace(/\/+$/, "");
    var idx = trimmed.lastIndexOf("/");
    return idx <= 0 ? "/" : trimmed.slice(0, idx);
  }

  function setPath(p) {
    state.path = p || "/";
    pathInput.value = state.path;
  }

  function iconFor(type) {
    if (type === "d") return "▾";  // overridden per-row by caret
    if (type === "l") return "↪";
    return "·";
  }

  // --- Tree state ---------------------------------------------------

  function ensureNode(path) {
    if (!state.tree[path]) {
      state.tree[path] = { entries: [], expanded: false, loaded: false };
    }
    return state.tree[path];
  }

  function fetchDir(path) {
    return fetch("/api/files/list?path=" + encodeURIComponent(path), {
      credentials: "same-origin",
    }).then(function (r) {
      if (r.status === 401) throw new Error("not authorized — refresh page");
      return r.json().then(function (j) { return { r: r, j: j }; });
    }).then(function (rj) {
      if (!rj.r.ok) {
        throw new Error(rj.j.error || ("error " + rj.r.status));
      }
      var canonical = rj.j.path || path;
      var node = ensureNode(canonical);
      // Sort: dirs first, then files, alpha within group.
      var sorted = (rj.j.entries || []).slice().sort(function (a, b) {
        var ad = a.type === "d" ? 0 : 1;
        var bd = b.type === "d" ? 0 : 1;
        if (ad !== bd) return ad - bd;
        return a.name.localeCompare(b.name);
      });
      node.entries = sorted;
      node.loaded = true;
      return canonical;
    });
  }

  // --- Rendering ----------------------------------------------------

  function clearList() { fileList.innerHTML = ""; }

  function renderEmpty(msg) {
    clearList();
    var li = document.createElement("li");
    li.className = "empty";
    li.style.padding = "8px 10px";
    li.style.color = "var(--text-3)";
    li.style.fontStyle = "italic";
    li.textContent = msg;
    fileList.appendChild(li);
  }

  function makeRow(entry, fullPath, depth) {
    var li = document.createElement("li");
    li.className = "tree-row " + (
      entry.type === "d" ? "dir" :
      entry.type === "l" ? "link" : "file"
    );
    li.dataset.path = fullPath;
    li.style.setProperty("--depth", String(depth));

    var caret = document.createElement("span");
    caret.className = "caret";
    if (entry.type === "d") {
      var node = state.tree[fullPath];
      caret.textContent = (node && node.expanded) ? "▾" : "▸";
    }

    var icon = document.createElement("span");
    icon.className = "icon";
    icon.textContent = iconFor(entry.type);

    var name = document.createElement("span");
    name.className = "name";
    name.textContent = entry.name;

    var meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = entry.type === "d" ? "" :
      fmtSize(entry.size) + "  " + fmtTime(entry.mtime);

    var actions = document.createElement("span");
    actions.className = "row-actions";
    if (entry.type !== "d") {
      var dl = document.createElement("button");
      dl.textContent = "download";
      dl.addEventListener("click", function (ev) {
        ev.stopPropagation();
        downloadFile(fullPath);
      });
      actions.appendChild(dl);
    }
    var del = document.createElement("button");
    del.textContent = "delete";
    del.className = "danger";
    del.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (confirm("Delete " + fullPath + "? This cannot be undone.")) {
        deleteFile(fullPath);
      }
    });
    actions.appendChild(del);

    li.appendChild(caret);
    li.appendChild(icon);
    li.appendChild(name);
    li.appendChild(meta);
    li.appendChild(actions);

    li.addEventListener("click", function () {
      if (entry.type === "d") {
        toggleDir(fullPath);
      } else if (window.Viewer && typeof window.Viewer.open === "function") {
        window.Viewer.open(fullPath);
      } else {
        downloadFile(fullPath);
      }
    });

    return li;
  }

  function renderTree() {
    if (state.searchActive) return;  // search mode owns the list
    clearList();
    var root = state.path;
    var node = state.tree[root];
    if (!node || !node.loaded) {
      renderEmpty("loading…");
      return;
    }
    if (!node.entries.length) {
      renderEmpty("(empty)");
      return;
    }
    var frag = document.createDocumentFragment();
    renderInto(frag, root, 0);
    fileList.appendChild(frag);
  }

  function renderInto(frag, path, depth) {
    var node = state.tree[path];
    if (!node) return;
    node.entries.forEach(function (entry) {
      var full = joinPath(path, entry.name);
      frag.appendChild(makeRow(entry, full, depth));
      if (entry.type === "d") {
        var child = state.tree[full];
        if (child && child.expanded && child.loaded) {
          renderInto(frag, full, depth + 1);
        }
      }
    });
  }

  function toggleDir(path) {
    var node = ensureNode(path);
    if (node.expanded) {
      node.expanded = false;
      renderTree();
      return;
    }
    node.expanded = true;
    if (!node.loaded) {
      fetchDir(path).then(renderTree).catch(function (err) {
        node.expanded = false;
        alert("list failed: " + (err.message || err));
        renderTree();
      });
    } else {
      renderTree();
    }
  }

  function refresh() {
    // Reload the current root + invalidate all cached children so an
    // expansion below picks up edits made via the terminal.
    state.tree = Object.create(null);
    return fetchDir(state.path).then(function (canonical) {
      if (canonical !== state.path) setPath(canonical);
      renderTree();
    }).catch(function (err) {
      renderEmpty(String(err.message || err));
    });
  }

  function navigate(p) {
    setPath(p);
    state.tree = Object.create(null);
    return fetchDir(state.path).then(function (canonical) {
      if (canonical !== state.path) setPath(canonical);
      renderTree();
    }).catch(function (err) {
      renderEmpty(String(err.message || err));
    });
  }

  // --- Search -------------------------------------------------------

  var searchTimer = null;
  var SEARCH_DEBOUNCE_MS = 250;

  function setSearchActive(active) {
    state.searchActive = active;
    fileList.hidden = active;
    searchResults.hidden = !active;
    searchClear.hidden = !searchInput.value;
  }

  function runSearch(q) {
    if (!q || !q.trim()) {
      setSearchActive(false);
      renderTree();
      return;
    }
    setSearchActive(true);
    searchResults.innerHTML = "";
    var loading = document.createElement("li");
    loading.className = "tree-meta";
    loading.style.padding = "8px 10px";
    loading.textContent = "searching…";
    searchResults.appendChild(loading);

    fetch("/api/files/search?q=" + encodeURIComponent(q) + "&root=/", {
      credentials: "same-origin",
    }).then(function (r) {
      return r.ok ? r.json() : r.text().then(function (t) {
        throw new Error("search failed: " + (t || r.status));
      });
    }).then(function (j) {
      searchResults.innerHTML = "";
      var results = j.results || [];
      if (!results.length) {
        var empty = document.createElement("li");
        empty.className = "empty";
        empty.style.padding = "8px 10px";
        empty.style.color = "var(--text-3)";
        empty.style.fontStyle = "italic";
        empty.textContent = 'no matches for "' + q + '"';
        searchResults.appendChild(empty);
        return;
      }
      var frag = document.createDocumentFragment();
      results.forEach(function (r) {
        var li = document.createElement("li");
        li.className = "tree-row " + (
          r.type === "d" ? "dir" :
          r.type === "l" ? "link" : "file"
        );
        var caret = document.createElement("span");
        caret.className = "caret";
        var icon = document.createElement("span");
        icon.className = "icon";
        icon.textContent = iconFor(r.type);
        var name = document.createElement("span");
        name.className = "name";
        name.textContent = r.name;
        var pathSpan = document.createElement("span");
        pathSpan.className = "result-path";
        pathSpan.textContent = r.path;
        li.appendChild(caret);
        li.appendChild(icon);
        li.appendChild(name);
        li.appendChild(pathSpan);
        li.addEventListener("click", function () {
          if (r.type === "d") {
            // Navigate the tree root to this directory; exit search.
            searchInput.value = "";
            setSearchActive(false);
            navigate(r.path);
          } else if (window.Viewer && typeof window.Viewer.open === "function") {
            window.Viewer.open(r.path);
          } else {
            downloadFile(r.path);
          }
        });
        frag.appendChild(li);
      });
      searchResults.appendChild(frag);
    }).catch(function (err) {
      searchResults.innerHTML = "";
      var li = document.createElement("li");
      li.className = "tree-error";
      li.style.padding = "8px 10px";
      li.textContent = String(err.message || err);
      searchResults.appendChild(li);
    });
  }

  searchInput.addEventListener("input", function () {
    var q = searchInput.value;
    searchClear.hidden = !q;
    if (searchTimer) clearTimeout(searchTimer);
    if (!q.trim()) {
      setSearchActive(false);
      renderTree();
      return;
    }
    searchTimer = setTimeout(function () { runSearch(q); }, SEARCH_DEBOUNCE_MS);
  });
  searchInput.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      searchInput.value = "";
      searchClear.hidden = true;
      setSearchActive(false);
      renderTree();
    }
  });
  searchClear.addEventListener("click", function () {
    searchInput.value = "";
    searchClear.hidden = true;
    setSearchActive(false);
    renderTree();
    searchInput.focus();
  });

  // --- File operations ---------------------------------------------

  function downloadFile(p) {
    var url = "/api/files/download?path=" + encodeURIComponent(p);
    var a = document.createElement("a");
    a.href = url;
    a.download = p.split("/").pop();
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  function deleteFile(p) {
    var fd = new FormData();
    fd.append("path", p);
    return fetch("/api/files/delete", {
      method: "POST", body: fd, credentials: "same-origin",
    }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) { alert("delete failed: " + (t || r.status)); });
      }
      refresh();
    });
  }

  function newFilePrompt() {
    var name = prompt("New file name (in " + state.path + "):");
    if (!name) return;
    if (name.indexOf("/") >= 0) { alert("file name cannot contain '/'"); return; }
    // Create an empty file by uploading a 0-byte Blob with the chosen name.
    var fd = new FormData();
    fd.append("path", state.path);
    fd.append("file", new Blob([""], { type: "text/plain" }), name);
    fetch("/api/files/upload", {
      method: "POST", body: fd, credentials: "same-origin",
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { alert("create failed: " + t); });
      refresh().then(function () {
        var full = joinPath(state.path, name);
        if (window.Viewer && typeof window.Viewer.open === "function") {
          window.Viewer.open(full);
        }
      });
    });
  }

  function mkdirPrompt() {
    var name = prompt("New folder name (in " + state.path + "):");
    if (!name) return;
    if (name.indexOf("/") >= 0) { alert("folder name cannot contain '/'"); return; }
    var fd = new FormData();
    fd.append("path", joinPath(state.path, name));
    fetch("/api/files/mkdir", {
      method: "POST", body: fd, credentials: "same-origin",
    }).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { alert("mkdir failed: " + t); });
      refresh();
    });
  }

  // Deep-link to the IDE — the new/open project wizard lives there.
  function gotoDev(action) {
    // Same-host: term.wizerith.ai → dev.wizerith.ai. Strip the leading
    // "term." subdomain and prepend "dev." so this also works on .ald3
    // hostnames in case a dev.ald3.com ever lands.
    var host = location.host.replace(/^term\./, "dev.");
    var url = location.protocol + "//" + host + "/?action=" + encodeURIComponent(action);
    if (state.path && state.path !== "/") {
      url += "&path=" + encodeURIComponent(state.path);
    }
    window.location.assign(url);
  }

  // --- Upload pipeline ---------------------------------------------

  var uploadCounter = 0;

  function showProgress(item) {
    progressEl.hidden = false;
    progressEl.appendChild(item);
  }

  function uploadOne(file) {
    if (file.size > MAX_UPLOAD) {
      alert("'" + file.name + "' is " + fmtSize(file.size) +
            " — exceeds 1 GB limit");
      return Promise.resolve();
    }
    var id = ++uploadCounter;
    var item = document.createElement("div");
    item.className = "item";
    item.dataset.id = id;
    var label = document.createElement("span");
    label.textContent = file.name + " (" + fmtSize(file.size) + ")";
    var pct = document.createElement("span");
    pct.className = "pct";
    pct.textContent = "0%";
    item.appendChild(label);
    item.appendChild(pct);
    showProgress(item);

    return new Promise(function (resolve) {
      var fd = new FormData();
      fd.append("path", state.path);
      fd.append("file", file, file.name);
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/files/upload");
      xhr.withCredentials = true;
      xhr.upload.addEventListener("progress", function (e) {
        if (e.lengthComputable) {
          var p = Math.round((e.loaded / e.total) * 100);
          pct.textContent = p + "%";
        }
      });
      xhr.addEventListener("load", function () {
        if (xhr.status >= 200 && xhr.status < 300) {
          item.classList.add("done");
          pct.textContent = "✓";
          setTimeout(function () {
            item.remove();
            if (!progressEl.firstChild) progressEl.hidden = true;
          }, 2500);
        } else {
          item.classList.add("err");
          pct.textContent = "✗ " + xhr.status;
        }
        refresh();
        resolve();
      });
      xhr.addEventListener("error", function () {
        item.classList.add("err");
        pct.textContent = "✗ network";
        resolve();
      });
      xhr.send(fd);
    });
  }

  function uploadFiles(files) {
    var arr = Array.prototype.slice.call(files);
    arr.reduce(function (p, f) {
      return p.then(function () { return uploadOne(f); });
    }, Promise.resolve());
  }

  uploadInput.addEventListener("change", function () {
    if (uploadInput.files && uploadInput.files.length) {
      uploadFiles(uploadInput.files);
      uploadInput.value = "";
    }
  });

  // --- Drag and drop -----------------------------------------------

  var dragDepth = 0;
  ["dragenter", "dragover"].forEach(function (evname) {
    sidebar.addEventListener(evname, function (e) {
      if (!e.dataTransfer || !Array.prototype.includes.call(e.dataTransfer.types || [], "Files")) return;
      e.preventDefault();
      e.stopPropagation();
      if (evname === "dragenter") dragDepth++;
      dropZone.classList.add("dragging");
    });
  });
  sidebar.addEventListener("dragleave", function () {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropZone.classList.remove("dragging");
  });
  sidebar.addEventListener("drop", function (e) {
    if (!e.dataTransfer || !e.dataTransfer.files) return;
    e.preventDefault();
    e.stopPropagation();
    dragDepth = 0;
    dropZone.classList.remove("dragging");
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
  dropZone.addEventListener("click", function () { uploadInput.click(); });

  // --- Button wiring -----------------------------------------------

  upBtn.addEventListener("click", function () { navigate(parentOf(state.path)); });
  refreshBtn.addEventListener("click", function () { refresh(); });
  newFileBtn.addEventListener("click", newFilePrompt);
  newFolderBtn.addEventListener("click", mkdirPrompt);
  newProjectBtn.addEventListener("click", function () { gotoDev("new-project"); });
  openProjectBtn.addEventListener("click", function () { gotoDev("open-project"); });
  pathInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") navigate(pathInput.value || "/");
  });

  // --- Resizer ------------------------------------------------------

  (function () {
    var dragging = false;
    var startX = 0;
    var startW = 0;
    resizer.addEventListener("mousedown", function (e) {
      dragging = true;
      resizer.classList.add("dragging");
      startX = e.clientX;
      var layout = document.getElementById("layout");
      var col = getComputedStyle(layout).getPropertyValue("--sidebar-w") || "320px";
      startW = parseInt(col, 10) || 320;
      e.preventDefault();
    });
    document.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      var w = Math.max(180, Math.min(window.innerWidth - 240, startW + (e.clientX - startX)));
      document.getElementById("layout").style.setProperty("--sidebar-w", w + "px");
    });
    document.addEventListener("mouseup", function () {
      dragging = false;
      resizer.classList.remove("dragging");
    });
  })();

  // --- Deep-link boot ----------------------------------------------

  function bootFromUrl() {
    var params = new URLSearchParams(location.search);
    var p = params.get("path");
    if (p && p.startsWith("/")) {
      setPath(p);
    } else {
      setPath("/workspace");
    }
    refresh();
  }
  bootFromUrl();

  window.Files = {
    cwd: function () { return state.path; },
    navigate: navigate,
    refresh: refresh,
  };

  // Workspace switch hook (Phase: shared-workspace).
  window.__termResetFileList = function () {
    setPath("/workspace");
    refresh();
  };
})();
