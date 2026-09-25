#!/usr/bin/env python3
"""Bounded disposable HTTP/load gate for the TermDeck browser host."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, timeout: float = 20.0) -> None:
    deadline=time.monotonic()+timeout
    last: Exception | None=None
    while time.monotonic()<deadline:
        try:
            with urllib.request.urlopen(url,timeout=1) as response:
                if response.status==200:return
        except Exception as error: last=error
        time.sleep(.05)
    raise RuntimeError(f"server readiness timeout: {last}")


def percentile(values: list[float], quantile: float) -> float:
    ordered=sorted(values)
    index=min(len(ordered)-1,max(0,int(round((len(ordered)-1)*quantile))))
    return ordered[index]


def rss_bytes(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"): return int(line.split()[1])*1024
    except OSError: pass
    return 0


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("--binary",type=Path,required=True)
    parser.add_argument("--evidence",type=Path,required=True)
    parser.add_argument("--requests",type=int,default=5000)
    parser.add_argument("--concurrency",type=int,default=32)
    args=parser.parse_args()
    binary=args.binary.resolve(); evidence=args.evidence.resolve(); evidence.parent.mkdir(parents=True,exist_ok=True)
    if args.requests<100 or not 1<=args.concurrency<=128: raise SystemExit("invalid load bounds")
    repository=Path.cwd().resolve(); temp_parent=repository/'.ai-bridge/tmp'; temp_parent.mkdir(parents=True,exist_ok=True)
    root=Path(tempfile.mkdtemp(prefix='web-load-',dir=temp_parent)); socket_name=f"termdeck-load-{os.getpid()}"; session='termdeck_load_random'; port=free_port()
    database=root/'sessions.sqlite3'; wrapper=root/'tmux'; server: subprocess.Popen[bytes] | None=None
    stdout=(evidence.parent/'load-server.stdout').open('wb'); stderr=(evidence.parent/'load-server.stderr').open('wb')
    try:
        wrapper.write_text(f'#!/bin/sh\nexec /usr/bin/tmux -L {socket_name} "$@"\n'); wrapper.chmod(0o700)
        subprocess.run(['/usr/bin/tmux','-L',socket_name,'-f','/dev/null','new-session','-d','-s',session,'-x','100','-y','30','/usr/bin/sleep','600'],check=True,timeout=10)
        con=sqlite3.connect(database)
        try:
            con.executescript('''CREATE TABLE sessions (id TEXT PRIMARY KEY,name TEXT NOT NULL,cwd TEXT NOT NULL,tmux_name TEXT NOT NULL,status TEXT NOT NULL,window_id TEXT NOT NULL,window_title TEXT NOT NULL,tab_index INTEGER NOT NULL,visible INTEGER NOT NULL,pinned INTEGER NOT NULL,last_opened_at TEXT NOT NULL,pane_target TEXT);CREATE TABLE remembered_windows(window_id TEXT PRIMARY KEY,window_title TEXT NOT NULL);INSERT INTO remembered_windows VALUES('win-load','Random');''')
            con.execute('insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?)',('td-load','Load fixture',str(repository),session,'running','win-load','Random',0,1,0,'2026-08-28T00:00:00Z',None)); con.commit()
        finally: con.close()
        command=[str(binary),'--db',str(database),'--listen',f'127.0.0.1:{port}','--refresh-ms','250','--max-stale-ms','10000','--max-connections','128','--tmux',str(wrapper),'--tmux-timeout-ms','2000']
        started=time.monotonic(); server=subprocess.Popen(command,cwd=repository,stdout=stdout,stderr=stderr,start_new_session=True); wait_http(f'http://127.0.0.1:{port}/_termdeck/healthz'); ready_ms=(time.monotonic()-started)*1000
        paths=['/','/random','/_termdeck/api/v1/snapshot','/_termdeck/api/v1/windows/random','/_termdeck/assets/v7/app.css','/_termdeck/assets/v7/app.js','/_termdeck/assets/v7/terminal.css','/_termdeck/assets/v7/terminal.js']
        stop=threading.Event(); rss_samples: list[int]=[]
        def sample() -> None:
            while not stop.wait(.02):
                if server is not None:rss_samples.append(rss_bytes(server.pid))
        sampler=threading.Thread(target=sample,daemon=True); sampler.start()
        errors: list[str]=[]; latencies: list[float]=[]; bytes_read=0; lock=threading.Lock()
        def request(index: int) -> None:
            nonlocal bytes_read
            path=paths[index%len(paths)]; begin=time.perf_counter(); connection=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
            try:
                connection.request('GET',path,headers={'Accept':'*/*','Connection':'close'}); response=connection.getresponse(); body=response.read()
                elapsed=(time.perf_counter()-begin)*1000
                with lock:
                    latencies.append(elapsed); bytes_read+=len(body)
                    if response.status!=200:errors.append(f'{path}:HTTP {response.status}')
            except Exception as error:
                with lock:errors.append(f'{path}:{type(error).__name__}:{error}')
            finally: connection.close()
        for i in range(100):request(i)
        latencies.clear(); errors.clear(); bytes_read=0
        load_start=time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:list(pool.map(request,range(args.requests)))
        elapsed=time.perf_counter()-load_start; stop.set(); sampler.join(timeout=2)
        if len(latencies)!=args.requests or errors: raise RuntimeError(f'load errors={errors[:20]} completed={len(latencies)}/{args.requests}')
        summary: dict[str,Any]={'schema':'termdeck.web-load.v1','ok':True,'requests':args.requests,'concurrency':args.concurrency,'elapsed_s':round(elapsed,3),'throughput_rps':round(args.requests/elapsed,3),'latency_ms':{'p50':round(percentile(latencies,.5),3),'p95':round(percentile(latencies,.95),3),'p99':round(percentile(latencies,.99),3),'max':round(max(latencies),3)},'bytes_read':bytes_read,'server_ready_ms':round(ready_ms,3),'rss_bytes':{'min':min(rss_samples or [0]),'max':max(rss_samples or [0]),'delta':max(rss_samples or [0])-min(rss_samples or [0])},'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}
        if summary['throughput_rps']<50 or summary['latency_ms']['p99']>1000 or summary['rss_bytes']['max']>256*1024*1024: raise RuntimeError(f'load budget failed: {summary}')
        evidence.write_text(json.dumps(summary,sort_keys=True,indent=2)+'\n'); print(json.dumps(summary,sort_keys=True))
    finally:
        stop=locals().get('stop');
        if stop is not None:stop.set()
        if server is not None and server.poll() is None:
            os.killpg(server.pid,signal.SIGTERM)
            try:server.wait(timeout=10)
            except subprocess.TimeoutExpired:os.killpg(server.pid,signal.SIGKILL); server.wait(timeout=10)
        stdout.close(); stderr.close(); subprocess.run(['/usr/bin/tmux','-L',socket_name,'kill-server'],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10); shutil.rmtree(root,ignore_errors=True)

if __name__=='__main__':main()
