import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
const source = readFileSync(process.env.TERMINAL_COPY_SOURCE || new URL("../../crates/termdeck-web-server/assets/terminal-copy.js", import.meta.url), "utf8");
function fixture() {
  class Element {
    constructor(tag="div") { this.tag=tag; this.listeners=new Map(); this.children=[]; this.hidden=false; this.textContent=""; }
    addEventListener(type,fn) { const set=this.listeners.get(type)||new Set();set.add(fn);this.listeners.set(type,set); }
    removeEventListener(type,fn) { this.listeners.get(type)?.delete(fn); }
    emit(type,extra={}) { for (const fn of [...(this.listeners.get(type)||[])]) fn({button:0,clientX:1,clientY:1,detail:0,target:this,...extra}); }
    append(...children) { this.children.push(...children); }
    remove() { this.removed=true; }
    closest() { return ["button","input","select","a"].includes(this.tag) ? this : null; }
  }
  const window=new Element();const timers=new Map();let timerId=0;
  window.isSecureContext=true;window.setTimeout=(fn,delay)=>{timers.set(++timerId,{fn,delay});return timerId;};window.clearTimeout=id=>timers.delete(id);
  const host=new Element();const writes=[];let selected="",denied=false;
  const navigator={clipboard:{writeText(value){if(denied)return Promise.reject(new Error("denied"));writes.push(value);return Promise.resolve();}}};
  vm.runInNewContext(source,{window,document:{createElement:tag=>new Element(tag)},navigator});
  const dispose=window.T1000TerminalCopy.install(host,{selection:()=>selected});
  return {window,host,writes,navigator,dispose,set selected(value){selected=value;},set denied(value){denied=value;},notice:host.children[0],
    down(extra={}){host.emit("pointerdown",extra);},up(extra={}){window.emit("mouseup",{clientX:30,...extra});},pointerUp(extra={}){window.emit("pointerup",{clientX:30,...extra});},
    async flush(){for(const [id,timer] of [...timers])if(timer.delay===0){timers.delete(id);timer.fn();}await Promise.resolve();await Promise.resolve();},
  };
}
test("mouse selection finalizes before window mouseup, even outside the host",async()=>{
 const f=fixture();f.down();f.pointerUp();f.selected="final xterm selection";f.up();await f.flush();assert.deepEqual(f.writes,["final xterm selection"]);assert.equal(f.notice.children[0].textContent,"Selection copied");
});
test("double and triple mouse clicks copy without dragging",async()=>{
 for(const detail of [2,3]){const f=fixture();f.down();f.pointerUp({clientX:1});f.selected="clicked word or line";f.up({clientX:1,detail});await f.flush();assert.deepEqual(f.writes,["clicked word or line"]);}
});
test("one pointer gesture followed by mouseup copies once",async()=>{
 const f=fixture();f.selected="once";f.down();f.pointerUp();f.up();await f.flush();assert.deepEqual(f.writes,["once"]);
});
test("pointer-only release keeps automatic copy",async()=>{
 const f=fixture();f.selected="pointer";f.down();f.pointerUp();await f.flush();assert.deepEqual(f.writes,["pointer"]);
});
test("selection completed by a later mouseup listener is copied after dispatch",async()=>{
 const f=fixture();f.down();f.up();f.selected="late";await f.flush();assert.deepEqual(f.writes,["late"]);
});
test("ordinary click, empty selection and unrelated release never copy",async()=>{
 const f=fixture();f.selected="old selection";f.up();f.down();f.up({clientX:1});f.selected="";f.down();f.up();await f.flush();assert.deepEqual(f.writes,[]);
});
test("cancelled, right button and control gestures never copy",async()=>{
 const f=fixture();f.selected="old selection";f.down();f.window.emit("pointercancel");f.up();f.down({button:2});f.up({button:2});f.down({target:{closest:()=>({})}});f.up();await f.flush();assert.deepEqual(f.writes,[]);
});
test("disposal cancels pending copy and removes listeners",async()=>{
 const f=fixture();f.selected="cancel";f.down();f.pointerUp();f.dispose();await f.flush();f.down();f.up();assert.deepEqual(f.writes,[]);for(const set of f.window.listeners.values())assert.equal(set.size,0);for(const set of f.host.listeners.values())assert.equal(set.size,0);
});
test("blur cancels an unfinished gesture",async()=>{
 const f=fixture();f.selected="old";f.down();f.window.emit("blur");f.up();await f.flush();assert.deepEqual(f.writes,[]);
});
test("denial shows retry and retry copies only the current selected text",async()=>{
 const f=fixture();f.selected="first";f.denied=true;f.down();f.up();await f.flush();assert.match(f.notice.children[0].textContent,/permission denied/);assert.equal(f.notice.children[1].hidden,false);f.denied=false;f.selected="current";f.notice.children[1].emit("click");await f.flush();assert.deepEqual(f.writes,["current"]);
});
test("synchronous clipboard errors show denial",async()=>{
 const f=fixture();f.navigator.clipboard.writeText=()=>{throw new Error("denied");};f.selected="selected";f.down();f.up();await f.flush();assert.match(f.notice.children[0].textContent,/permission denied/);
});
test("insecure contexts report a browser copy fallback",async()=>{
 const f=fixture();f.window.isSecureContext=false;f.selected="selected";f.down();f.up();await f.flush();assert.deepEqual(f.writes,[]);assert.match(f.notice.children[0].textContent,/unavailable/);
});
