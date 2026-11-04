import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import vm from "node:vm";
const source = readFileSync(process.env.TERMINAL_SESSION_SOURCE || new URL("../../crates/termdeck-web-server/assets/terminal-session.js", import.meta.url), "utf8");
function fixture(options = {}) {
  let now=0, next=0, geometry={cols:80,rows:24}, layout, input, scrollOptions, wheelInput=false;
  const timers=new Map(), sockets=[], writes=[], callbacks=[];
  const timeout=(fn,ms=0)=>{timers.set(++next,{fn,at:now+ms});return next};
  function advance(ms) { const end=now+ms;for(;;){const due=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;timers.delete(due[0]);now=due[1].at;due[1].fn()}now=end; }
  class Socket {
    static OPEN=1;static CONNECTING=0;
    constructor(){this.readyState=0;this.bufferedAmount=0;this.events={};this.sent=[];sockets.push(this)}
    addEventListener(n,f){(this.events[n] ||= []).push(f)}
    emit(n,v){for(const f of this.events[n] || [])f(v)}
    send(v){this.sent.push(typeof v==='string'?JSON.parse(v):v)}
    close(code,reason){this.closed={code,reason};this.readyState=3}
  }
  class Element {constructor(){this.dataset={};this.hidden=true;this.textContent=''} addEventListener(){} setAttribute(){} contains(){return false} append(){} before(){} remove(){} replaceChildren(){}}
  const host=new Element(), status=new Element(), overlay=new Element();
  const api={terminal:{rows:24,blur(){},resize(){},refresh(){}},
    fit:()=>({...geometry}),dimensions:()=>({...geometry}),fitGrid(){},reset(){},
    write(bytes,done){writes.push(bytes);callbacks.push(done)},onData(fn){input=fn},focus(){},dispose(){},activateWebgl(){}};
  const window={fetch:options.fetch || (async()=>({ok:false,status:404})),TermDeckTerminal:{createTerminal(_host,options){layout=options.onLayoutChange;return api}},T1000UI:{preferences:()=>({})},
    T1000TerminalCopy:{install:()=>()=>{}},T1000TerminalScroll:{install:(_host,_api,options)=>{scrollOptions=options;return {isWheelInput:()=>wheelInput,dispose(){}}}},
    setTimeout:timeout,clearTimeout:id=>timers.delete(id),requestAnimationFrame:fn=>timeout(fn,16),cancelAnimationFrame:id=>timers.delete(id),
    setInterval:()=>++next,clearInterval(){},addEventListener(){},removeEventListener(){},navigator:{onLine:true},location:{protocol:'http:',host:'fixture'}};
  const document={createElement:()=>new Element(),visibilityState:'visible',hidden:false,body:new Element(),addEventListener(){},removeEventListener(){},hasFocus:()=>false};
  vm.runInNewContext(source,{window,document,WebSocket:Socket,ResizeObserver:class{observe(){} disconnect(){}},Uint8Array,ArrayBuffer,TextEncoder,DataView,Map,Date,URLSearchParams});
  const mount=window.T1000TerminalSession.mount('fixture','td-test',host,status,overlay,{appPath:s=>s,isRecord:v=>v && typeof v==='object',nonNegativeInteger:v=>v,errorMessage:e=>e.message});
  const ws=sockets[0];ws.readyState=1;ws.emit('open',{});
  const control={schema:'termdeck.terminal-control.v1',mine:true,epoch:1,next_sequence:1};
  const event=value=>ws.emit('message',{data:typeof value==='string'?value:JSON.stringify(value)});
  const ready=()=>event({type:'resynced',geometry:{cols:80,rows:24},control});
  event({type:'ready',geometry:{cols:80,rows:24},control});
  return {ws,writes,callbacks,overlay,mount,advance,ready,event,
    type(value){input(new TextEncoder().encode(value))},
    wheel(reports){wheelInput=true;try{scrollOptions.onMouseWheelBatch(()=>{for(const value of reports)input(new TextEncoder().encode(value))})}finally{wheelInput=false}},
    bytes:n=>ws.emit('message',{data:new ArrayBuffer(n)}),resize(cols){geometry={cols,rows:24};layout()}};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
test('a stalled renderer cannot hide an unbounded output backlog in promises',async()=>{
 const f=fixture();for(let i=0;i<40;i++){f.bytes(512*1024);await tick()}
 assert.equal(f.ws.closed?.code,4009);assert.match(f.overlay.textContent,/bounded browser queue/);f.mount.dispose();
});
test('continuous resize waits for a settled grid and keeps at most one request in flight',async()=>{
 const f=fixture();for(let i=0;i<40;i++){f.resize(90+i);f.advance(16)}
 f.advance(120);let resizes=f.ws.sent.filter(x=>x.type==='resize');
 assert.equal(resizes.length,1);assert.equal(resizes[0].cols,129);
 f.resize(150);f.advance(200);assert.equal(f.ws.sent.filter(x=>x.type==='resize').length,1);
 f.ready();f.advance(120);resizes=f.ws.sent.filter(x=>x.type==='resize');assert.equal(resizes.length,2);assert.equal(resizes[1].cols,150);
 f.mount.dispose();
});
test('output still reaches the renderer once and in order below the bound',async()=>{
 const f=fixture();f.bytes(10);f.bytes(20);await tick();assert.equal(f.writes.length,1);assert.equal(f.writes[0].length,10);
 f.callbacks.shift()();await tick();f.advance(20);await tick();assert.equal(f.writes.length,2);assert.equal(f.writes[1].length,20);f.callbacks.shift()();await tick();f.mount.dispose();
});

const wheelUp='\x1b[<64;2;3M',wheelDown='\x1b[<65;2;3M';
const frames=f=>f.ws.sent.filter(x=>x instanceof Uint8Array);
const payload=frame=>new TextDecoder().decode(frame.subarray(9));
const sequence=frame=>Number(new DataView(frame.buffer,frame.byteOffset,frame.byteLength).getBigUint64(1,false));
test('one gesture sends eight identical native reports immediately in one passive frame',async()=>{
 const f=fixture();await tick();f.wheel(Array(8).fill(wheelUp));
 assert.equal(frames(f).length,1);const frame=frames(f)[0];
 assert.equal(new Uint8Array(frame)[0],2);assert.equal(sequence(frame),1);assert.equal(payload(frame),wheelUp.repeat(8));
 // Typing must not wait for this wheel acknowledgement or lose ordering.
 f.type('A');f.advance(3);assert.equal(frames(f).length,2);
 assert.equal(sequence(frames(f)[1]),2);assert.equal(new Uint8Array(frames(f)[1])[0],1);assert.equal(payload(frames(f)[1]),'A');
 f.mount.dispose();
});
test('wheel batches preserve direction, coordinates, modifiers and the eight report bound',async()=>{
 const f=fixture();await tick();const other='\x1b[<64;3;3M',modified='\x1b[<68;2;3M';
 f.wheel([...Array(9).fill(wheelUp),wheelDown,other,modified]);
 assert.deepEqual(frames(f).map(payload),[wheelUp.repeat(8),wheelUp,wheelDown,other,modified]);
 assert.deepEqual(frames(f).map(sequence),[1,2,3,4,5]);f.mount.dispose();
});
test('pending keyboard bytes precede a wheel batch and passive backpressure stays bounded',async()=>{
 const f=fixture();await tick();f.type('first');f.type('second');f.wheel(Array(8).fill(wheelUp));
 assert.deepEqual(frames(f).map(payload),['first','second',wheelUp.repeat(8)]);
 for(let i=0;i<100;i++)f.wheel(Array(8).fill(wheelDown));
 assert.equal(frames(f).length,64);f.mount.dispose();
});
test('viewer, deactivated and unavailable streams cannot send a passive wheel batch',async()=>{
 const f=fixture();await tick();f.event({type:'control',control:{schema:'termdeck.terminal-control.v1',mine:false,epoch:2,next_sequence:1}});await tick();
 f.wheel(Array(8).fill(wheelUp));assert.equal(frames(f).length,0);
 f.event({type:'control',control:{schema:'termdeck.terminal-control.v1',mine:true,epoch:3,next_sequence:1}});await tick();
 f.mount.deactivate();f.wheel([wheelUp]);assert.equal(frames(f).length,0);
 f.mount.setPaneState({status:'stopped',hasTerminal:false});f.wheel([wheelUp]);assert.equal(frames(f).length,0);f.mount.dispose();
});


test('saved output replays after pane loss and never authorizes terminal input',async()=>{
 const pages=[{generation:'g',generations:['g'],next_offset:64,complete:true,truncated_tail:false,
 events:[{kind:0,cols:80,rows:24,data_hex:'7361766564206f7574707574'}]}];
 let requests=[];
 const f=fixture({fetch:async(path,options)=>{requests.push({path,options});return {ok:true,json:async()=>pages.shift()}}});
 f.mount.setPaneState({status:'missing',hasTerminal:false});
 for(let i=0;i<5;i++)await tick();
 assert.equal(f.writes.length,1);assert.equal(new TextDecoder().decode(f.writes[0]),'saved output');
 f.callbacks.shift()();await tick();
 f.type('must not send');f.advance(100);
 assert.equal(frames(f).length,0);assert.equal(f.ws.closed.code,1000);
 assert.match(requests[0].path,/panes\/td-test\/recording/);assert.equal(requests[0].options.credentials,'same-origin');
 assert.equal(f.overlay.hidden,true);f.mount.dispose();
});

test('missing recording preserves the existing display and keeps input disabled',async()=>{
 const f=fixture({fetch:async()=>({ok:false,status:404})});
 f.bytes(5);await tick();f.callbacks.shift()();await tick();
 f.mount.setPaneState({status:'missing',hasTerminal:false});
 await tick();await tick();
 assert.equal(f.writes.length,1);assert.match(f.overlay.textContent,/No saved output/);
 f.type('forbidden');f.advance(100);assert.equal(frames(f).length,0);f.mount.dispose();
});
