"""Rendered Chromium checks against a disposable TermDeck host only."""
import json
import os
from pathlib import Path
import re

from playwright.sync_api import expect, sync_playwright


def check_client_files(*,origin,cookies,root:Path,output:Path,assertions):
    output.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(executable_path="/usr/bin/chromium",headless=True,args=["--disable-dev-shm-usage","--no-first-run"])
        context=browser.new_context(viewport={"width":1280,"height":1000})
        context.add_cookies(cookies)
        page=context.new_page()
        errors=[]
        page.on("pageerror",lambda error:errors.append(str(error)))
        page.on("dialog",lambda dialog:dialog.accept())
        page.goto(origin+"/")
        expect(page.get_by_role("button",name="New window",exact=True)).to_be_visible()
        def snapshot():
            response=context.request.get(origin+"/_termdeck/api/v1/snapshot")
            assert response.ok,response.status
            return response.json()
        def inventory():
            return {window["id"]:(window["name"],tuple((pane["id"],pane["session_id"],pane["cwd"]) for pane in window["panes"]))
                    for window in snapshot()["windows"]}
        original_inventory=inventory()
        page.get_by_role("button",name="Client files",exact=True).click()
        modal=page.locator("#management-dialog")
        expect(modal.locator(".client-machine-status")).to_contain_text("no enrolled machine")
        modal.get_by_label("Machine label",exact=True).fill("Browser fixture A")
        modal.get_by_role("button",name="Enroll this browser's machine",exact=True).click()
        expect(modal.locator(".client-machine-status")).to_contain_text(" · enrolled")
        expect(modal.get_by_label("Select local files",exact=True)).to_be_enabled()
        machine_a=re.search(r"mach_[a-f0-9]{32}",modal.locator(".client-machine-status").inner_text()).group()
        page.evaluate("""() => { window.__uploadProgress=[]; new MutationObserver(() => { const p=document.querySelector('.client-files progress'); if(p) window.__uploadProgress.push(p.value); }).observe(document.querySelector('.client-files progress'),{attributes:true}); }""")
        modal.get_by_label("Select local files",exact=True).set_input_files({"name":"sample.txt","mimeType":"text/plain","buffer":b"a"*(600*1024)})
        try:
            expect(modal.locator('.client-file-row[data-status="ready"]')).to_have_count(1,timeout=20000)
        except AssertionError:
            page.screenshot(path=str(output/"upload-failure.png"),full_page=True)
            raise AssertionError({"notice":modal.locator(".client-files-notice").inner_text(),"page_errors":errors}) from None
        assert page.evaluate("window.__uploadProgress.some(v => v > 0 && v < 1)")
        expect(modal.locator("progress")).to_have_attribute("value","1")
        expect(modal.locator(".client-files-notice")).to_contain_text("Ready on TermDeck host")
        original=modal.locator('.client-file-row[data-status="ready"]').get_attribute("data-object-id")
        row=modal.locator(f'.client-file-row[data-object-id="{original}"][data-generation="1"]')
        row.locator('input[type="file"]').first.set_input_files({"name":"sample.txt","mimeType":"text/plain","buffer":b"changed"})
        expect(modal.locator(f'.client-file-row[data-object-id="{original}"][data-generation="2"]')).to_have_attribute("data-status","ready",timeout=20000)
        expect(row).to_have_attribute("data-status","changed")
        assertions["browser_explicit_selection_progress_and_immutable_refresh"]=True
        failed=False
        def fail_finalize(route):
            nonlocal failed
            body=route.request.post_data_json
            if body.get("operation")=="finalize" and not failed:
                failed=True
                route.fulfill(status=503,content_type="application/json",body=json.dumps({"ok":False,"error":"Fixture staging unavailable"}))
            else: route.continue_()
        page.route("**/_termdeck/api/v1/client-files",fail_finalize)
        modal.get_by_label("Select local files",exact=True).set_input_files({"name":"retry.txt","mimeType":"text/plain","buffer":b"retry"})
        expect(modal.locator(".client-files-notice")).to_contain_text("Upload failed: Fixture staging unavailable",timeout=20000)
        page.unroute("**/_termdeck/api/v1/client-files",fail_finalize)
        modal.get_by_role("button",name="Retry upload",exact=True).click()
        expect(modal.locator('.client-file-row[data-status="ready"]')).to_have_count(2,timeout=20000)
        directory=root/"browser-local-directory"; directory.mkdir(); (directory/"alpha.txt").write_text("directory alpha"); (directory/"sub").mkdir(); (directory/"sub/beta.txt").write_text("directory beta")
        modal.get_by_label("Select local directory",exact=True).set_input_files(directory)
        expect(modal.locator('.client-file-row[data-status="ready"]')).to_have_count(3,timeout=20000)
        modal.get_by_role("button",name="Rotate machine credential",exact=True).click()
        expect(modal.locator(".client-files-notice")).to_contain_text("credential rotated",timeout=10000)
        expect(modal.locator(".client-machine-status")).to_contain_text(machine_a)
        page.screenshot(path=str(output/"files-ready.png"),full_page=True)
        assertions["browser_directory_selection_failure_retry_and_rotation"]=True
        modal.get_by_role("button",name="Close",exact=True).click()
        page.get_by_role("button",name="New window",exact=True).click()
        modal.get_by_label("Window name",exact=True).fill("Browser configured")
        modal.get_by_text("Configure workspace, runtime and client files",exact=True).click()
        expect(modal.get_by_label("Server workspace",exact=True)).to_be_visible(timeout=10000)
        modal.get_by_label("Server workspace",exact=True).select_option("ws_"+"a"*32)
        runtime_choice=modal.get_by_label("Runtime",exact=True)
        expect(runtime_choice.locator('option[value="shell"]')).to_have_text("Native shell")
        expect(runtime_choice.locator('option[value="codex"]')).to_have_text("Codex (TermDeck private account)")
        expect(modal).to_contain_text("Runs on the host as your user. The selected folder is the starting directory.")
        task_input=modal.get_by_label("Codex task (optional)",exact=True)
        expect(task_input).to_be_disabled()
        runtime_choice.select_option("codex")
        expect(task_input).to_be_enabled()
        task_input.fill("Disposable option-selection check")
        runtime_choice.select_option("shell")
        expect(task_input).to_be_disabled()
        expect(task_input).to_have_value("")
        assertions["browser_native_shell_codex_choices_and_task_field"]=True
        modal.get_by_label("Initial tab names (one per line, maximum 8)",exact=True).fill("First\nSecond")
        modal.get_by_label("Attach sample.txt generation 2",exact=True).check()
        modal.evaluate("node => node.scrollTop = 0")
        page.screenshot(path=str(output/"new-window-configured.png"),full_page=True)
        delayed_snapshots=0
        def stale_snapshot(route):
            nonlocal delayed_snapshots
            response=route.fetch()
            body=response.json()
            if delayed_snapshots < 3 and any(window["name"]=="Browser configured" for window in body.get("windows",[])):
                body["windows"]=[window for window in body["windows"] if window["name"]!="Browser configured"]
                body["total_panes"]=sum(len(window["panes"]) for window in body["windows"])
                delayed_snapshots+=1
                route.fulfill(response=response,json=body)
            else: route.fulfill(response=response)
        page.route("**/_termdeck/api/v1/snapshot",stale_snapshot)
        modal.get_by_role("button",name="Create window",exact=True).click()
        page.wait_for_url("**/browser-configured",timeout=20000)
        page.unroute("**/_termdeck/api/v1/snapshot",stale_snapshot)
        assert delayed_snapshots==3
        expect(page.locator('.workspace-tabbar [role="tab"]')).to_have_count(2,timeout=20000)
        assertions["browser_new_window_navigation_and_initial_tabs"]=True
        assertions["browser_waits_for_stale_inventory_before_canonical_navigation"]=True
        page.goto(origin+"/")
        expect(page.locator(".window-list-row").filter(has_text="Browser configured")).to_have_count(1)
        expect(page.locator(".window-list-row").filter(has_text="Native integration")).to_have_count(1)
        expect(page.locator(".window-list-row").filter(has_text="Server directory integration")).to_have_count(1)
        saved_inventory=inventory()
        assert all(saved_inventory.get(key)==value for key,value in original_inventory.items())

        server_directory=root/"browser server project with spaces"
        server_directory.mkdir(mode=0o700)
        page.get_by_role("button",name="New window",exact=True).click()
        modal.get_by_label("Window name",exact=True).fill("Browser server directory")
        modal.get_by_text("Configure workspace, runtime and client files",exact=True).click()
        workspace_choice=modal.get_by_label("Server workspace",exact=True)
        expect(workspace_choice).to_be_visible(timeout=10000)
        expect(workspace_choice.locator('option[value="__server_directory__"]')).to_have_text("Enter server directory…")
        workspace_choice.select_option("__server_directory__")
        directory_input=modal.get_by_label("Server directory",exact=True)
        expect(directory_input).to_be_visible()
        expect(modal).to_contain_text("Choose an existing folder on the TermDeck host. Files selected below are uploaded copies from this client machine.")
        modal.get_by_label("Initial tab names (one per line, maximum 8)",exact=True).fill("Server path tab")
        modal.get_by_label("Attach sample.txt generation 2",exact=True).check()
        missing=root/"browser missing server directory"
        inaccessible=root/"browser inaccessible server directory"
        inaccessible.mkdir(mode=0o700)
        inaccessible.chmod(0)
        def create_response(response):
            return (response.request.method=="POST"
                    and response.url.endswith("/_termdeck/api/v1/management")
                    and response.request.post_data_json.get("operation")=="create-window")
        try:
            assert not os.access(inaccessible,os.R_OK|os.X_OK,effective_ids=True), "Inaccessible-directory proof requires an unprivileged fixture account"
            for invalid in (missing,inaccessible):
                directory_input.fill(str(invalid))
                with page.expect_response(create_response,timeout=20000) as refused:
                    modal.get_by_role("button",name="Create window",exact=True).click()
                assert refused.value.status==400,refused.value.status
                response=refused.value.json()
                assert response["code"]=="unavailable-workspace"
                expect(modal.locator(".management-notice")).to_contain_text(re.compile("Server directory.*(missing|inaccessible|readable|searchable)"))
                expect(modal).to_be_visible()
                expect(directory_input).to_have_value(str(invalid))
                expect(modal.get_by_label("Initial tab names (one per line, maximum 8)",exact=True)).to_have_value("Server path tab")
                expect(modal.get_by_label("Attach sample.txt generation 2",exact=True)).to_be_checked()
                expect(modal.get_by_role("button",name="Create window",exact=True)).to_be_enabled()
                assert inventory()==saved_inventory
            modal.evaluate("node => node.scrollTop = 0")
            page.screenshot(path=str(output/"server-directory-refused.png"),full_page=True)
        finally: inaccessible.chmod(0o700)
        assertions["browser_server_directory_invalid_inaccessible_refusal_preserves_form_and_windows"]=True

        directory_input.fill(str(server_directory))
        modal.evaluate("node => node.scrollTop = 0")
        page.screenshot(path=str(output/"new-window-server-directory.png"),full_page=True)
        with page.expect_response(create_response,timeout=20000) as creation:
            modal.get_by_role("button",name="Create window",exact=True).click()
        assert creation.value.ok,creation.value.status
        created=creation.value.json()
        assert created["ok"] and created["status"]=="started"
        submitted=creation.value.request.post_data_json
        assert submitted["workspace_path"]==str(server_directory) and "workspace_id" not in submitted
        page.wait_for_url("**/browser-server-directory",timeout=20000)
        expect(page.locator('.workspace-tabbar [role="tab"]')).to_have_count(1,timeout=20000)
        current=snapshot()
        current_window=next(window for window in current["windows"] if window["id"]==created["window_id"])
        assert page.url==origin+current_window["path"]
        assert [pane["cwd"] for pane in current_window["panes"]]==[str(server_directory)]
        assert [pane["session_id"] for pane in current_window["panes"]]==created["tabs"]
        current_inventory=inventory()
        assert all(current_inventory.get(key)==value for key,value in saved_inventory.items())
        assert set(current_inventory)==set(saved_inventory)|{created["window_id"]}
        page.goto(origin+"/")
        expect(page.locator(".window-list-row").filter(has_text="Browser server directory")).to_have_count(1,timeout=10000)
        expect(page.locator(".window-list-row").filter(has_text="Browser configured")).to_have_count(1)
        expect(page.locator(".window-list-row").filter(has_text="Native integration")).to_have_count(1)
        page.screenshot(path=str(output/"windows-after-server-directory.png"),full_page=True)
        assertions["browser_unregistered_server_directory_correction_navigation_and_immediate_inventory"]=True
        page.get_by_role("button",name="Client files",exact=True).click()
        expect(modal.locator(".client-machine-status")).to_contain_text(machine_a)
        row=modal.locator(f'.client-file-row[data-object-id="{original}"][data-generation="2"]')
        row.get_by_role("button",name="Details / attachments",exact=True).click()
        expect(row.get_by_role("button",name=re.compile("Detach from window:"))).to_have_count(2)
        assertions["browser_attachment_state_and_existing_windows_preserved"]=True
        other=browser.new_context(viewport={"width":1280,"height":1000}); other.add_cookies(cookies)
        page_b=other.new_page(); page_b.on("dialog",lambda dialog:dialog.accept()); page_b.goto(origin+"/")
        page_b.get_by_role("button",name="Client files",exact=True).click()
        modal_b=page_b.locator("#management-dialog")
        expect(modal_b.locator(".client-machine-status")).to_contain_text("no enrolled machine")
        modal_b.get_by_label("Machine label",exact=True).fill("Browser fixture B")
        modal_b.get_by_role("button",name="Enroll this browser's machine",exact=True).click()
        expect(modal_b.locator(".client-machine-status")).to_contain_text(" · enrolled")
        expect(modal_b.locator(".client-file-row")).to_have_count(0)
        modal_b.get_by_role("button",name="List enrolled machines",exact=True).click()
        with page_b.expect_response(
            lambda response: response.request.method == "POST"
            and response.url.endswith("/_termdeck/api/v1/machines")
            and response.request.post_data_json.get("operation") == "revoke"
            and response.request.post_data_json.get("machine_id") == machine_a
        ) as revoked:
            modal_b.get_by_role("button",name="Revoke Browser fixture A",exact=True).click()
        assert revoked.value.ok, revoked.value.status
        page.get_by_role("button",name="Refresh staged list",exact=True).click()
        expect(modal.locator(".client-machine-status")).to_contain_text("revoked",timeout=10000)
        expect(modal.get_by_label("Select local files",exact=True)).to_have_count(0)
        page.screenshot(path=str(output/"machine-revoked.png"),full_page=True)
        assertions["browser_new_context_machine_enrollment_and_revoked_state"]=True
        assert not errors,errors
        other.close(); context.close(); browser.close()
