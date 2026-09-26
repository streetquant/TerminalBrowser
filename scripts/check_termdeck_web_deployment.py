#!/usr/bin/python3
"""Static contract checks for persistent TermDeck LAN and Cloudflare deployment."""
from __future__ import annotations
from pathlib import Path
import json,re,sys
ROOT=Path(__file__).resolve().parents[1]
CANONICAL_SOURCE = '/warp/projects/softwares/termdeck'
CANONICAL_RUNTIME = f'{CANONICAL_SOURCE}/.termdeck'
CANONICAL_STATE = f'{CANONICAL_RUNTIME}/state'
CANONICAL_BIN = f'{CANONICAL_RUNTIME}/bin'
CANONICAL_MANAGED = f'{CANONICAL_RUNTIME}/t1000-managed'
required={
 'nginx':ROOT/'deploy/nginx/termdeck-web.nginx.conf',
 'prepare':ROOT/'scripts/termdeck-web-prepare-runtime.sh',
 'backend':ROOT/'deploy/systemd/termdeck-web-backend.service',
 'lan':ROOT/'deploy/systemd/termdeck-web-lan.service',
 'recovery':ROOT/'deploy/systemd/termdeck-web-recovery.service',
 'cloudflared_unit':ROOT/'deploy/systemd/termdeck-cloudflared.service',
 'preview':ROOT/'scripts/termdeck-cloudflare-preview.py',
 'preview_unit':ROOT/'deploy/systemd/termdeck-cloudflare-preview.service',
 'checkpoint':ROOT/'deploy/systemd/termdeck-checkpoint.service',
 'wrappers':ROOT/'termdeck/service_wrappers.py',
 'tunnel_example':ROOT/'deploy/cloudflare/termdeck.yml.example',
}
missing=[str(p) for p in required.values() if not p.is_file()]
if missing: raise SystemExit(f'missing deployment files: {missing}')
texts={k:p.read_text(errors='strict') for k,p in required.items()}
activation_text=(ROOT/'termdeck/web_activation.py').read_text(errors='strict')

def _directive_lines(text, directive):
    """Return the unparsed value of every matching unit directive."""
    prefix = directive + '='
    return tuple(
        line[len(prefix):]
        for line in text.splitlines()
        if line.startswith(prefix)
    )

def _directive_values(text, directive):
    """Return whitespace-separated values from every matching unit line."""
    return tuple(value for line in _directive_lines(text, directive) for value in line.split())

def _condition_directives(text):
    return tuple(
        line.split('=', 1)[0]
        for line in text.splitlines()
        if line.startswith('Condition')
    )

def _exec_directives(text):
    return tuple(line for line in text.splitlines() if line.startswith('Exec'))

# These files are installed or executed as part of the active deployment. A
# stale storage-era path here would make a clean Warp install resolve to a
# different tree even when all runtime defaults look correct. Test fixtures
# and historical archive compatibility are intentionally outside this gate.
active_path_files = tuple(dict.fromkeys((*required.values(),
    ROOT/'deploy/cloudflare/maxchat-termdeck-route.json',
    ROOT/'scripts/termdeck-web-prepare-runtime.sh',
    ROOT/'termdeck/desktop_integration.py',
    ROOT/'termdeck/release_manager.py',
    ROOT/'termdeck/web_activation.py',
)))
legacy_path_markers = (
    '/8tb',
    '/var/lib/cloudmcp-exec',
    '/warp/projects/software/termdeck',
    '/warp/runtime/termdeck',
    '/warp/state/termdeck',
)
legacy_path_hits = [
    {'path': str(path.relative_to(ROOT)), 'marker': marker}
    for path in active_path_files
    for marker in legacy_path_markers
    if marker in path.read_text(errors='strict')
]
if legacy_path_hits:
    raise SystemExit(f'active deployment contains legacy path(s): {legacy_path_hits}')

warp_path_checks = {
    'source_is_warp': CANONICAL_SOURCE in
        (ROOT/'termdeck/release_manager.py').read_text(errors='strict'),
    'runtime_is_warp': CANONICAL_RUNTIME in ''.join(texts.values()),
    'state_is_warp': CANONICAL_STATE in ''.join(texts.values()),
    'service_home_is_state': all(
        f'Environment=HOME={CANONICAL_STATE}' in texts[name]
        for name in ('backend', 'lan', 'recovery', 'cloudflared_unit', 'preview_unit', 'checkpoint')
    ),
}
if not all(warp_path_checks.values()):
    raise SystemExit(f'Warp deployment path contract failed: {warp_path_checks}')

canonical_mounts = f'{CANONICAL_SOURCE} {CANONICAL_RUNTIME} {CANONICAL_STATE}'
unit_stems = {
    'backend': 'termdeck-web-backend',
    'lan': 'termdeck-web-lan',
    'recovery': 'termdeck-web-recovery',
    'cloudflared_unit': 'termdeck-cloudflared',
    'preview_unit': 'termdeck-cloudflare-preview',
    'checkpoint': 'termdeck-checkpoint',
}
# Keep service relationships soft: Wants starts and After orders, while no
# unit requires another TermDeck service and therefore cannot cascade a stop.
unit_contracts = {
    'backend': {
        'Wants': ('network-online.target',),
        'After': ('warp.mount', 'network-online.target'),
        'conditions': {
            'ConditionPathIsMountPoint': ('/warp',),
            'ConditionPathIsDirectory': (CANONICAL_RUNTIME, CANONICAL_STATE),
            'ConditionPathExists': (),
        },
    },
    'lan': {
        'Wants': ('network-online.target', 'termdeck-web-backend.service'),
        'After': ('warp.mount', 'network-online.target', 'termdeck-web-backend.service'),
        'conditions': {
            'ConditionPathIsMountPoint': ('/warp',),
            'ConditionPathIsDirectory': (CANONICAL_RUNTIME,),
            'ConditionPathExists': (),
        },
    },
    'recovery': {
        'Wants': (),
        'After': ('warp.mount', 'default.target'),
        'conditions': {
            'ConditionPathIsMountPoint': ('/warp',),
            'ConditionPathIsDirectory': (
                CANONICAL_SOURCE,
                CANONICAL_MANAGED,
                CANONICAL_BIN,
            ),
            'ConditionPathExists': (f'{CANONICAL_STATE}/recovery/activation-v2/LATEST',),
        },
    },
    'cloudflared_unit': {
        'Wants': ('network-online.target', 'termdeck-web-lan.service'),
        'After': ('warp.mount', 'network-online.target', 'termdeck-web-lan.service'),
        'conditions': {
            'ConditionPathIsMountPoint': ('/warp',),
            'ConditionPathIsDirectory': (CANONICAL_RUNTIME,),
            'ConditionPathExists': (),
        },
    },
    'preview_unit': {
        'Wants': ('network-online.target', 'termdeck-web-lan.service'),
        'After': ('warp.mount', 'network-online.target', 'termdeck-web-lan.service'),
        'conditions': {
            'ConditionPathIsMountPoint': ('/warp',),
            'ConditionPathIsDirectory': (CANONICAL_RUNTIME,),
            'ConditionPathExists': (),
        },
    },
    'checkpoint': {
        'Wants': (),
        'After': ('warp.mount', 'user@1000.service', 'local-fs.target'),
        'conditions': {
            'ConditionPathIsMountPoint': ('/warp',),
            'ConditionPathIsDirectory': (CANONICAL_STATE, CANONICAL_MANAGED),
            'ConditionPathExists': (),
        },
    },
}

checks={
 'dynamic_lan_bind':'listen 0.0.0.0:7443 ssl' in texts['nginx'],
 'strict_host_allow':'allowed-hosts.map' in texts['nginx'] and 'return 421' in texts['nginx'],
 'websocket_upgrade':all(x in texts['nginx'] for x in ('proxy_set_header Upgrade $http_upgrade','proxy_set_header Connection "upgrade"','proxy_read_timeout 86400s')),
 'origin_tls_verified':'caPool:' in texts['tunnel_example'] and 'originServerName:' in texts['tunnel_example'] and 'noTLSVerify' not in texts['tunnel_example'],
 'dynamic_rfc1918':all(x in texts['prepare'] for x in ('ip -4 route get','192.168.0.0/16','subjectAltName')),
 'quick_tunnel_isolated':'cloudflare-preview-empty.yml' in texts['preview'] and 'trycloudflare' in texts['preview'],
 'active_paths_are_warp': not legacy_path_hits and all(warp_path_checks.values()),
}

execute_start = activation_text.find('def execute(')
rollback_start = activation_text.find('\ndef rollback(', execute_start)
execute_text = activation_text[execute_start:rollback_start if rollback_start >= 0 else None]
recovery_wrapper_embeds_latest = (
    'if spec.kind == "recovery":' in texts['wrappers']
    and '"--watchdog",\n            "--latest",' in texts['wrappers']
)
checks.update({
    'all_six_units_present': (
        len(unit_stems) == 6
        and set(unit_contracts) == set(unit_stems)
        and all(name in texts for name in unit_stems)
    ),
    'all_six_exact_mount_roots': all(
        _directive_lines(texts[name], 'RequiresMountsFor') == (canonical_mounts,)
        for name in unit_stems
    ),
    'all_twelve_exact_managed_wrappers': (
        len(unit_stems) * 2 == 12
        and len({f'{stem}{suffix}' for stem in unit_stems.values() for suffix in ('', '-stop')}) == 12
        and all(
            _exec_directives(texts[name]) == (
                f'ExecStart={CANONICAL_MANAGED}/{stem}',
                f'ExecStop={CANONICAL_MANAGED}/{stem}-stop',
            )
            for name, stem in unit_stems.items()
        )
    ),
    'soft_non_cascading_dependencies': all(
        _directive_values(texts[name], 'Wants') == contract['Wants']
        and _directive_values(texts[name], 'After') == contract['After']
        and _directive_values(texts[name], 'Requires') == ()
        for name, contract in unit_contracts.items()
    ),
    'all_six_restart_directly_on_failure': all(
        _directive_values(texts[name], 'Restart') == ('on-failure',)
        and _directive_values(texts[name], 'RestartMode') == ('direct',)
        for name in unit_stems
    ),
    'all_six_safe_kill_mode': all(
        _directive_values(texts[name], 'KillMode') == ('control-group',)
        for name in unit_stems
    ),
    'all_six_exact_conditions': all(
        _condition_directives(texts[name]) == tuple(
            directive
            for directive, values in contract['conditions'].items()
            for _ in values
        )
        and all(
            _directive_values(texts[name], directive) == values
            for directive, values in contract['conditions'].items()
        )
        for name, contract in unit_contracts.items()
    ),
    'all_six_home_is_state': all(
        tuple(value for value in _directive_values(texts[name], 'Environment') if value.startswith('HOME='))
        == (f'HOME={CANONICAL_STATE}',)
        for name in unit_stems
    ),
    'checkpoint_stop_is_unprefixed': 'ExecStop=-' not in texts['checkpoint'],
    'recovery_uses_latest_wrapper': (
        f'ExecStart={CANONICAL_MANAGED}/termdeck-web-recovery' in texts['recovery']
        and f'ExecStop={CANONICAL_MANAGED}/termdeck-web-recovery-stop' in texts['recovery']
        and 'wrapper = paths.managed_root / "termdeck-web-recovery"' in activation_text
        and 'command = shlex.join(recovery_command or [str(wrapper)])' in activation_text
        and 'shlex.join([str(wrapper), "--latest"])' not in activation_text
        and recovery_wrapper_embeds_latest
    ),
    'activation_keeps_web_allowlist': 'SERVICES = ("termdeck-web-backend.service", "termdeck-web-lan.service")' in activation_text,
    'wrapper_publication_precedes_web_stop': (
        execute_start >= 0
        and '_publish_wrappers(paths, release_id)' in execute_text
        and 'package_result.get("service_wrappers")' in execute_text
        and execute_text.find('_publish_wrappers(paths, release_id)')
        < execute_text.find('_stop_web()')
    ),
})
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
