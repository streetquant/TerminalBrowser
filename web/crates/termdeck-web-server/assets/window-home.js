"use strict";

// Windows-list presentation is independent of terminal rendering and tab
// management. Retain nodes across inventory refreshes so keyboard focus,
// search state, and click handlers do not reset on every host revision.
window.T1000WindowHome = (() => {
  const views = new WeakMap();
  const element = (tag, className = "", text) => {
    const node = document.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const setText = (node, text) => {
    if (node.textContent !== text) node.textContent = text;
  };
  function createEntry(id) {
    const entry = element("div", "window-list-entry");
    entry.dataset.id = id;
    const link = element("a", "window-list-row");
    const description = element("span", "window-list-description");
    const name = element("strong");
    const paneTitles = element("span");
    description.append(name, paneTitles);
    const count = element("span", "window-list-count");
    link.append(element("span", "window-list-icon", ">_"), description, count);
    const kill = element("button", "window-list-kill danger", "Kill window…");
    kill.type = "button";
    kill.addEventListener("click", () => window.T1000Management.killWindow(entry.dataset.id));
    entry.append(link, kill);
    return { entry, link, name, paneTitles, count, kill, searchText: "" };
  }
  function refreshEntry(view, data, appPath) {
    const name = String(data.name);
    const titles = data.panes.map(pane => pane.title).join(" · ") || "No visible tabs";
    const href = appPath(data.path);
    const aria = `Kill ${name} window permanently`;
    if (view.link.getAttribute("href") !== href) view.link.setAttribute("href", href);
    setText(view.name, name);
    setText(view.paneTitles, titles);
    setText(view.count, `${data.panes.length} tabs`);
    if (view.kill.getAttribute("aria-label") !== aria) view.kill.setAttribute("aria-label", aria);
    view.searchText = [name, ...data.panes.map(pane => `${pane.title} ${pane.cwd}`)]
      .join(" ").toLocaleLowerCase();
  }
  function renderHome(root, host, stale, appPath) {
    let view = views.get(root);
    if (!view) {
      root.className = "window-list-main";
      const header = element("header", "window-list-header");
      const brand = element("a", "workspace-home", "t1000");
      brand.href = appPath("/");
      const search = element("input", "window-filter");
      search.type = "search";
      search.placeholder = "Find a window or tab";
      search.setAttribute("aria-label", search.placeholder);
      const status = element("p", "window-list-status");
      status.setAttribute("role", "status");
      const list = element("nav", "window-list");
      list.setAttribute("aria-label", "t1000 windows");
      header.append(brand, element("h1", "", "Windows"), search, status);
      window.T1000Management.home(header);
      root.replaceChildren(header, list);
      view = { search, status, list, rows: new Map(), empty: element("p", "subtle"), signature: null };
      views.set(root, view);
      search.addEventListener("input", () => renderHome(root, root.t1000Host, root.t1000Stale, appPath));
    }
    root.t1000Host = host;
    root.t1000Stale = stale;
    const query = view.search.value.trim().toLocaleLowerCase();
    const status = stale ? "Connection interrupted. Showing the last known windows."
      : `${host.windows.length} windows · ${host.totalPanes} tabs`;
    setText(view.status, status);
    const signature = JSON.stringify([query, host.revision]);
    if (view.signature === signature) return;

    const visible = [];
    const present = new Set();
    for (const data of host.windows) {
      const id = String(data.id);
      if (present.has(id)) continue; // Never create duplicate interactive controls.
      present.add(id);
      let row = view.rows.get(id);
      if (!row) {
        row = createEntry(id);
        view.rows.set(id, row);
      }
      refreshEntry(row, data, appPath);
      if (!query || row.searchText.includes(query)) visible.push(row.entry);
    }
    for (const id of view.rows.keys()) {
      if (!present.has(id)) view.rows.delete(id);
    }
    if (!visible.length) {
      setText(view.empty, query ? "No matching windows or tabs."
        : "No windows yet. Choose New window to get started.");
      visible.push(view.empty);
    }

    // Move only changed/reordered nodes, retaining stable focus and event
    // listeners. No unsafe markup construction, repeated row construction, or global redraw.
    const { list } = view;
    const priorFocus = document.activeElement;
    const focusWasHere = priorFocus && list.contains(priorFocus);
    let cursor = list.firstChild;
    for (const row of visible) {
      if (row === cursor) cursor = cursor.nextSibling;
      else list.insertBefore(row, cursor);
    }
    while (cursor) {
      const next = cursor.nextSibling;
      list.removeChild(cursor);
      cursor = next;
    }
    if (focusWasHere && list.contains(priorFocus) && document.activeElement !== priorFocus) {
      priorFocus.focus({ preventScroll: true });
    }
    view.signature = signature;
  }
  window.T1000UI.renderHome = renderHome;
  return { renderHome };
})();
