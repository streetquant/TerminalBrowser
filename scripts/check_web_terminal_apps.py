#!/usr/bin/env python3
"""Qualify installed terminal behavior with real apps in a private tmux runtime.

Only disposable registries, panes, editor buffers and auth capabilities are used.
No model prompt is submitted and no user application receives test input.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

from playwright.sync_api import sync_playwright
import check_web_terminal_browser as proof

ROOT = Path(__file__).resolve().parents[1]


def visible(page):
    return page.evaluate("() => document.querySelector('#terminal-xterm')?.termdeckTerminal?.readBuffer() || ''")


def first_line(page):
    values = re.findall(r"QA_APP_LINE_(\d+)", visible(page))
    return min(map(int, values)) if values else None


def mode(page):
    return page.evaluate("""() => {
        const host=document.querySelector('#terminal-xterm'), t=host.termdeckTerminal.terminal;
        return {buffer:t.buffer.active.type,mouse:t.modes.mouseTrackingMode,
          owner:host.dataset.termdeckScrollOwner,cols:t.cols,rows:t.rows};
    }""")


def focus(page):
    page.evaluate("()=>document.querySelector('#terminal-xterm').termdeckTerminal.focus()")
    page.wait_for_function("()=>document.body.dataset.termdeckConnection==='live'")


def wheel(page, delta):
    box = page.locator('#terminal-xterm .xterm-screen').bounding_box()
    page.mouse.move(box['x'] + 100, box['y'] + 120)
    page.mouse.wheel(0, delta)


def moved(page, previous, direction):
    def check():
        current = first_line(page)
        return current is not None and (current > previous if direction > 0 else current < previous)
    proof.wait_until(check, timeout=10, detail='application scroll distance')
    return first_line(page)


def trackpad_app(page, direction=1):
    """A continuous gesture uses the app's own mouse/page protocol."""
    assert page.locator('.terminal-scroll-position,.terminal-scroll-controls').count()==0
    box=page.locator('#terminal-xterm .xterm-screen').bounding_box()
    page.mouse.move(box['x']+100,box['y']+120)
    for _ in range(6):
        page.mouse.wheel(0,24*direction)
        page.wait_for_timeout(16)


def scroll_app(page, trace=None):
    trace = {} if trace is None else trace
    before = first_line(page);trace['initial']=before
    wheel(page, 160)
    down = moved(page, before, 1)
    page.wait_for_timeout(300)
    down = first_line(page);trace['wheel_down']=down
    trackpad_app(page)
    dragged = moved(page, down, 1)
    page.wait_for_timeout(400)
    dragged = first_line(page);trace['trackpad_down']=dragged
    wheel(page, -160)
    up = moved(page, dragged, -1)
    page.wait_for_timeout(350)
    up = first_line(page);trace['wheel_up']=up
    focus(page)
    page.keyboard.press('PageDown')
    key = moved(page, up, 1);trace['keyboard_page_down']=key
    return {'initial':before,'wheel_down':down,'trackpad_down':dragged,'wheel_up':up,'keyboard_page_down':key}


def resize(page):
    before = mode(page)
    page.set_viewport_size({'width':1180,'height':760})
    proof.wait_until(lambda: (mode(page)['cols'],mode(page)['rows']) != (before['cols'],before['rows']),
                     timeout=10, detail='application grid resize')
    page.wait_for_timeout(250)
    after = mode(page)
    page.set_viewport_size({'width':1440,'height':900})
    return {'before':[before['cols'],before['rows']], 'after':[after['cols'],after['rows']]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--browser', default='/usr/bin/brave')
    parser.add_argument('--only', help='Comma separated fixture names')
    args = parser.parse_args()
    binary = args.server_binary.resolve()
    if not binary.is_file() or not os.access(binary,os.X_OK):
        parser.error('server binary must exist and be executable')
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(tempfile.mkdtemp(prefix='apps-',dir=ROOT/'.termdeck/qa'))
    home = root/'home'
    for d in [home/'.config',home/'.local/share',home/'.cache',root/'tmux',root/'auth-parent']:
        d.mkdir(mode=0o700,parents=True,exist_ok=True)
    env = {'HOME':str(home),'USER':pwd.getpwuid(os.getuid()).pw_name,'LOGNAME':pwd.getpwuid(os.getuid()).pw_name,
           'PATH':'/usr/bin:/bin','SHELL':'/bin/bash','LANG':'C.UTF-8',
           'TERM':'xterm-256color','COLORTERM':'truecolor','PYTHONPATH':str(ROOT),
           'TMUX_TMPDIR':str(root/'tmux'),'XDG_CONFIG_HOME':str(home/'.config'),
           'XDG_DATA_HOME':str(home/'.local/share'),'XDG_CACHE_HOME':str(home/'.cache'),
           'LESSHISTFILE':'-','PS1':'QA_SHELL_PROMPT> '}
    for key in ('XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS'):
        if key in os.environ:env[key]=os.environ[key]
    tmux,t1000 = proof.write_wrappers(root/'bin',home=home,tmux_tmpdir=root/'tmux',python=Path(sys.executable))
    db = root/'sessions.sqlite3'; auth=root/'auth-parent/auth'
    lines = root/'qa-lines.txt'
    lines.write_text(''.join(f'QA_APP_LINE_{i:04d}: disposable terminal application fixture\n' for i in range(2000)))
    shell = root/'shell.sh'
    shell.write_text('cat '+shlex.quote(str(lines))+"\nexport PS1='QA_SHELL_PROMPT> '\nexec /bin/bash --noprofile --norc -i\n")
    normal = root/'normal-mouse.py'
    normal.write_text(r"""import os,re,tty

tty.setraw(0);offset=0;pending=b''
os.write(1,b'\x1b[?1000h\x1b[?1006h')
def paint():
 os.write(1,b'\x1b[H'+''.join(f'QA_APP_LINE_{i:04d}: normal-screen mouse application\x1b[K\r\n' for i in range(offset,offset+25)).encode())
paint()
while True:
 data=os.read(0,4096)
 if not data:break
 pending+=data
 while (m:=re.search(rb'\x1b\[<(64|65);[0-9]+;[0-9]+M',pending)):
  offset=max(0,min(1800,offset+(3 if m[1]==b'65' else -3)));pending=pending[m.end():];paint()
 pending=pending[-64:]
""")
    nested = root/'nested.sock'; nested_conf=root/'nested.conf'
    nested_conf.write_text('set -g mouse on\nset -g status off\n')
    cases = [
        ('shell',['/bin/bash',str(shell)],'shell'),
        ('less',['/usr/bin/less',str(lines)],'scroll'),
        ('less-mouse',['/usr/bin/less','--mouse',str(lines)],'scroll'),
        ('vim',['/usr/bin/vim','-Nu','NONE','-n','-i','NONE','-c','set mouse= nomore',str(lines)],'editor'),
        ('vim-mouse',['/usr/bin/vim','-Nu','NONE','-n','-i','NONE','-c','set mouse=a nomore',str(lines)],'editor'),
        ('nvim',['/usr/bin/nvim','-u','NONE','-n','-i','NONE','--cmd','set mouse=',str(lines)],'editor'),
        ('nvim-mouse',['/usr/bin/nvim','-u','NONE','-n','-i','NONE','--cmd','set mouse=a',str(lines)],'editor'),
        ('nested-tmux',['/usr/bin/env','-u','TMUX','/usr/bin/tmux','-S',str(nested),'-f',str(nested_conf),'new-session','-s','qa-nested',shlex.join(['/usr/bin/less','--mouse',str(lines)])],'scroll'),
        ('normal-mouse',[sys.executable,'-u',str(normal)],'normal-mouse'),
        ('top',['/usr/bin/top','-d','0.5'],'monitor'),
    ]
    if args.only:
        selected=set(args.only.split(',')); cases=[c for c in cases if c[0] in selected]
        if not cases:parser.error('no matching fixture')
    result={'schema':'termdeck.terminal-apps.v1','binary_sha256':proof.sha256_file(binary),'cases':{},'errors':[]}
    server=None; browser=None; log=(out/'server.log').open('wb')
    try:
        tabs={}
        for name,command,kind in cases:
            if not Path(command[0]).exists():raise RuntimeError(f'missing app: {command[0]}')
            value=proof.run_json([str(t1000),'new','--db',str(db),'--cwd',str(root),'--window-id','qa-apps',
              '--window-title','Terminal apps','--name',name,'--command',shlex.join(command)],env=env)
            tabs[name]=value['tabs'][0]['id']
        origin=f'http://127.0.0.1:{proof.free_port()}'
        secret=proof.invitation(binary,auth,'Private app matrix')
        server=subprocess.Popen([str(binary),'--db',str(db),'--listen',origin[7:],'--refresh-ms','750',
          '--max-stale-ms','30000','--tmux',str(tmux),'--t1000',str(t1000),'--auth-dir',str(auth),
          '--host-id','host-browser-e2e','--owner-id','owner-browser-e2e','--allow-insecure-loopback-auth'],
          env=env,stdout=log,stderr=log,start_new_session=True)
        proof.wait_http(origin+'/_termdeck/healthz');cookies=proof.pair_cookies(origin,secret);secret=''
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path=args.browser,headless=True,args=['--disable-dev-shm-usage','--no-first-run'])
            ctx=browser.new_context(viewport={'width':1440,'height':900});ctx.add_cookies(cookies)
            result['browser']=browser.version
            for name,command,kind in cases:
                page=ctx.new_page();page.set_default_timeout(15000)
                page.on('pageerror',lambda e:result['errors'].append(str(e)))
                page.goto(origin+'/terminal-apps/t/'+tabs[name])
                proof.wait_until(lambda: bool(visible(page)),timeout=20,detail=name+' initial output')
                focus(page);page.wait_for_timeout(350)
                entry=result['cases'][name]={'mode':mode(page)}
                if kind=='shell':
                    proof.wait_until(lambda:'QA_SHELL_PROMPT>' in visible(page),timeout=10,detail='shell prompt')
                    before=page.evaluate("()=>document.querySelector('#terminal-xterm').termdeckTerminal.terminal.buffer.active.viewportY")
                    wheel(page,-160)
                    page.wait_for_function('y=>document.querySelector("#terminal-xterm").termdeckTerminal.terminal.buffer.active.viewportY<y',arg=before)
                    slider=page.locator('#terminal-xterm .scrollbar.vertical');slider.focus();page.keyboard.press('Home')
                    page.wait_for_function('()=>document.querySelector("#terminal-xterm").termdeckTerminal.terminal.buffer.active.viewportY===0')
                    page.get_by_role('button',name='Return to live output',exact=True).click()
                    focus(page);page.keyboard.type("printf 'QA_SHELL_INPUT_OK\\n'");page.keyboard.press('Enter')
                    proof.wait_until(lambda:re.search(r'(?m)^QA_SHELL_INPUT_OK\s*$',visible(page)),timeout=10,detail='shell executed typed command')
                    entry['normal_history_and_shell_input']=True
                elif kind=='monitor':
                    proof.wait_until(lambda:'top -' in visible(page),timeout=10,detail='top initial screen')
                    page.keyboard.press('h')
                    proof.wait_until(lambda:'Help' in visible(page) and 'Interactive' in visible(page),timeout=10,detail='top help keyboard input')
                    page.keyboard.press('Escape');page.wait_for_timeout(200);entry['keyboard_help']=True
                else:
                    proof.wait_until(lambda:first_line(page) is not None,timeout=15,detail=name+' fixture content')
                    if kind=='normal-mouse':
                        assert entry['mode']['buffer']=='normal' and entry['mode']['mouse']!='none'
                        assert entry['mode']['owner']=='application',entry['mode']
                        before=first_line(page);wheel(page,160);down=moved(page,before,1)
                        page.wait_for_timeout(400);down=first_line(page)
                        trackpad_app(page)
                        dragged=moved(page,down,1);entry['scroll']={'initial':before,'wheel_down':down,'trackpad_down':dragged}
                    else:
                        entry['scroll']=scroll_app(page)
                    if kind=='editor':
                        focus(page);page.keyboard.press('Home');page.keyboard.press('i')
                        page.keyboard.type('QA_EDITOR_DRAFT');page.keyboard.press('Escape')
                        proof.wait_until(lambda:'QA_EDITOR_DRAFT' in visible(page),timeout=10,detail=name+' insert text')
                        entry['unsaved_editor_input']=True
                        assert 'QA_EDITOR_DRAFT' not in lines.read_text()
                entry['resize']=resize(page)
                if kind not in ('shell','normal-mouse'):
                    focus(page)
                    if kind=='editor':
                        page.keyboard.type(':q!');page.keyboard.press('Enter')
                    else:
                        page.keyboard.press('q')
                    proof.wait_until(lambda:mode(page)['buffer']=='normal' and mode(page)['mouse']=='none',
                                     timeout=10,detail=name+' exit restores terminal modes')
                    try:
                        proof.wait_until(lambda:'[T1000 application exited; interactive shell ready]' in visible(page),
                                         timeout=10,detail=name+' returned shell ready')
                    except AssertionError:
                        (out/(name+'-exit-buffer.txt')).write_text(visible(page))
                        page.screenshot(path=str(out/(name+'-exit.png')))
                        raise
                    page.wait_for_timeout(150)
                    page.keyboard.type("printf 'QA_RETURNED_SHELL_OK\\n'");page.keyboard.press('Enter')
                    try:
                        proof.wait_until(lambda:re.search(r'(?m)^QA_RETURNED_SHELL_OK\s*$',visible(page)),
                                         timeout=10,detail=name+' shell input after exit')
                    except AssertionError:
                        (out/(name+'-exit-buffer.txt')).write_text(visible(page))
                        page.screenshot(path=str(out/(name+'-exit.png')))
                        print(json.dumps({'failed_shell':name,'mode':mode(page),'tail':visible(page)[-650:]}),flush=True)
                        raise
                    entry['exit_restores_shell_modes_and_input']=True
                entry['ok']=True
                page.screenshot(path=str(out/(name+'.png')))
                print(json.dumps({name:entry}),flush=True);page.close()
            assert not result['errors'],result['errors']
            result['ok']=True;browser.close();browser=None
    except BaseException:
        try:
            if browser is not None and page is not None:
                (out/'failure-buffer.txt').write_text(visible(page))
                page.screenshot(path=str(out/'failure.png'))
        except Exception:
            pass
        raise
    finally:
        if server is not None:
            server.terminate()
            try:server.wait(timeout=10)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
        subprocess.run([str(tmux),'kill-server'],env=env,capture_output=True,timeout=5)
        subprocess.run(['/usr/bin/tmux','-S',str(nested),'kill-server'],env=env,capture_output=True,timeout=5)
        log.close();(out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'ok':result.get('ok',False),'apps':len(result['cases']),'binary_sha256':result['binary_sha256']}))


if __name__=='__main__':
    main()
