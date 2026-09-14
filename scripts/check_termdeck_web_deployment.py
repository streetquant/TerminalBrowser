#!/usr/bin/python3
"""Static contract checks for persistent TermDeck LAN and Cloudflare deployment."""
from __future__ import annotations
from pathlib import Path
import json,re,sys
ROOT=Path(__file__).resolve().parents[1]
required={
 'nginx':ROOT/'deploy/nginx/termdeck-web.nginx.conf',
 'prepare':ROOT/'scripts/termdeck-web-prepare-runtime.sh',
 'backend':ROOT/'deploy/systemd/termdeck-web-backend.service',
 'lan':ROOT/'deploy/systemd/termdeck-web-lan.service',
 'preview':ROOT/'scripts/termdeck-cloudflare-preview.py',
 'preview_unit':ROOT/'deploy/systemd/termdeck-cloudflare-preview.service',
 'tunnel_example':ROOT/'deploy/cloudflare/termdeck.yml.example',
}
missing=[str(p) for p in required.values() if not p.is_file()]
if missing: raise SystemExit(f'missing deployment files: {missing}')
texts={k:p.read_text(errors='strict') for k,p in required.items()}

# These files are installed or executed as part of the active deployment. A
# stale storage-era path here would make a clean Warp install resolve to a
# different tree even when all runtime defaults look correct. Test fixtures
# and historical archive compatibility are intentionally outside this gate.
active_path_files = tuple(dict.fromkeys((*required.values(),
    ROOT/'deploy/systemd/termdeck-checkpoint.service',
    ROOT/'deploy/systemd/termdeck-cloudflared.service',
    ROOT/'deploy/cloudflare/maxchat-termdeck-route.json',
    ROOT/'scripts/termdeck-web-prepare-runtime.sh',
    ROOT/'termdeck/desktop_integration.py',
    ROOT/'termdeck/release_manager.py',
    ROOT/'termdeck/web_activation.py',
)))
legacy_path_markers = ('/8tb', '/var/lib/cloudmcp-exec/projects/softwares',
                       '/projects/softwares', '/softwares/termdeck')
legacy_path_hits = [
    {'path': str(path.relative_to(ROOT)), 'marker': marker}
    for path in active_path_files
    for marker in legacy_path_markers
    if marker in path.read_text(errors='strict')
]
if legacy_path_hits:
    raise SystemExit(f'active deployment contains legacy path(s): {legacy_path_hits}')

warp_path_checks = {
    'source_is_warp': '/warp/projects/software/termdeck' in
        (ROOT/'termdeck/release_manager.py').read_text(errors='strict'),
    'runtime_is_warp': '/warp/runtime/termdeck' in ''.join(texts.values()),
    'state_is_warp': '/warp/state/termdeck' in ''.join(texts.values()),
    'service_home_is_state': all(
        'Environment=HOME=/warp/state/termdeck' in texts[name]
        for name in ('backend', 'lan', 'preview_unit')
    ),
}
if not all(warp_path_checks.values()):
    raise SystemExit(f'Warp deployment path contract failed: {warp_path_checks}')

checks={
 'dynamic_lan_bind':'listen 0.0.0.0:7443 ssl' in texts['nginx'],
 'strict_host_allow':'allowed-hosts.map' in texts['nginx'] and 'return 421' in texts['nginx'],
 'websocket_upgrade':all(x in texts['nginx'] for x in ('proxy_set_header Upgrade $http_upgrade','proxy_set_header Connection "upgrade"','proxy_read_timeout 86400s')),
 'origin_tls_verified':'caPool:' in texts['tunnel_example'] and 'originServerName:' in texts['tunnel_example'] and 'noTLSVerify' not in texts['tunnel_example'],
 'dynamic_rfc1918':all(x in texts['prepare'] for x in ('ip -4 route get','192.168.0.0/16','subjectAltName')),
 'persistent_backend':'Restart=always' in texts['backend'] and 'WantedBy=default.target' in texts['backend'],
 'persistent_proxy':'Restart=always' in texts['lan'] and 'WantedBy=default.target' in texts['lan'],
 'warp_mount_guard':'ConditionPathIsMountPoint=/warp' in texts['backend'] and 'ConditionPathIsMountPoint=/warp' in texts['lan'],
 'warp_mount_order':'After=warp.mount' in texts['backend'] and 'After=warp.mount' in texts['lan'],
 'noncascading_web_restart':'Requires=termdeck-web-backend.service' not in texts['lan'],
 'noncascading_cloudflare_restart':'Requires=termdeck-web-lan.service' not in texts['preview'] and 'Requires=termdeck-web-lan.service' not in (ROOT/'deploy/systemd/termdeck-cloudflared.service').read_text(errors='strict'),
 'quick_tunnel_isolated':'cloudflare-preview-empty.yml' in texts['preview'] and 'trycloudflare' in texts['preview'],
 'active_paths_are_warp': not legacy_path_hits and all(warp_path_checks.values()),
}
failed=[name for name,ok in checks.items() if not ok]
if failed: raise SystemExit(f'deployment contract failed: {failed}')
secret_patterns=[r'(?i)\b(?:api[_-]?token|tunnel[_-]?secret|authorization)\s*[:=]\s*[A-Za-z0-9_-]{20,}',r'eyJhIjoi[0-9a-f]{20,}']
secret_hits=[]
for path in required.values():
 text=path.read_text(errors='replace')
 for pattern in secret_patterns:
  if re.search(pattern,text): secret_hits.append({'path':str(path),'pattern':pattern})
if secret_hits: raise SystemExit(f'credential-like material found: {secret_hits}')
print(json.dumps({'ok':True,'checks':checks,
                  'files':{k:str(v.relative_to(ROOT)) for k,v in required.items()},
                  'active_path_files':[str(item.relative_to(ROOT)) for item in active_path_files]},
                 sort_keys=True,indent=2))
