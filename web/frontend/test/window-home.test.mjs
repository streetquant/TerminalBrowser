import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../crates/termdeck-web-server/assets/window-home.js', import.meta.url), 'utf8');
function fixture() {
  let document;
  let updates = { creates:0, inserts:0, removals:0, texts:0 };
  class Element {
    constructor(tag) { this.tagName = tag; this._text = ''; this.className=''; this.dataset={};this.attrs={};this.children=[];this.parent=null;this.listeners=new Map();this.value='';updates.creates++; }
    get textContent() { return this._text; }
    set textContent(value) { this._text=value; updates.texts++; }
    get firstChild() { return this.children[0] || null; }
    get lastChild() { return this.children.at(-1) || null; }
    get nextSibling() { if(!this.parent)return null;const i=this.parent.children.indexOf(this);return this.parent.children[i+1]||null; }
    getAttribute(key){return this.attrs[key]??null;}
    setAttribute(key,value){this.attrs[key]=value;}
    addEventListener(type,fn){ this.listeners.set(type,fn); }
    contains(node){return this===node||this.children.some(c=>c.contains(node));}
    focus(){document.activeElement=this;}
    append(...nodes) {for(const n of nodes){if(n.parent)n.parent.removeChild(n);this.children.push(n);n.parent=this;}}
    insertBefore(node,before){ if(before!==null)assert.equal(before.parent,this); if(node===before)return; if(node.parent)node.parent.removeChild(node);const i=before===null?this.children.length:this.children.indexOf(before);assert.ok(i>=0);this.children.splice(i,0,node);node.parent=this;updates.inserts++; }
    removeChild(node){const i=this.children.indexOf(node);assert.ok(i>=0);if(node.contains(document.activeElement))document.activeElement=null;this.children.splice(i,1);node.parent=null;updates.removals++;return node;}
    replaceChildren(...nodes){for(const child of [...this.children])this.removeChild(child);this.append(...nodes);}
    matches(selector){return selector.startsWith('.')?this.className.split(' ').includes(selector.slice(1)):this.tagName===selector;}
    querySelector(selector){for(const child of this.children){if(child.matches(selector))return child;const deep=child.querySelector(selector);if(deep)return deep;}return null;}
    closest(selector){for(let n=this;n;n=n.parent)if(n.matches(selector))return n;return null;}
  }
  document={ createElement:tag=>new Element(tag), activeElement:null };
  const stats={home:0,killed:[]};
  const window={T1000UI:{renderHome(){throw Error('not overridden')}},T1000Management:{home(header){stats.home++;},killWindow(id){stats.killed.push(id);}}};
  vm.runInNewContext(source,{document,window});
  const root=new Element('main');
  const render=(data,stale=false)=>window.T1000UI.renderHome(root,data,stale,p=>p);
  return {root,render,document,window,stats,updates,Element};
}
const wnd=(id,name,pane=`Shell ${id}`)=>({id,name,path:`/${id}`,panes:[{title:pane,cwd:`/work/${id}`}]});
const snap=(revision,windows)=>({revision,windows,totalPanes:windows.reduce((n,w)=>n+w.panes.length,0)});
const list=f=>f.root.querySelector('.window-list');
const rows=f=>list(f).children.filter(x=>x.className==='window-list-entry');

test('loads after original UI, preserving public renderHome function',()=>{
 const f=fixture();f.render(snap(1,[wnd('a','Alpha')]));
 assert.equal(f.stats.home,1);assert.equal(rows(f).length,1);
 assert.equal(rows(f)[0].dataset.id,'a');
 assert.equal(rows(f)[0].querySelector('strong').textContent,'Alpha');
});

test('100 equal revisions do not mutate row DOM, and changed revisions retain focus and handlers',()=>{
 const f=fixture();f.render(snap(1,[wnd('a','Alpha'),wnd('b','Beta')]));
 const link=rows(f)[0].querySelector('.window-list-row'); const kill=rows(f)[0].querySelector('.window-list-kill');
 kill.focus();
 const start={...f.updates};
 for(let i=0;i<100;i++) f.render(snap(1,[wnd('a','Alpha'),wnd('b','Beta')]));
 assert.deepEqual(f.updates,start);
 for(let i=2;i<102;i++) f.render(snap(i,[wnd('a','Alpha'),wnd('b','Beta')]));
 assert.equal(rows(f)[0].querySelector('.window-list-kill'),kill);
 assert.equal(rows(f)[0].querySelector('.window-list-row'),link);
 assert.equal(f.document.activeElement,kill);
 assert.equal(f.updates.inserts,start.inserts);
 kill.listeners.get('click')();assert.deepEqual(f.stats.killed,['a']);
 assert.equal(kill.listeners.size,1);
});

test('reordering retains focus, node identity, and correct order',()=>{
 const f=fixture();f.render(snap(1,[wnd('a','Alpha'),wnd('b','Beta'),wnd('c','Gamma')]));
 const focus=rows(f)[1].querySelector('.window-list-kill');focus.focus();
 const original=new Map(rows(f).map(r=>[r.dataset.id,r]));
 f.render(snap(2,[wnd('c','Gamma'),wnd('a','Alpha'),wnd('b','Beta')]));
 assert.deepEqual(rows(f).map(r=>r.dataset.id),['c','a','b']);
 assert.equal(f.document.activeElement,focus);
 for(const r of rows(f))assert.equal(r,original.get(r.dataset.id));
});

test('search detaches only filtered rows and reuses them when cleared',()=>{
 const f=fixture();f.render(snap(1,[wnd('a','Alpha'),wnd('b','Beta')]));
 const original=new Map(rows(f).map(r=>[r.dataset.id,r]));
 const search=f.root.querySelector('.window-filter');
 search.value='beta';search.listeners.get('input')();
 assert.deepEqual(rows(f).map(r=>r.dataset.id),['b']);
 assert.equal(rows(f)[0],original.get('b'));
 search.value='';search.listeners.get('input')();
 assert.deepEqual(rows(f).map(r=>r.dataset.id),['a','b']);
 assert.equal(rows(f)[0],original.get('a'));
});

test('renames update visible text and accessible destructive-action label without rebuilding nodes',()=>{
 const f=fixture();f.render(snap(1,[wnd('a','Old')]));
 const old=rows(f)[0];const kill=old.querySelector('.window-list-kill');
 f.render(snap(2,[wnd('a','New')]));
 assert.equal(rows(f)[0],old);
 assert.equal(old.querySelector('strong').textContent,'New');
 assert.equal(kill.getAttribute('aria-label'),'Kill New window permanently');
});

test('stale indicator updates even with unchanged revision and no row churn',()=>{
 const f=fixture();f.render(snap(1,[wnd('a','Alpha')]));
 const start={...f.updates};f.render(snap(1,[wnd('a','Alpha')]),true);
 assert.match(f.root.querySelector('.window-list-status').textContent,/Connection interrupted/);
 assert.equal(f.updates.inserts,start.inserts);
 assert.equal(f.updates.creates,start.creates);
});

test('removal and empty filtered state show accurate text',()=>{
 const f=fixture();f.render(snap(1,[wnd('a','Alpha')]));
 f.render(snap(2,[]));
 assert.equal(rows(f).length,0);
 assert.match(list(f).firstChild.textContent,/No windows yet/);
 f.render(snap(3,[wnd('b','Beta')]));
 f.root.querySelector('.window-filter').value='nothere';
 f.root.querySelector('.window-filter').listeners.get('input')();
 assert.match(list(f).firstChild.textContent,/No matching windows/);
});

test('untrusted window title stays text and duplicate IDs do not produce extra controls',()=>{
 const f=fixture();
 f.render(snap(1,[wnd('a','<img src=x onerror=evil()>'),wnd('a','Duplicate')]));
 assert.equal(rows(f).length,1);
 assert.equal(rows(f)[0].querySelector('strong').textContent,'<img src=x onerror=evil()>');
});
