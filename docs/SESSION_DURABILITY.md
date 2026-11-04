# Saved terminal durability

TermDeck keeps tab identity, window layout, launch command, working directory,
recovery recipe and private account binding in the session registry. New Python
registry connections use SQLite WAL with `synchronous=FULL`. A committed change
is flushed before success, subject to the storage device honoring flushes.

The verified directory observer also maintains `.termdeck/state/durability/`:

- `screens/<session-id>/<generation>.json`: private, atomically replaced copies
  of the visible screen and up to 10,000 tmux history lines, limited to 4 MiB.
  A capture pass starts every five seconds (plus the time to inspect the fleet).
  Each process generation has its own file. Missing panes and new shells never
  erase a previous generation. These are plain text screen snapshots, not a
  complete byte log, process image, or application-native conversation export.
- `registry/sessions-<UTC-date>T<hour>.sqlite3`: a consistent SQLite online
  backup, including committed WAL content, refreshed every 60 seconds. Hourly
  generations are retained. Backups are never automatically substituted for
  the live database, which avoids overwriting newer state or active writers.

The output recorder adds these private recovery artifacts:

- `journals/<session-id>/<time>-<random>.tdj`: append-only raw ANSI output with
  terminal geometry and reset records. Python and Rust share the `T1000J1`
  framing format, with a SHA-256 checksum on every frame. A `.tdj.commit`
  sidecar advertises only bytes already flushed with `fsync`; readers ignore
  later, uncommitted bytes. Every reconnect creates another generation and
  never overwrites an older application's recording.
- `journals/<session-id>/latest`: the canonical unattended generation;
  `latest-view` is a separate GTK/browser generation. Saved output defaults to
  the most recently published pointer, so a stopped sink cannot hide newer
  displayed output. Both histories remain separately selectable. Publication
  flushes both the sidecar and its directory before forwarding output to GTK
  or a browser. A storage failure pauses that renderer and reports the error;
  it does not kill the application or pretend its output is safely recorded.
- `applications/<session-id>/<time>-<random>.json`: observed exact native
  recovery commands and working directories. New observations are saved before
  updating the registry, and older observations survive a return to an idle
  shell. These files contain resume metadata, not copies of native transcripts.

The background observer installs output-only tmux pipes while no UI is open.
It adds no tmux clients, sends no input and does not resize or stop applications.
A pipe and its initial bounded screen capture are requested in one server-side
command list, with the pipe first. A private lifetime lease prevents overlapping
canonical writers. Existing user pipes are preserved and reported as coverage
gaps. Inventory timeouts retain existing sinks; restarting the observer does not
stop tmux-owned sinks. The stable pane's sink continues across an explicit pane
respawn, with the observer publishing the new process identity for size tracking.

The unattended pipe is asynchronous: it cannot delay tmux or another client
until storage has flushed. Strict flush-before-display applies to the new GTK
and web writers. Killing the whole server/account can still lose background
bytes in flight. Recording starts at attachment; earlier output exists only to
the extent retained by tmux or snapshots. A failed sink leaves the application
running and its committed history intact. Retry is bounded and never removes an
ambiguous or user-owned pipe.

Independent tabs may flush concurrently through `.journal-copy.lock`. The normal
SSD mirror copies and validates each generation's committed prefix without
pausing writers, publishes durable data before its watermark, and retains older
generations. Screen/SQLite snapshots use the separate `.lock`. Ordinary rsync
excludes `journals/`; copying an active journal and its watermark independently
would not provide a consistent recovery point.

All snapshots are private to the owning OS account. Terminal output can contain
secrets; do not put this directory in Git or expose it as an unauthenticated
static directory. No generation is automatically deleted. Monitor storage and
choose retention explicitly. An unavailable disk or failed capture is logged;
the last good copy is retained.

Read preserved output without sending it into a terminal application:

```sh
python -m termdeck.session_durability --root .termdeck/state/durability \
  --session-id <saved-tab-id>
python -m termdeck.session_durability --root .termdeck/state/durability \
  --session-id <saved-tab-id> --generation <listed-generation> > saved-output.txt
python -m termdeck.output_journal --root .termdeck/state/durability \
  --session-id <saved-tab-id> --list
python -m termdeck.output_journal --root .termdeck/state/durability \
  --session-id <saved-tab-id> --generation <listed-generation> --export saved-ansi.txt
```

The journal exporter creates a new mode-0600 file and refuses to overwrite one.
The browser's Saved output view uses an authenticated, same-account saved-pane
route even after the live tmux target is missing. It offers older generations,
disables terminal input and reconnects, and labels replay as saved output. A
Live terminal action leaves that mode. Replayed bytes are never sent as input
to a shell, and missing archives do not erase an already displayed screen.

The managed saved-terminal service runs a supervisor with bounded recovery
workers, checking again 30 seconds after each completed pass. Live panes receive
no input or lifecycle action. A previously ready attempt may be recovered again
in the same boot only after its original worker generation has disappeared,
the saved recipe still matches, the pane is authoritatively absent or dead,
and native metadata/writer locks prove quiescence. Private auth scope, native
resume identity, stopped/archive intent, completed applications and existing
account deferrals remain authoritative. An interrupted pending attempt has one
automatic retry per saved input and boot, with the same owner/quiescence checks.
Further interruptions, failed and legacy attempts require an exact-tab operator
retry. Normal shutdown drains the current recovery worker briefly and makes a
final bounded screen/registry save while tmux is still available.

Qualification uses disposable sockets and a real tmux server-loss test; it does
not kill the user's live sessions. Publish with `startup_publication.prepare`
and `publish`, and `directory_tracking.install`, preserving their inverse
receipts. Activate only those observer/recovery services. Updating these services
does not replace the web server or existing terminal/application processes.
An older installed application writer keeps its original SQLite connection
policy until that application is separately updated/reopened.

No application can promise zero loss of unsaved process memory after SIGKILL,
or recover bytes that had not reached a snapshot before a power failure. These
primary copies share the `/warp` volume: disk destruction requires a verified
copy on another physical device. A same-UID process can still signal TermDeck; preventing
another workload from issuing a host-wide `kill(-1, SIGKILL)` requires containment
at that workload's launch boundary, using a separate OS identity or PID namespace.
Snapshots preserve observed text; native application resume restores only what
the application itself persisted. Old output lost before this feature was
enabled cannot be recreated from a saved launch command.

Restoring a shell tab does not automatically restore every foreground program
the user typed into that shell. That requires an exact application-native resume
identity in its saved state. The observer recognizes OMP's process-bound
`terminal-sessions/pts-N` breadcrumb and validates its exact native JSONL header,
UUID, cwd device/inode, owner, process generation and foreground TTY. It retains
the effective HOME, agent directory and absolute launch prefix for `--resume`;
bare PATH-dependent launches, unsupported profile
layouts and stale/changed files fail closed. The observed owner is revalidated
at the registry write boundary. It never chooses the newest conversation by cwd
or emits OMP `--restore`, which could continue an unfinished task. Existing
Codex/Claude identity safeguards remain in force. Unknown applications still
need their own checkpoint/resume support. Do not report a replacement shell as
a restored OMP or Codex conversation.

On this host, `t1000-ssd-backup.timer` also copies the recovery registry and
screen generations to `/cyber-a/.termdeck-backups/termdeck/`. `/cyber-a` is on
NVMe device `nvme2n1`, physically separate from `/warp` on `nvme0n1`. The
oneshot service checks the mounted filesystem UUID
`7f9df7ce-7574-4308-ba94-bbf0b06897e4` and the private destination before copying.
An absent or different mount fails the copy instead of falling back to another
disk. No formatting, mount changes, deletion or modification of unrelated data
is part of this setup.

The first copy runs after activation; the timer runs again 60 seconds after each
completed attempt, with up to ten seconds of timer accuracy/jitter. Coordinated
source locks keep each copy consistent with the observer's atomic publication.
Rsync flushes screen/registry copies, delays replacement until transfer completes, keeps
directories at mode 0700 and files at 0600, and never deletes older generations.
The verified journal mirror then copies committed prefixes. The service flushes
destination directories. Failed copies leave the last
good generation available; systemd records the failure and the timer tries again.

Check the deployed backup with:

```sh
systemctl --user status t1000-ssd-backup.timer t1000-ssd-backup.service
python -m termdeck.session_durability \
  --root /cyber-a/.termdeck-backups/termdeck --session-id <saved-tab-id>
```

This replica protects against loss of the `/warp` SSD after a successful copy.
It shares the host and OS account, so host destruction, account-wide deletion
and uncommitted app memory still require other protections. It is not an
off-host backup or a native conversation export. Both SSD space and backup
service failures need monitoring; generations are not automatically pruned.

Deployment qualification is separate from source tests. Check the verified
payload named in `termdeck-directory-tracker.service` and the installed web
release before assuming these new recorder/viewer paths are active. The prior
screen/registry observer and SSD timer can remain active while a replacement
recorder is being qualified. Never enable a recorder that fails the disposable
fleet's output, responsiveness, shutdown or pane-preservation checks.
