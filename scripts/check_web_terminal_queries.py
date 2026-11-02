#!/usr/bin/env python3
"""Check duplicate-query suppression and real input with built xterm assets.

The backing tmux pane answers capability/status requests. Its browser view
must render those requests without sending a second answer into the app.
"""
import argparse
import json
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "web/crates/termdeck-web-server/assets"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    queries = {'DA1': '\x1b[c', 'DA2': '\x1b[>c', 'identify': '\x1bZ',
               'DSR': '\x1b[5n', 'CPR': '\x1b[6n', 'private-CPR': '\x1b[?6n',
               'mode': '\x1b[4$p', 'private-mode': '\x1b[?25$p', 'status-string': '\x1bP$qm\x1b\\'}
    result = {'queries': {}, 'errors': []}
    # A private native pane must still receive exactly one answer from tmux.
    # Browser suppression is safe only while this positive control remains.
    private = Path(tempfile.mkdtemp(prefix='queries-', dir=ROOT/'.termdeck/qa'))
    probe = private/'probe.py'
    reply_file = private/'replies.json'
    probe.write_text("""import json,os,select,sys,time,tty
from pathlib import Path
tty.setraw(0);result={}
for name,query in [('DA1',b'\\x1b[c'),('DSR',b'\\x1b[5n'),('CPR',b'\\x1b[6n')]:
 os.write(1,query);data=b'';end=time.monotonic()+0.2
 while time.monotonic()<end:
  if select.select([0],[],[],max(0,end-time.monotonic()))[0]:data+=os.read(0,4096)
 result[name]=data.hex()
Path(sys.argv[1]).write_text(json.dumps(result))
time.sleep(30)
""")
    native = ['/usr/bin/tmux','-S',str(private/'tmux.sock')]
    try:
        subprocess.run([*native,'-f','/dev/null','new-session','-d','-s','qa-query',
                        shlex.join([sys.executable,str(probe),str(reply_file)])],check=True,capture_output=True)
        end=time.monotonic()+5
        while not reply_file.exists():
            if time.monotonic()>end:raise AssertionError('native tmux query probe did not finish')
            time.sleep(0.05)
        replies=json.loads(reply_file.read_text())
        assert bytes.fromhex(replies['DA1']).startswith(b'\x1b[?') and bytes.fromhex(replies['DA1']).count(b'c')==1,replies
        assert bytes.fromhex(replies['DSR'])==b'\x1b[0n',replies
        assert bytes.fromhex(replies['CPR']).startswith(b'\x1b[') and bytes.fromhex(replies['CPR']).count(b'R')==1,replies
        result['native_tmux_single_replies']=replies
    finally:
        subprocess.run([*native,'kill-server'],capture_output=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path='/usr/bin/brave', headless=True,
                                    args=['--disable-dev-shm-usage', '--no-first-run'])
        page = browser.new_page(viewport={'width':1000,'height':700})
        page.on('pageerror', lambda e: result['errors'].append(str(e)))
        page.set_content('<div id="terminal" style="width:900px;height:600px"></div>')
        page.add_style_tag(path=str(ASSETS/'terminal.css'))
        page.add_script_tag(path=str(ASSETS/'terminal.js'))
        page.evaluate("""() => {
          window.api=TermDeckTerminal.createTerminal(document.querySelector('#terminal'),{webgl:false});
          window.input=[];api.onData(bytes=>input.push(Array.from(bytes)));
        }""")
        for name, query in queries.items():
            replies = page.evaluate("""async query => {
              input=[];await new Promise(resolve=>api.write(query,resolve));
              await new Promise(resolve=>setTimeout(resolve,30));return input;
            }""", query)
            assert replies == [], (name, replies)
            result['queries'][name] = 'no duplicate browser reply'
        replies = page.evaluate('''async () => {
          api.reset();input=[];await new Promise(resolve=>api.write(String.fromCharCode(27)+'[c',resolve));
          return input;
        }''')
        assert replies == [], replies
        result['query_suppression_survives_reset'] = True
        page.evaluate('api.focus();input=[]')
        page.keyboard.press('Shift+F3')
        keys = page.evaluate('input.flat()')
        # Shift-F3 and a CPR reply can have identical bytes. Real keyboard input
        # must survive; filtering onData by an escape-sequence pattern is unsafe.
        assert bytes(keys) == b'\x1b[1;2R', keys
        result['modified_function_key_preserved'] = True
        pasted = '\x1b[?1;2c literal input'
        actual = page.evaluate('(text)=>{input=[];api.paste(text);return input.flat()}', pasted)
        assert bytes(actual).decode() == pasted, actual
        result['query_shaped_paste_preserved'] = True
        # Use the real mode switch rather than changing the terminal options.
        page.evaluate("()=>new Promise(r=>api.write(String.fromCharCode(27)+'[?2004h',r))")
        actual = page.evaluate('(text)=>{input=[];api.paste(text);return input.flat()}', 'QA paste')
        assert bytes(actual) == b'\x1b[200~QA paste\x1b[201~', actual
        result['bracketed_paste_preserved'] = True
        assert not result['errors'], result['errors']
        page.evaluate('api.dispose()');browser.close()
    result['ok'] = True
    (args.evidence_dir/'queries.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
