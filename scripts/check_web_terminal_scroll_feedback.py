#!/usr/bin/env python3
"""Check native thumb geometry against actual terminal history and app boundaries."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from check_web_terminal_scroll import ASSETS, write, wheel, drag_history


def view(page):
    return page.evaluate("""() => {
      const p=probes.first,t=p.api.terminal.buffer.active,s=p.host.querySelector('.scrollbar.vertical'),thumb=s.querySelector('.slider');
      const track=s.getBoundingClientRect(),rect=thumb.getBoundingClientRect();
      return {value:Number(s.getAttribute('aria-valuenow')),maximum:Number(s.getAttribute('aria-valuemax')),
        viewport:t.viewportY,extent:t.baseY,rows:p.api.terminal.rows,
        hidden:s.getAttribute('aria-hidden')==='true',width:rect.width,height:rect.height,trackHeight:track.height,
        offset:rect.top-track.top,border:getComputedStyle(s).borderWidth,
        background:getComputedStyle(s).backgroundColor,opacity:Number(getComputedStyle(s).opacity),
        text:p.api.readBuffer(),input:p.input.slice(),focus:p.focus};
    }""")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir',type=Path,required=True)
    args=parser.parse_args();args.evidence_dir.mkdir(parents=True,exist_ok=True)
    result={'checks':{},'errors':[]}
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path='/usr/bin/brave',headless=True,args=['--disable-dev-shm-usage','--no-first-run'])
        page=browser.new_page(viewport={'width':1000,'height':800})
        page.on('pageerror',lambda e:result['errors'].append(str(e)))
        page.set_content('<!doctype html><html data-chrome="noir"><body class="workspace-page"><div id="first-shell" class="xterm-shell"><div id="first" class="xterm-host"></div></div></body></html>')
        for f in ('terminal.css','app.css'):page.add_style_tag(path=str(ASSETS/f))
        page.add_style_tag(content='body{margin:16px}.xterm-shell{position:relative;inset:auto;width:940px;height:740px}')
        for f in ('terminal.js','terminal-scroll.js'):page.add_script_tag(path=str(ASSETS/f))
        page.evaluate("""()=>{
          const host=document.querySelector('#first'),p={host,shell:host.parentElement,input:[],mine:true,focus:0};
          p.api=TermDeckTerminal.createTerminal(host,{webgl:false,cursorBlink:false,scrollback:1500});
          p.scroll=T1000TerminalScroll.install(host,p.api,{isController:()=>p.mine,
            onApplicationScroll:(direction,count)=>{p.input.push({direction,count});return true}});
          p.api.onData(bytes=>p.input.push({bytes:Array.from(bytes),wheel:p.scroll.isWheelInput()}));
          host.addEventListener('pointerdown',()=>p.focus++);window.probes={first:p};
        }""")
        write(page,'first','\r\n'.join(f'saved terminal history {i:04d}' for i in range(200))+'\r\n')
        page.wait_for_timeout(50);initial=view(page);wheel(page,-160);normal=view(page)
        result['normal']=normal
        result['checks']['history_thumb_follows_actual_viewport']=normal['value']==normal['viewport']<initial['viewport']
        result['checks']['slim_unframed_native_track']=normal['width']==5 and normal['border']=='0px' and normal['background']=='rgba(0, 0, 0, 0)' and normal['opacity']==1
        expected=normal['trackHeight']*normal['rows']/(normal['extent']+normal['rows'])
        result['checks']['thumb_height_proportional_to_real_history']=abs(normal['height']-expected)<2
        offset=(normal['trackHeight']-normal['height'])*normal['viewport']/normal['extent']
        result['checks']['thumb_offset_matches_visible_content']=abs(normal['offset']-offset)<2
        write(page,'first','more history\r\n'*400);page.wait_for_timeout(50);larger=view(page)
        result['checks']['thumb_shrinks_as_history_grows']=larger['extent']>normal['extent'] and larger['height']<normal['height']
        drag_history(page,'first',False);top=view(page)
        drag_history(page,'first',True);bottom=view(page)
        result['checks']['real_pointer_drag_reaches_both_content_boundaries']=top['viewport']==top['value']==0 and bottom['viewport']==bottom['value']==bottom['extent'] and bottom['focus']==0 and bottom['input']==[]
        result['themes']={}
        for theme in ('noir','dark','light','system'):
            page.evaluate('theme=>document.documentElement.dataset.chrome=theme',theme)
            result['themes'][theme]=view(page)
        result['checks']['all_themes_preserve_narrow_thumb']=all(t['width']==5 and t['border']=='0px' for t in result['themes'].values())
        page.evaluate("document.documentElement.dataset.chrome='noir'");wheel(page,-160)
        page.screenshot(path=str(args.evidence_dir/'history.png'))
        rect=page.locator('#first .scrollbar.vertical').bounding_box()
        page.screenshot(path=str(args.evidence_dir/'scroller.png'),clip={'x':rect['x']-8,'y':rect['y'],'width':rect['width']+16,'height':rect['height']})
        write(page,'first','\x1b[?1049h\x1b[?1000h\x1b[?1006hApp at its boundary')
        page.wait_for_timeout(50);before=view(page);wheel(page,-160);up=view(page);wheel(page,160);down=view(page)
        result['application_wheel']={'before':before,'up':up,'down':down}
        result['checks']['no_false_position_when_app_ignores_wheel']=all(v['hidden'] and v['text']==before['text'] and v['value']==0 for v in (before,up,down)) and len(down['input'])>0
        result['checks']['no_custom_application_position_controls']=page.locator('.terminal-scroll-controls,input[type=range]').count()==0
        page.evaluate('probes.first.mine=false;probes.first.input=[]');wheel(page,-160)
        result['checks']['viewer_cannot_send_application_scroll']=view(page)['input']==[]
        page.evaluate('probes.first.mine=true;probes.first.input=[]')
        write(page,'first','\x1b[?1000l\x1b[?1006l');wheel(page,-160)
        result['checks']['non_mouse_application_uses_fenced_page_navigation']=view(page)['input']==[{'direction':'up','count':1}]
        write(page,'first','\x1b[?1049l');page.wait_for_timeout(50);restored=view(page)
        result['checks']['real_history_position_restored_after_app_exit']=not restored['hidden'] and restored['value']==restored['viewport'] and restored['maximum']==restored['extent']
        page.evaluate('probes.first.scroll.dispose();probes.first.api.dispose()');browser.close()
    result['ok']=all(result['checks'].values()) and not result['errors']
    (args.evidence_dir/'feedback.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'checks':result['checks'],'ok':result['ok']}));assert result['ok'],result['checks']


if __name__=='__main__':main()
