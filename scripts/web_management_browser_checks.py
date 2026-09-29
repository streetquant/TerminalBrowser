"""Browser checks called only inside the private terminal-browser fixture."""
from __future__ import annotations


def is_cancelled_manager_read(failure):
    """Navigation may cancel telemetry; cancelled mutations still fail proof."""
    from urllib.parse import urlsplit
    return (failure.get("failure") == "net::ERR_ABORTED"
            and failure.get("management_operation") == "list"
            and urlsplit(failure.get("url", "")).path == "/_termdeck/api/v1/management")


def check_management(page, *, origin, database, initial_id, created_id, output, result, database_row, wait_until, click_action, t1000, run_root, base_env, run_json):
    ids = [initial_id, created_id]
    before = {item: database_row(database, item) for item in ids}
    dialog = page.locator("#management-dialog")

    def manager():
        click_action(page, "Tab manager")
        dialog.get_by_role("heading", name="Tab manager", exact=True).wait_for()
        dialog.locator("tbody tr").first.wait_for()

    def select(items):
        checked = dialog.locator('tbody input[type=checkbox]:checked')
        while checked.count():
            checked.first.uncheck()
        for item in items:
            dialog.locator(f'tr[data-session-id="{item}"] input[type=checkbox]').check()

    def action(label):
        selected_action = dialog.locator(".manager-actions").get_by_role("button", name=label, exact=True)
        (selected_action if selected_action.count() else dialog.get_by_role("button", name=label, exact=True)).click()
        if label in {"Pin", "Unpin", "Resume"}:
            page.wait_for_function("() => document.querySelector('.management-notice')?.textContent === 'Changes saved.'")

    def close():
        page.wait_for_function("() => !document.querySelector('#management-dialog').hasAttribute('aria-busy')")
        dialog.get_by_role("button", name="Close", exact=True).click()
        dialog.wait_for(state="hidden")
        # Closing stops future live refreshes. Let an already-started read finish
        # before the fixture navigates or tears down its browser context.
        page.wait_for_load_state("networkidle")

    def confirmed(label):
        action(label)
        confirm = page.locator(".management-confirm")
        confirm.get_by_role("button", name="Confirm", exact=True).click()
        page.wait_for_function("() => !document.querySelector('.management-confirm') && !document.querySelector('#management-dialog').hasAttribute('aria-busy') && /Changes saved|action\\(s\\) failed/.test(document.querySelector('.management-notice')?.textContent || '')")

    manager()
    for heading in ["Tab", "Window", "PID", "Health", "CPU %", "RAM", "Procs", "Runtime", "Command", "Directory"]:
        if dialog.get_by_role("columnheader", name=heading, exact=True).count() != 1:
            raise AssertionError(f"missing manager column: {heading}")
    # The full lifecycle suite intentionally leaves a fixture tab hidden.
    # Restore only these two live fixture identities before bulk-hide checks.
    select(ids)
    action("Reopen")
    page.wait_for_function("() => document.querySelector('.management-notice')?.textContent === 'Changes saved.'")
    wait_until(lambda: all(database_row(database, item)["visible"] == 1 for item in ids), timeout=10, detail="manager fixture reopen")
    select(ids)
    action("Pin")
    wait_until(lambda: all(database_row(database, item)["pinned"] == 1 for item in ids), timeout=10, detail="bulk pin")
    select(ids)
    confirmed("Hide")
    dialog.get_by_text("2 action(s) failed.", exact=False).wait_for()
    if not all(database_row(database, item)["visible"] == 1 for item in ids):
        raise AssertionError("bulk hide bypassed pin protection")
    select(ids)
    action("Unpin")
    wait_until(lambda: all(database_row(database, item)["pinned"] == 0 for item in ids), timeout=10, detail="bulk unpin")
    select(ids)
    confirmed("Pause")
    wait_until(lambda: all(database_row(database, item)["status"] == "paused" for item in ids), timeout=10, detail="bulk pause")
    select(ids)
    action("Resume")
    wait_until(lambda: all(database_row(database, item)["status"] == "running" for item in ids), timeout=10, detail="bulk resume")
    select(ids)
    dialog.locator(".manager-composer summary").click()
    composer = dialog.get_by_label("Text for selected terminals", exact=True)
    composer.fill("MANAGER_CANCELLED_INPUT")
    action("Send text")
    page.locator(".management-confirm").get_by_role("button", name="Cancel", exact=True).click()
    logs = [database.parent / "logs/initial.raw", database.parent / "logs/created.raw"]
    if any(path.exists() and b"MANAGER_CANCELLED_INPUT" in path.read_bytes() for path in logs):
        raise AssertionError("cancelled manager input reached a terminal")
    for label, marker, enter in [("Send text", "MANAGER_TEXT_ONCE", False), ("Send + Enter", "MANAGER_ENTER_ONCE", True)]:
        composer.fill(marker)
        action(label)
        page.locator(".management-confirm").get_by_role("button", name="Send to selected terminals", exact=True).click()
        # Closing the confirmation schedules its close event. The old send's
        # success remains visible until the next snapshot arrives. Wait for the
        # confirmation handler and this whole operation, not that stale notice.
        page.wait_for_function("() => !document.querySelector('.management-confirm') && !document.querySelector('#management-dialog').hasAttribute('aria-busy') && /Sent to|action\\(s\\) failed/.test(document.querySelector('.management-notice')?.textContent || '')")
        notice = dialog.locator('.management-notice').inner_text()
        if notice != 'Sent to 2 terminal(s).':
            page.screenshot(path=str(output / 'manager-send-failure.png'))
            raise AssertionError(f'manager input failed: {notice}')
        def received():
            for path in logs:
                raw = path.read_bytes() if path.exists() else b''
                if marker.encode() not in raw:
                    return False
                if enter and b'\r' not in raw[raw.index(marker.encode()) + len(marker):]:
                    return False
            return True
        # tmux acknowledges queuing before the child reads and logs the bytes.
        # Wait for both text and Enter, not just the earlier text receipt.
        wait_until(received, timeout=10, detail="manager input and optional Enter received")
        for path in logs:
            raw = path.read_bytes()
            if raw.count(marker.encode()) != 1:
                raise AssertionError("manager input was duplicated")
            if enter and b"\r" not in raw[raw.index(marker.encode()) + len(marker):]:
                raise AssertionError("Send + Enter omitted Enter")
    dialog.locator(".manager-composer summary").click()
    result["assertions"]["manager_input_confirmation_exactly_once_and_enter"] = True
    select([created_id])
    action("Rename")
    dialog.get_by_label("Tab name", exact=True).fill("Manager renamed λ")
    action("Save tab name")
    wait_until(lambda: database_row(database, created_id)["name"] == "Manager renamed λ", timeout=10, detail="manager rename")
    select([created_id])
    action("Preview")
    page.wait_for_function("() => document.querySelector('.manager-preview').textContent !== 'Loading…'")
    dialog.locator(".manager-preview").wait_for(state="visible")
    if "KEYBOARD_PASTE_MOUSE_RESIZE_READY" not in dialog.locator(".manager-preview").inner_text():
        raise AssertionError("manager output preview did not capture the selected live terminal")
    page.wait_for_timeout(5200)
    if not dialog.locator(f'tr[data-session-id="{created_id}"] input').is_checked():
        raise AssertionError("live manager refresh lost selection")
    dialog.screenshot(path=str(output / "tab-manager-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 760})
    if dialog.evaluate("el => el.getBoundingClientRect().right > innerWidth || el.getBoundingClientRect().left < 0"):
        raise AssertionError("manager escapes mobile viewport")
    page.screenshot(path=str(output / "tab-manager-mobile.png"))
    page.set_viewport_size({"width": 1440, "height": 900})
    close()
    # A click is the user's explicit request to reclaim WebSocket control
    # after management input. Programmatic DOM focus alone is not a claim.
    page.locator('#terminal-xterm').click(position={"x": 100, "y": 100})
    page.wait_for_function(
        "() => document.activeElement?.classList?.contains('xterm-helper-textarea') === true"
    )
    page.keyboard.type('AFTER_MANAGER_CONTROL_RELEASE', delay=2)
    wait_until(lambda: any(path.exists() and b'AFTER_MANAGER_CONTROL_RELEASE' in path.read_bytes() for path in logs), timeout=10, detail='keyboard control after manager send')
    result["assertions"]["keyboard_control_returns_after_manager_send"] = True
    result["assertions"]["manager_selection_resources_preview_bulk_and_pin_protection"] = True

    # CSRF remains mandatory even for management's read-like POST operation.
    status = page.evaluate("""async () => (await fetch('/_termdeck/api/v1/management', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({operation:'list'})})).status""")
    if status != 403:
        raise AssertionError(f"management accepted missing CSRF: {status}")
    result["assertions"]["management_requires_csrf"] = True

    page.goto(origin + "/")
    page.get_by_role("button", name="New window", exact=True).click()
    dialog.get_by_label("Window name", exact=True).fill("Management fixture")
    action("Create window")
    page.get_by_role("button", name="Rename window", exact=True).wait_for()
    page.get_by_role("button", name="Rename window", exact=True).click()
    dialog.get_by_label("Window name", exact=True).fill("Management renamed λ")
    action("Save name")
    page.wait_for_function("() => document.querySelector('.workspace-title')?.textContent === 'Management renamed λ'")
    manager()
    select(ids)
    action("Move…")
    dialog.get_by_label("Destination window", exact=True).select_option(label="Management renamed λ")
    action("Move tabs")
    page.wait_for_function("() => document.querySelectorAll('[role=tab]').length === 2")
    for item in ids:
        after = database_row(database, item)
        if after["window_title"] != "Management renamed λ" or after["last_pid"] != before[item]["last_pid"] or after["tmux_name"] != before[item]["tmux_name"]:
            raise AssertionError("cross-window move changed process identity or failed")
    result["assertions"]["window_creation_rename_move_preserves_live_processes"] = True

    first = page.locator('[role="tab"]').first
    second = page.locator('[role="tab"]').nth(1)
    first_id = first.get_attribute("data-pane-id")
    second_id = second.get_attribute("data-pane-id")
    first.drag_to(second)
    wait_until(lambda: database_row(database, first_id)["tab_index"] > database_row(database, second_id)["tab_index"], timeout=10, detail="drag tab reorder")
    page.wait_for_function("id => document.querySelectorAll('[role=tab]')[0]?.dataset.paneId === id", arg=second_id)
    page.locator(f'[role=tab][data-pane-id="{first_id}"]').click()
    page.keyboard.press("Control+Shift+PageUp")
    wait_until(lambda: database_row(database, first_id)["tab_index"] < database_row(database, second_id)["tab_index"], timeout=10, detail="keyboard tab reorder")
    result["assertions"]["drag_and_keyboard_reorder_persist"] = True

    click_action(page, "Settings · themes and fonts")
    settings = page.locator("#workspace-tools-dialog")
    settings.get_by_label("Interface theme", exact=True).select_option("light")
    settings.get_by_label("Density", exact=True).select_option("comfortable")
    settings.get_by_label("Scrollback lines", exact=True).fill("30000")
    settings.get_by_label("Scrollback lines", exact=True).press("Tab")
    settings.get_by_label("Cursor color", exact=True).fill("#00ff88")
    settings.get_by_label("Cursor color", exact=True).press("Tab")
    settings.get_by_label("Interface scale", exact=True).fill("1.15")
    settings.get_by_label("Interface scale", exact=True).press("Tab")
    settings.get_by_label("Reduce motion", exact=True).check()
    save = settings.get_by_role("button", name="Save desktop defaults", exact=True)
    if not save.is_disabled():
        raise AssertionError("saving desktop defaults does not require explicit consent")
    settings.get_by_label("Update desktop defaults for the next desktop launch", exact=True).check()
    save.click()
    settings.get_by_text("Desktop defaults saved.", exact=False).wait_for()
    import json
    config = json.loads((database.parent / "home/.config/termdeck/config.json").read_text())
    if config["appearance"]["chrome_theme"] != "light" or config["appearance"]["terminal"]["scrollback_lines"] != 30000:
        raise AssertionError("desktop defaults were not saved")
    settings.get_by_label("Interface theme", exact=True).select_option("noir")
    settings.get_by_role("button", name="Load desktop settings", exact=True).click()
    settings.get_by_text("Desktop settings loaded into this browser.", exact=False).wait_for()
    if settings.get_by_label("Interface theme", exact=True).input_value() != "light":
        raise AssertionError("desktop settings import did not apply")
    result["assertions"]["explicit_desktop_settings_exchange"] = True
    page.screenshot(path=str(output / "settings-light.png"))
    settings.get_by_role("button", name="Close", exact=True).click()
    page.reload()
    page.wait_for_function("() => document.querySelector('#workspace-terminal')?.termdeckTerminal || document.querySelector('.xterm-host')?.termdeckTerminal")
    saved = page.evaluate("JSON.parse(localStorage.getItem('t1000.appearance'))")
    if saved["chrome"] != "light" or saved["scrollback"] != 30000 or saved["cursorColor"] != "#00ff88" or saved["uiScale"] != 1.15:
        raise AssertionError(f"settings did not persist: {saved}")
    for width, height, scale, theme in [(1440, 900, 1.75, 'light'), (390, 760, 1, 'noir'), (320, 568, 1.75, 'dark')]:
        click_action(page, "Settings · themes and fonts")
        settings.get_by_label("Interface scale", exact=True).fill(str(scale))
        settings.get_by_label("Interface scale", exact=True).press("Tab")
        settings.get_by_label("Interface theme", exact=True).select_option(theme)
        settings.get_by_role("button", name="Close", exact=True).click()
        page.set_viewport_size({"width": width, "height": height})
        bounds = page.evaluate("""() => {
          const box = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,height:r.height}; };
          return {header:box('.workspace-headerbar'),tabs:box('.workspace-tabbar'),title:box('.workspace-title'),footer:box('.workspace-statusbar'),overflow:document.documentElement.scrollWidth > innerWidth};
        }""")
        if bounds['overflow'] or bounds['title']['bottom'] > bounds['tabs']['top'] + 1 or bounds['header']['bottom'] > bounds['tabs']['top'] + 1 or bounds['footer']['top'] - bounds['tabs']['bottom'] < 100:
            raise AssertionError(f"scaled workspace layout overlaps at {width}/{scale}: {bounds}")
        page.screenshot(path=str(output / f"workspace-{theme}-{width}-scale-{scale}.png"))
        page.set_viewport_size({"width": 1440, "height": 900})
    result["assertions"]["responsive_chrome_and_maximum_interface_scale"] = True
    click_action(page, "Settings · themes and fonts")
    settings.get_by_role("button", name="Reset to defaults", exact=True).click()
    settings.get_by_role("button", name="Close", exact=True).click()
    page.screenshot(path=str(output / "workspace-charcoal.png"), animations="disabled")
    result["assertions"]["complete_appearance_controls_and_persistence"] = True

    # Window-scoped bulk hide has both cancel and confirm paths; reopen never
    # creates a replacement process.
    page.locator(f'[role=tab][data-pane-id="{initial_id}"]').click()
    click_action(page, "Hide other tabs")
    page.locator(".management-confirm").get_by_role("button", name="Cancel", exact=True).click()
    if any(database_row(database, item)["visible"] != 1 for item in ids):
        raise AssertionError("cancelled bulk hide changed tabs")
    close()
    click_action(page, "Hide other tabs")
    page.locator(".management-confirm").get_by_role("button", name="Confirm", exact=True).click()
    wait_until(lambda: database_row(database, created_id)["visible"] == 0, timeout=10, detail="hide other tabs")
    page.wait_for_function("() => document.querySelector('.management-notice')?.textContent === 'Changes saved.'")
    close()
    manager(); select([created_id]); action("Reopen")
    page.wait_for_function("() => document.querySelector('.management-notice')?.textContent === 'Changes saved.'")
    if database_row(database, created_id)["last_pid"] != before[created_id]["last_pid"]:
        raise AssertionError("bulk reopen changed the terminal process")
    close()
    page.wait_for_function("() => document.querySelectorAll('[role=tab]').length === 2")
    right_id = page.locator('[role=tab]').nth(1).get_attribute("data-pane-id")
    page.locator('[role=tab]').first.click()
    click_action(page, "Hide tabs to the right")
    page.locator(".management-confirm").get_by_role("button", name="Confirm", exact=True).click()
    wait_until(lambda: database_row(database, right_id)["visible"] == 0, timeout=10, detail="hide right tabs")
    page.wait_for_function("() => document.querySelector('.management-notice')?.textContent === 'Changes saved.'")
    close()
    manager(); select([right_id]); action("Reopen")
    page.wait_for_function("() => document.querySelector('.management-notice')?.textContent === 'Changes saved.'")
    select([created_id]); confirmed("Archive")
    wait_until(lambda: database_row(database, created_id)["status"] == "archived", timeout=10, detail="archive tab")
    select([created_id]); action("Reopen")
    page.wait_for_function("() => document.querySelector('.management-notice')?.textContent === 'Changes saved.'")
    if database_row(database, created_id)["status"] != "running" or database_row(database, created_id)["last_pid"] != before[created_id]["last_pid"]:
        raise AssertionError("unarchive did not preserve the live terminal")
    select([created_id]); confirmed("Kill…")
    wait_until(lambda: database_row(database, created_id)["status"] == "stopped", timeout=10, detail="manager kill")
    from pathlib import Path
    wait_until(lambda: not Path(f'/proc/{before[created_id]["last_pid"]}').exists(), timeout=10, detail="manager kill terminated fixture process")
    select([created_id]); confirmed("Remove…")
    wait_until(lambda: database_row(database, created_id) is None, timeout=10, detail="remove stopped record")
    if database_row(database, initial_id) is None:
        raise AssertionError("remove touched an unselected tab")
    close()
    result["assertions"]["bulk_hide_reopen_archive_kill_remove_and_cancel"] = True

    # All destructive actions use only this fixture's isolated tmux server.
    fixture_ids = []
    for name in ("Kill fixture one", "Kill fixture two"):
        created = run_json([str(t1000), "new", "--db", str(database), "--cwd", str(run_root),
                            "--window-id", "win-kill-fixture", "--window-title", "Kill fixture",
                            "--name", name, "--command", "sleep 120"], env=base_env, timeout=40)
        fixture_ids.append(created["tabs"][0]["id"])
    from pathlib import Path
    fixture_pids = [database_row(database, item)["last_pid"] for item in fixture_ids]
    kill_page = page.context.new_page()
    try:
        kill_page.goto(origin + "/")
        entry = kill_page.locator(".window-list-entry").filter(has_text="Kill fixture")
        entry.get_by_role("button", name="Kill Kill fixture window permanently").click()
        kill_dialog = kill_page.locator("#management-dialog")
        kill_dialog.get_by_text("Kill fixture one", exact=False).wait_for()
        kill_dialog.get_by_text("Kill fixture two", exact=False).wait_for()
        csrf_status = kill_page.evaluate("""async () => (await fetch('/_termdeck/api/v1/management', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({operation:'kill-window',window_id:'win-kill-fixture'})
        })).status""")
        if csrf_status != 403:
            raise AssertionError(f"window kill accepted missing CSRF: {csrf_status}")
        kill_dialog.get_by_role("button", name="Cancel", exact=True).click()
        if any(database_row(database, item) is None for item in fixture_ids):
            raise AssertionError("cancelled window kill changed saved tabs")
        entry.get_by_role("button", name="Kill Kill fixture window permanently").click()
        kill_dialog.get_by_role("button", name="Kill window permanently", exact=True).click()
        wait_until(lambda: all(database_row(database, item) is None for item in fixture_ids),
                   timeout=15, detail="window kill removed both managed tabs")
        wait_until(lambda: all(pid and not Path(f"/proc/{pid}").exists() for pid in fixture_pids),
                   timeout=15, detail="window kill stopped both fixture processes")
        kill_page.wait_for_function("() => ![...document.querySelectorAll('.window-list-row')].some(row => row.textContent.includes('Kill fixture'))")
        if database_row(database, initial_id) is None:
            raise AssertionError("window kill touched an unrelated tab")
        result["assertions"]["window_kill_cascades_and_preserves_other_windows"] = True
    finally:
        kill_page.close()
