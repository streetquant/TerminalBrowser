//! Lightweight tmux-owned output sink writing T1000J1 durable journals.
//!
//! Preserves the exact security, locking, atomic commit-watermark, and fail-closed
//! 4 MiB backlog invariants of `termdeck/output_journal.py` without per-pane Python
//! interpreter overhead.

use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const MAGIC: &[u8] = b"T1000J1\n";
const MAX_RECORD: usize = 8 * 1024 * 1024 + 4;
const PIPE_READ_CHUNK_BYTES: usize = 64 * 1024;
const MAX_PIPE_BACKLOG_BYTES: usize = 4 * 1024 * 1024;
const COALESCE_WINDOW: Duration = Duration::from_millis(150);
const MAX_COALESCE_BATCH_BYTES: usize = 256 * 1024;
const IDLE_GEOMETRY_INTERVAL: Duration = Duration::from_secs(2);
const ACTIVE_GEOMETRY_INTERVAL: Duration = Duration::from_millis(500);
const INITIAL_REPLAY_WAIT: Duration = Duration::from_secs(3);

#[derive(Debug)]
struct Args {
    root: PathBuf,
    session_id: String,
    pane_id: String,
    pane_pid: u32,
    pane_start: u64,
    server_pid: u32,
    server_start: u64,
    columns: u16,
    rows: u16,
    token: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ProcStat {
    ppid: u32,
    starttime: u64,
}

#[derive(Serialize)]
struct PipeOwner {
    session_id: String,
    pane_id: String,
    pane_pid: u32,
    pane_start: u64,
    server_pid: u32,
    server_start: u64,
    pid: u32,
    starttime: u64,
    argv: Vec<String>,
    token: String,
    generation: String,
}

#[derive(Serialize)]
struct FailureNotice<'a> {
    error: &'a str,
    #[serde(rename = "type")]
    error_type: &'a str,
    reason: &'a str,
}

#[derive(serde::Deserialize)]
struct PreviousPipeOwner {
    session_id: String,
    pane_id: String,
    pane_pid: u32,
    pane_start: u64,
    server_pid: u32,
    server_start: u64,
    token: String,
    generation: String,
}

#[derive(serde::Deserialize)]
struct StoredFailureNotice {
    reason: Option<String>,
    #[serde(rename = "type")]
    error_type: Option<String>,
}

#[derive(Serialize)]
struct RecordingLineageEvent<'a> {
    schema: &'static str,
    event: &'static str,
    gap_detected: bool,
    session_id: &'a str,
    pane_id: &'a str,
    previous_generation: &'a str,
    new_generation: &'a str,
    prior_failure_reason: &'a str,
    gap_ms: Option<u64>,
    timestamp_ms: u64,
}

#[derive(serde::Deserialize)]
struct PaneCurrent {
    pane_id: String,
    pid: u32,
    starttime: u64,
    server_pid: u32,
    server_start: u64,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::other(message.into())
}

fn self_uid() -> io::Result<u32> {
    Ok(fs::metadata("/proc/self")?.uid())
}

fn hex(data: &[u8]) -> String {
    const DIGITS: &[u8] = b"0123456789abcdef";
    let mut out = String::with_capacity(data.len() * 2);
    for &byte in data {
        out.push(char::from(DIGITS[usize::from(byte >> 4)]));
        out.push(char::from(DIGITS[usize::from(byte & 15)]));
    }
    out
}

fn valid_session_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn valid_token(token: &str) -> bool {
    token.len() == 32
        && token
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn valid_pane_id(pane_id: &str) -> bool {
    pane_id
        .strip_prefix('%')
        .is_some_and(|digits| !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
}

fn valid_geometry(cols: u16, rows: u16) -> bool {
    (2..=1000).contains(&cols) && (2..=500).contains(&rows)
}

fn parse_stat_text(text: &str) -> Option<ProcStat> {
    let (_, rest_text) = text.rsplit_once(')')?;
    let rest: Vec<&str> = rest_text.split_whitespace().collect();
    if rest.len() < 20 {
        return None;
    }
    let ppid = rest[1].parse::<u32>().ok()?;
    let starttime = rest[19].parse::<u64>().ok()?;
    Some(ProcStat { ppid, starttime })
}

fn parse_proc_stat(pid: u32) -> Option<ProcStat> {
    let text = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    parse_stat_text(&text)
}

fn self_stat() -> Option<ProcStat> {
    let text = fs::read_to_string("/proc/self/stat").ok()?;
    parse_stat_text(&text)
}

fn self_cmdline() -> io::Result<Vec<String>> {
    let mut buf = Vec::with_capacity(512);
    let file = File::open("/proc/self/cmdline")?;
    file.take(65_537).read_to_end(&mut buf)?;
    if buf.is_empty() || buf.len() > 65_536 {
        return Err(invalid("invalid process cmdline"));
    }
    while buf.last() == Some(&0) {
        buf.pop();
    }
    Ok(buf
        .split(|&b| b == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).into_owned())
        .collect())
}

fn ensure_dir(path: &Path) -> io::Result<PathBuf> {
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    if !abs.exists() {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&abs)?;
    }
    let canonical = fs::canonicalize(&abs)?;
    let meta = fs::symlink_metadata(&abs)?;
    if canonical != abs
        || !meta.is_dir()
        || meta.uid() != self_uid()?
        || meta.permissions().mode() & 0o777 != 0o700
    {
        return Err(invalid("unsafe durability directory"));
    }
    Ok(abs)
}

fn sync_dir(path: &Path) -> io::Result<()> {
    File::open(path)?.sync_all()
}

fn journal_directory(root: &Path, session_id: &str) -> io::Result<PathBuf> {
    if !valid_session_id(session_id) {
        return Err(invalid("invalid terminal identity"));
    }
    let root = ensure_dir(root)?;
    let journals = ensure_dir(&root.join("journals"))?;
    sync_dir(&root)?;
    let directory = ensure_dir(&journals.join(session_id))?;
    sync_dir(&journals)?;
    Ok(directory)
}

fn open_read_safe(path: &Path) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file()
        || meta.nlink() > 1
        || meta.uid() != self_uid()?
        || meta.permissions().mode() & 0o777 != 0o600
    {
        return Err(invalid("unsafe terminal recording"));
    }
    Ok(file)
}

fn flock_op(file: &File, op: i32) -> io::Result<()> {
    // SAFETY: `file.as_raw_fd()` is an open descriptor owned by `file` for the call duration.
    let rc = unsafe { libc::flock(file.as_raw_fd(), op) };
    if rc == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

fn open_lock_safe(path: &Path) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file()
        || meta.nlink() != 1
        || meta.uid() != self_uid()?
        || meta.permissions().mode() & 0o777 != 0o600
    {
        return Err(invalid("unsafe recording lock"));
    }
    Ok(file)
}

fn atomic_write(path: &Path, content: &[u8], sync_parent: bool) -> io::Result<()> {
    let mut random = [0_u8; 16];
    getrandom::fill(&mut random).map_err(|_| invalid("randomness unavailable"))?;
    let name = path
        .file_name()
        .ok_or_else(|| invalid("missing filename"))?
        .to_string_lossy();
    let temporary = path.with_file_name(format!(".{name}.{}", hex(&random)));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&temporary)?;
        file.write_all(content)?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        if sync_parent && let Some(parent) = path.parent() {
            sync_dir(parent)?;
        }
        Ok(())
    })();
    let _cleanup = fs::remove_file(&temporary);
    result
}

struct PrimaryJournal {
    directory: PathBuf,
    generation: String,
    file: File,
    committed: u64,
    backup_lock: File,
    pointer_published: bool,
    failed: bool,
}

impl PrimaryJournal {
    fn open(root: &Path, session_id: &str) -> io::Result<Self> {
        let directory = journal_directory(root, session_id)?;
        let copy_lock_path = root.join(".journal-copy.lock");
        let backup_lock = open_lock_safe(&copy_lock_path)?;
        let mut random = [0_u8; 16];
        getrandom::fill(&mut random).map_err(|_| invalid("randomness unavailable"))?;
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| invalid("invalid clock"))?
            .as_millis();
        let generation = format!("{millis:020}-{}.tdj", hex(&random));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(directory.join(&generation))?;
        file.write_all(MAGIC)?;
        file.sync_all()?;
        sync_dir(&directory)?;
        Ok(Self {
            directory,
            generation,
            file,
            committed: MAGIC.len() as u64,
            backup_lock,
            pointer_published: false,
            failed: false,
        })
    }

    fn append(&mut self, data: &[u8], geometry: Option<(u16, u16)>, reset: bool) -> io::Result<()> {
        if self.failed {
            return Err(invalid("terminal recording previously failed"));
        }
        let result = self.append_inner(data, geometry, reset);
        if result.is_err() {
            self.failed = true;
        }
        result
    }

    fn append_inner(
        &mut self,
        data: &[u8],
        geometry: Option<(u16, u16)>,
        reset: bool,
    ) -> io::Result<()> {
        let kind = match geometry {
            Some(_) => u8::from(!reset) * 2, // 0 = reset+geometry, 2 = resize
            None => 1,
        };
        let mut body = Vec::with_capacity(data.len() + 5);
        body.push(kind);
        if let Some((cols, rows)) = geometry {
            if !valid_geometry(cols, rows) {
                return Err(invalid("invalid terminal geometry"));
            }
            body.extend_from_slice(&cols.to_be_bytes());
            body.extend_from_slice(&rows.to_be_bytes());
        }
        let payload_len = body.len() - 1 + data.len();
        if payload_len > MAX_RECORD {
            return Err(invalid("terminal recording frame too large"));
        }
        body.extend_from_slice(data);
        let len_u32 =
            u32::try_from(payload_len).map_err(|_| invalid("terminal recording frame too large"))?;
        let digest = Sha256::digest(&body);

        flock_op(&self.backup_lock, libc::LOCK_SH)?;
        let write_result = (|| -> io::Result<()> {
            self.file.write_all(&len_u32.to_be_bytes())?;
            self.file.write_all(&body)?;
            self.file.write_all(&digest)?;
            self.file.sync_all()?;
            self.committed = self
                .committed
                .checked_add(4 + body.len() as u64 + 32)
                .ok_or_else(|| invalid("journal offset overflow"))?;
            let commit_bytes = self.committed.to_string();
            atomic_write(
                &self.directory.join(format!("{}.commit", self.generation)),
                commit_bytes.as_bytes(),
                !self.pointer_published,
            )?;
            let pointer_path = self.directory.join("latest");
            let mut should_publish_pointer = !self.pointer_published;
            if !should_publish_pointer {
                let view_mtime = fs::metadata(self.directory.join("latest-view"))
                    .and_then(|m| m.modified())
                    .ok();
                let own_mtime = fs::metadata(&pointer_path).and_then(|m| m.modified()).ok();
                if let (Some(view), Some(own)) = (view_mtime, own_mtime)
                    && view >= own
                {
                    should_publish_pointer = true;
                }
            }
            if should_publish_pointer {
                atomic_write(&pointer_path, self.generation.as_bytes(), true)?;
                self.pointer_published = true;
            }
            Ok(())
        })();
        let _unlock = flock_op(&self.backup_lock, libc::LOCK_UN);
        write_result
    }
}

enum PipeEvent {
    Data(Vec<u8>),
    Eof,
}

#[derive(Clone)]
struct OverflowNoticeTarget {
    directory: PathBuf,
    token: String,
}

fn write_overflow_notice_bounded(target: Option<OverflowNoticeTarget>) {
    let Some(target) = target else {
        return;
    };
    let handle = thread::spawn(move || {
        if let Ok(bytes) = serde_json::to_vec(&FailureNotice {
            error: "output backlog exceeded 4 MiB",
            error_type: "BacklogOverflow",
            reason: "backlog_overflow",
        }) {
            let mut random = [0_u8; 8];
            let suffix = if getrandom::fill(&mut random).is_ok() {
                hex(&random)
            } else {
                process::id().to_string()
            };
            let dest = target.directory.join(format!("{}.failed.json", target.token));
            let tmp = target
                .directory
                .join(format!(".{}.failed.json.{suffix}", target.token));
            if let Ok(mut f) = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(&tmp)
            {
                if f.write_all(&bytes).is_ok() {
                    let _ = fs::rename(&tmp, &dest);
                }
                let _ = fs::remove_file(&tmp);
            }
        }
    });
    let deadline = Instant::now() + Duration::from_millis(200);
    while !handle.is_finished() && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(5));
    }
}

fn record_reattachment_if_same_pane(directory: &Path, args: &Args, new_generation: &str) {
    let Ok(prev_file) = open_read_safe(&directory.join("pipe-owner.json")) else {
        return;
    };
    let mut raw = Vec::new();
    if prev_file.take(16_384).read_to_end(&mut raw).is_err() {
        return;
    }
    let Ok(prev) = serde_json::from_slice::<PreviousPipeOwner>(&raw) else {
        return;
    };
    if prev.session_id != args.session_id
        || prev.pane_id != args.pane_id
        || prev.pane_pid != args.pane_pid
        || prev.pane_start != args.pane_start
        || prev.server_pid != args.server_pid
        || prev.server_start != args.server_start
    {
        return;
    }
    let mut prior_reason = String::from("unclean_exit_or_sigkill");
    if valid_token(&prev.token)
        && let Ok(fail_file) = open_read_safe(&directory.join(format!("{}.failed.json", prev.token)))
    {
        let mut fail_raw = Vec::new();
        if fail_file.take(8_192).read_to_end(&mut fail_raw).is_ok()
            && let Ok(doc) = serde_json::from_slice::<StoredFailureNotice>(&fail_raw)
            && let Some(reason) = doc.reason.or(doc.error_type)
            && !reason.is_empty()
        {
            prior_reason = reason;
        }
    }
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
        .unwrap_or(0);
    let gap_ms = fs::symlink_metadata(directory.join(format!("{}.commit", prev.generation)))
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
        .map(|commit_ms| now_ms.saturating_sub(commit_ms));

    let event = RecordingLineageEvent {
        schema: "termdeck.recording-event.v1",
        event: "sink_reattached",
        gap_detected: true,
        session_id: &args.session_id,
        pane_id: &args.pane_id,
        previous_generation: &prev.generation,
        new_generation,
        prior_failure_reason: &prior_reason,
        gap_ms,
        timestamp_ms: now_ms,
    };
    let Ok(mut line) = serde_json::to_vec(&event) else {
        return;
    };
    line.push(b'\n');
    if let Ok(mut log) = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(directory.join("recording-events.jsonl"))
        && let Ok(meta) = log.metadata()
        && meta.is_file()
        && meta.nlink() == 1
        && self_uid().is_ok_and(|u| meta.uid() == u)
        && meta.permissions().mode() & 0o777 == 0o600
    {
        let _ = log.write_all(&line);
        let _ = log.sync_all();
    }
}

fn spawn_pipe_reader<R: Read + Send + 'static>(
    mut reader: R,
    overflow_notice: Option<OverflowNoticeTarget>,
) -> (mpsc::Receiver<PipeEvent>, Arc<AtomicUsize>) {
    let (tx, rx) = mpsc::sync_channel(4096);
    let queued_bytes = Arc::new(AtomicUsize::new(0));
    let worker_bytes = Arc::clone(&queued_bytes);
    thread::Builder::new()
        .name("terminal-recording-input".to_owned())
        .spawn(move || {
            let mut buf = vec![0_u8; PIPE_READ_CHUNK_BYTES];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => {
                        let _ = tx.send(PipeEvent::Eof);
                        break;
                    }
                    Ok(n) => {
                        let current = worker_bytes.fetch_add(n, Ordering::AcqRel);
                        if current.saturating_add(n) > MAX_PIPE_BACKLOG_BYTES {
                            drop(reader);
                            write_overflow_notice_bounded(overflow_notice);
                            eprintln!("terminal recording failed: output backlog exceeded 4 MiB");
                            process::exit(1);
                        }
                        if tx.try_send(PipeEvent::Data(buf[..n].to_vec())).is_err() {
                            drop(reader);
                            write_overflow_notice_bounded(overflow_notice);
                            eprintln!("terminal recording failed: output backlog exceeded 4 MiB");
                            process::exit(1);
                        }
                    }
                    Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
                    Err(_) => {
                        let _ = tx.send(PipeEvent::Eof);
                        break;
                    }
                }
            }
        })
        .ok();
    (rx, queued_bytes)
}

fn tty_winsize(fd: i32) -> Option<(u16, u16)> {
    // SAFETY: Caller passes an open file descriptor without controlling-terminal
    // ownership; `ws` is zeroed before `TIOCGWINSZ` and checked on success.
    unsafe {
        if libc::isatty(fd) != 1 {
            return None;
        }
        let mut ws: libc::winsize = std::mem::zeroed();
        if libc::ioctl(fd, libc::TIOCGWINSZ, &mut ws) == 0 {
            Some((ws.ws_col, ws.ws_row))
        } else {
            None
        }
    }
}

fn parse_args(raw: impl IntoIterator<Item = String>) -> Result<Args, String> {
    let mut iter = raw.into_iter().peekable();
    if iter.peek().is_some_and(|arg| arg == "--record-output") {
        let _ = iter.next();
    }
    let mut root = None;
    let mut session_id = None;
    let mut pane_id = None;
    let mut pane_pid = None;
    let mut pane_start = None;
    let mut server_pid = None;
    let mut server_start = None;
    let mut columns = None;
    let mut rows = None;
    let mut token = None;

    while let Some(flag) = iter.next() {
        let value = iter
            .next()
            .ok_or_else(|| format!("missing value for {flag}"))?;
        match flag.as_str() {
            "--root" => root = Some(PathBuf::from(value)),
            "--session-id" => session_id = Some(value),
            "--pane-id" => pane_id = Some(value),
            "--pane-pid" => pane_pid = Some(value.parse().map_err(|_| "invalid --pane-pid")?),
            "--pane-start" => {
                pane_start = Some(value.parse().map_err(|_| "invalid --pane-start")?)
            }
            "--server-pid" => {
                server_pid = Some(value.parse().map_err(|_| "invalid --server-pid")?)
            }
            "--server-start" => {
                server_start = Some(value.parse().map_err(|_| "invalid --server-start")?)
            }
            "--columns" => columns = Some(value.parse().map_err(|_| "invalid --columns")?),
            "--rows" => rows = Some(value.parse().map_err(|_| "invalid --rows")?),
            "--token" => token = Some(value),
            _ => return Err(format!("unknown argument: {flag}")),
        }
    }

    let args = Args {
        root: root.ok_or("missing --root")?,
        session_id: session_id.ok_or("missing --session-id")?,
        pane_id: pane_id.ok_or("missing --pane-id")?,
        pane_pid: pane_pid.ok_or("missing --pane-pid")?,
        pane_start: pane_start.ok_or("missing --pane-start")?,
        server_pid: server_pid.ok_or("missing --server-pid")?,
        server_start: server_start.ok_or("missing --server-start")?,
        columns: columns.ok_or("missing --columns")?,
        rows: rows.ok_or("missing --rows")?,
        token: token.ok_or("missing --token")?,
    };
    if !valid_token(&args.token)
        || !valid_pane_id(&args.pane_id)
        || !valid_session_id(&args.session_id)
    {
        return Err("invalid recording identity".to_owned());
    }
    if !valid_geometry(args.columns, args.rows) {
        return Err("invalid terminal geometry".to_owned());
    }
    Ok(args)
}

fn read_initial_replay(directory: &Path, token: &str) -> io::Result<Vec<u8>> {
    let initial_path = directory.join(format!("{token}.initial"));
    let deadline = Instant::now() + INITIAL_REPLAY_WAIT;
    loop {
        match open_read_safe(&initial_path) {
            Ok(file) => {
                let mut replay = Vec::new();
                file.take((MAX_RECORD + 1) as u64).read_to_end(&mut replay)?;
                if replay.len() > MAX_RECORD - 4 {
                    return Err(invalid("recording initial replay too large"));
                }
                return Ok(replay);
            }
            Err(err) if err.kind() == io::ErrorKind::NotFound => {
                if Instant::now() >= deadline {
                    return Ok(Vec::new());
                }
                thread::sleep(Duration::from_millis(10));
            }
            Err(err) => return Err(err),
        }
    }
}

struct GeometryWatcher {
    pane_id: String,
    server_pid: u32,
    server_start: u64,
    current_pid: u32,
    current_start: u64,
    geometry: (u16, u16),
    pane_current_stamp: Option<(u64, u64, i64)>,
}

impl GeometryWatcher {
    fn maybe_refresh_pid(&mut self, directory: &Path) {
        let path = directory.join("pane-current.json");
        let Ok(meta) = fs::symlink_metadata(&path) else {
            return;
        };
        let stamp = (meta.dev(), meta.ino(), meta.mtime_nsec());
        if self.pane_current_stamp == Some(stamp) {
            return;
        }
        self.pane_current_stamp = Some(stamp);
        if let Ok(stream) = open_read_safe(&path) {
            let mut buf = Vec::new();
            if stream.take(4096).read_to_end(&mut buf).is_ok()
                && let Ok(current) = serde_json::from_slice::<PaneCurrent>(&buf)
                && current.pane_id == self.pane_id
                && current.server_pid == self.server_pid
                && current.server_start == self.server_start
            {
                self.current_pid = current.pid;
                self.current_start = current.starttime;
            }
        }
    }

    fn poll_resize(&mut self, directory: &Path) -> Option<(u16, u16)> {
        self.maybe_refresh_pid(directory);
        let current = parse_proc_stat(self.current_pid)?;
        if current.starttime != self.current_start {
            return None;
        }
        let tty = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOCTTY | libc::O_NONBLOCK | libc::O_CLOEXEC)
            .open(format!("/proc/{}/fd/0", self.current_pid))
            .ok()?;
        let (cols, rows) = tty_winsize(tty.as_raw_fd())?;
        if (cols, rows) != self.geometry && valid_geometry(cols, rows) {
            self.geometry = (cols, rows);
            Some((cols, rows))
        } else {
            None
        }
    }
}

fn record_pipe(args: &Args) -> io::Result<()> {
    let pane = parse_proc_stat(args.pane_pid)
        .ok_or_else(|| invalid("recording pane process unavailable"))?;
    let server = parse_proc_stat(args.server_pid)
        .ok_or_else(|| invalid("recording server process unavailable"))?;
    let own = self_stat().ok_or_else(|| invalid("recording self stat unavailable"))?;
    if pane.starttime != args.pane_start
        || server.starttime != args.server_start
        || own.ppid != args.server_pid
    {
        return Err(invalid("recording pane/server generation changed"));
    }

    let directory = journal_directory(&args.root, &args.session_id)?;
    let (rx, queued_bytes) = spawn_pipe_reader(
        io::stdin(),
        Some(OverflowNoticeTarget {
            directory: directory.clone(),
            token: args.token.clone(),
        }),
    );
    let primary_lease = open_lock_safe(&directory.join(".primary.lock"))?;
    flock_op(&primary_lease, libc::LOCK_EX | libc::LOCK_NB)
        .map_err(|err| invalid(format!("primary recording lease busy: {err}")))?;

    let mut journal = PrimaryJournal::open(&args.root, &args.session_id)?;
    let replay = read_initial_replay(&journal.directory, &args.token)?;
    journal.append(&replay, Some((args.columns, args.rows)), true)?;
    record_reattachment_if_same_pane(&journal.directory, args, &journal.generation);

    let owner = PipeOwner {
        session_id: args.session_id.clone(),
        pane_id: args.pane_id.clone(),
        pane_pid: args.pane_pid,
        pane_start: args.pane_start,
        server_pid: args.server_pid,
        server_start: args.server_start,
        pid: process::id(),
        starttime: own.starttime,
        argv: self_cmdline()?,
        token: args.token.clone(),
        generation: journal.generation.clone(),
    };
    let owner_bytes = serde_json::to_vec(&owner).map_err(|err| invalid(err.to_string()))?;
    atomic_write(&journal.directory.join("pipe-owner.json"), &owner_bytes, true)?;

    let mut watcher = GeometryWatcher {
        pane_id: args.pane_id.clone(),
        server_pid: args.server_pid,
        server_start: args.server_start,
        current_pid: args.pane_pid,
        current_start: args.pane_start,
        geometry: (args.columns, args.rows),
        pane_current_stamp: None,
    };
    let mut last_geometry_check = Instant::now() - IDLE_GEOMETRY_INTERVAL;
    loop {
        let mut eof = false;
        let mut data = Vec::new();
        match rx.recv_timeout(IDLE_GEOMETRY_INTERVAL) {
            Ok(PipeEvent::Eof) => eof = true,
            Ok(PipeEvent::Data(first)) => {
                queued_bytes.fetch_sub(first.len(), Ordering::AcqRel);
                data.extend_from_slice(&first);
                let deadline = Instant::now() + COALESCE_WINDOW;
                while data.len() < MAX_COALESCE_BATCH_BYTES {
                    let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                        break;
                    };
                    if remaining.is_zero() {
                        break;
                    }
                    match rx.recv_timeout(remaining) {
                        Ok(PipeEvent::Eof) => {
                            eof = true;
                            break;
                        }
                        Ok(PipeEvent::Data(next)) => {
                            queued_bytes.fetch_sub(next.len(), Ordering::AcqRel);
                            data.extend_from_slice(&next);
                        }
                        Err(mpsc::RecvTimeoutError::Timeout) => break,
                        Err(mpsc::RecvTimeoutError::Disconnected) => {
                            eof = true;
                            break;
                        }
                    }
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => eof = true,
        }

        let now = Instant::now();
        let interval = if data.is_empty() {
            IDLE_GEOMETRY_INTERVAL
        } else {
            ACTIVE_GEOMETRY_INTERVAL
        };
        if eof || now.duration_since(last_geometry_check) >= interval {
            last_geometry_check = now;
            if let Some(new_geometry) = watcher.poll_resize(&journal.directory) {
                journal.append(&[], Some(new_geometry), false)?;
            }
        }
        if !data.is_empty() {
            journal.append(&data, None, true)?;
        }
        if eof {
            break;
        }
    }
    drop(primary_lease);
    Ok(())
}

fn write_failure_notice(args: &Args, error: &io::Error) {
    if let Ok(directory) = journal_directory(&args.root, &args.session_id) {
        let msg = error.to_string();
        if let Ok(bytes) = serde_json::to_vec(&FailureNotice {
            error: &msg,
            error_type: "OSError",
            reason: "io_error",
        }) {
            let _ = atomic_write(
                &directory.join(format!("{}.failed.json", args.token)),
                &bytes,
                false,
            );
        }
    }
}

fn main() -> process::ExitCode {
    let args = match parse_args(std::env::args().skip(1)) {
        Ok(args) => args,
        Err(msg) => {
            eprintln!("termdeck-record-output: {msg}");
            return process::ExitCode::from(2);
        }
    };
    if let Err(err) = record_pipe(&args) {
        eprintln!("terminal recording failed: {err}");
        write_failure_notice(&args, &err);
        return process::ExitCode::from(1);
    }
    process::ExitCode::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Seek;

    struct Fixture(PathBuf);
    impl Fixture {
        fn new(name: &str) -> io::Result<Self> {
            let mut random = [0_u8; 8];
            getrandom::fill(&mut random).map_err(|_| invalid("random failed"))?;
            let path = std::env::temp_dir().join(format!("termdeck-rec-{name}-{}", hex(&random)));
            ensure_dir(&path)?;
            Ok(Self(path))
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn parses_proc_stat_with_spaces_and_parens_in_comm() {
        let sample = "12345 (python3 (worker) test) S 4479 12345 12345 0 -1 4194304 100 0 0 0 5 1 0 0 20 0 1 0 9876543 1000 50 18446744073709551615";
        let stat = parse_stat_text(sample).unwrap_or_else(|| panic!("expected valid proc stat"));
        assert_eq!(stat.ppid, 4479);
        assert_eq!(stat.starttime, 9_876_543);
    }

    #[test]
    fn parses_args_with_or_without_leading_record_output_flag() {
        let base = vec![
            "--root", "/tmp/d", "--session-id", "td-123", "--pane-id", "%7",
            "--pane-pid", "10", "--pane-start", "11", "--server-pid", "12",
            "--server-start", "13", "--columns", "91", "--rows", "29",
            "--token", "0123456789abcdef0123456789abcdef",
        ]
        .into_iter()
        .map(str::to_owned)
        .collect::<Vec<_>>();
        let mut with_prefix = vec!["--record-output".to_owned()];
        with_prefix.extend(base.clone());
        let a = parse_args(base).unwrap_or_else(|e| panic!("base args: {e}"));
        let b = parse_args(with_prefix).unwrap_or_else(|e| panic!("prefixed args: {e}"));
        assert_eq!(a.session_id, b.session_id);
        assert_eq!(b.columns, 91);
        assert_eq!(b.rows, 29);
    }

    #[test]
    fn primary_journal_matches_t1000j1_format_and_watermark() -> io::Result<()> {
        let fixture = Fixture::new("t1000j1")?;
        let mut journal = PrimaryJournal::open(&fixture.0, "td-tab")?;
        journal.append(b"initial", Some((91, 29)), true)?;
        journal.append(b"hello ", None, true)?;
        journal.append(b"", Some((100, 40)), false)?;

        let tdj_path = journal.directory.join(&journal.generation);
        let commit_path = journal
            .directory
            .join(format!("{}.commit", journal.generation));
        let committed: u64 = fs::read_to_string(commit_path)?
            .trim()
            .parse()
            .map_err(|_| invalid("bad commit"))?;
        assert_eq!(committed, fs::metadata(&tdj_path)?.len());
        assert_eq!(
            fs::read_to_string(journal.directory.join("latest"))?,
            journal.generation
        );

        let mut f = File::open(&tdj_path)?;
        let mut magic = [0_u8; 8];
        f.read_exact(&mut magic)?;
        assert_eq!(&magic, MAGIC);
        for (expected_kind, expected_payload) in [
            (0_u8, [91_u16.to_be_bytes(), 29_u16.to_be_bytes()].concat().into_iter().chain(b"initial".iter().copied()).collect::<Vec<_>>()),
            (1_u8, b"hello ".to_vec()),
            (2_u8, [100_u16.to_be_bytes(), 40_u16.to_be_bytes()].concat().to_vec()),
        ] {
            let mut hdr = [0_u8; 5];
            f.read_exact(&mut hdr)?;
            let len = u32::from_be_bytes([hdr[0], hdr[1], hdr[2], hdr[3]]) as usize;
            let kind = hdr[4];
            assert_eq!(kind, expected_kind);
            assert_eq!(len, expected_payload.len());
            let mut payload = vec![0_u8; len];
            f.read_exact(&mut payload)?;
            assert_eq!(payload, expected_payload);
            let mut digest = [0_u8; 32];
            f.read_exact(&mut digest)?;
            let mut body = vec![kind];
            body.extend_from_slice(&payload);
            assert_eq!(Sha256::digest(&body)[..], digest);
        }
        assert_eq!(f.stream_position()?, committed);
        Ok(())
    }

    #[test]
    fn shared_copy_lock_blocks_append_while_backup_holds_exclusive() -> io::Result<()> {
        let fixture = Fixture::new("lock")?;
        let mut journal = PrimaryJournal::open(&fixture.0, "td-lock")?;
        let copy_lock = open_lock_safe(&fixture.0.join(".journal-copy.lock"))?;
        flock_op(&copy_lock, libc::LOCK_EX)?;
        // Try-lock from another handle should fail while exclusive lock is held.
        assert!(flock_op(&journal.backup_lock, libc::LOCK_SH | libc::LOCK_NB).is_err());
        flock_op(&copy_lock, libc::LOCK_UN)?;
        journal.append(b"after unlock", None, true)?;
        Ok(())
    }

    #[test]
    fn pipe_reader_delivers_chunks_then_eof() -> io::Result<()> {
        let payload = b"chunk-1|chunk-2|chunk-3".to_vec();
        let (rx, queued) = spawn_pipe_reader(io::Cursor::new(payload.clone()), None);
        let mut out = Vec::new();
        while let Ok(event) = rx.recv_timeout(Duration::from_secs(2)) {
            match event {
                PipeEvent::Data(bytes) => {
                    queued.fetch_sub(bytes.len(), Ordering::AcqRel);
                    out.extend_from_slice(&bytes);
                }
                PipeEvent::Eof => break,
            }
        }
        assert_eq!(out, payload);
        assert_eq!(queued.load(Ordering::Acquire), 0);
        Ok(())
    }

    #[test]
    fn overflow_notice_records_structured_backlog_failure_json() -> io::Result<()> {
        let fixture = Fixture::new("overflow")?;
        let directory = journal_directory(&fixture.0, "td-overflow")?;
        let token = "abcdef0123456789abcdef0123456789".to_owned();
        write_overflow_notice_bounded(Some(OverflowNoticeTarget {
            directory: directory.clone(),
            token: token.clone(),
        }));
        let raw = fs::read_to_string(directory.join(format!("{token}.failed.json")))?;
        let parsed: serde_json::Value =
            serde_json::from_str(&raw).map_err(|e| invalid(e.to_string()))?;
        assert_eq!(parsed["type"], "BacklogOverflow");
        assert_eq!(parsed["reason"], "backlog_overflow");
        Ok(())
    }

    #[test]
    fn reattachment_lineage_records_gap_only_when_same_pane_generation_continues() -> io::Result<()> {
        let fixture = Fixture::new("lineage")?;
        let mut first = PrimaryJournal::open(&fixture.0, "td-gap")?;
        first.append(b"gen-1", Some((80, 24)), true)?;
        let token1 = "11111111111111111111111111111111".to_owned();
        write_overflow_notice_bounded(Some(OverflowNoticeTarget {
            directory: first.directory.clone(),
            token: token1.clone(),
        }));
        let owner1 = PipeOwner {
            session_id: "td-gap".to_owned(),
            pane_id: "%9".to_owned(),
            pane_pid: 4242,
            pane_start: 9001,
            server_pid: 100,
            server_start: 200,
            pid: 5555,
            starttime: 9005,
            argv: vec!["termdeck-record-output".to_owned()],
            token: token1,
            generation: first.generation.clone(),
        };
        atomic_write(
            &first.directory.join("pipe-owner.json"),
            &serde_json::to_vec(&owner1).map_err(|e| invalid(e.to_string()))?,
            true,
        )?;

        let same_pane_args = Args {
            root: fixture.0.clone(),
            session_id: "td-gap".to_owned(),
            pane_id: "%9".to_owned(),
            pane_pid: 4242,
            pane_start: 9001,
            server_pid: 100,
            server_start: 200,
            columns: 80,
            rows: 24,
            token: "22222222222222222222222222222222".to_owned(),
        };
        let second = PrimaryJournal::open(&fixture.0, "td-gap")?;
        record_reattachment_if_same_pane(&second.directory, &same_pane_args, &second.generation);

        // A brand-new pane process (different pane_start) must not emit a false gap record.
        let mut restarted_args = same_pane_args;
        restarted_args.pane_start = 9999;
        record_reattachment_if_same_pane(&second.directory, &restarted_args, "00000000000000000003-ffffffffffffffffffffffffffffffff.tdj");

        let lines: Vec<String> = fs::read_to_string(second.directory.join("recording-events.jsonl"))?
            .lines()
            .map(str::to_owned)
            .collect();
        assert_eq!(lines.len(), 1);
        let ev: serde_json::Value =
            serde_json::from_str(&lines[0]).map_err(|e| invalid(e.to_string()))?;
        assert_eq!(ev["event"], "sink_reattached");
        assert_eq!(ev["gap_detected"], true);
        assert_eq!(ev["previous_generation"], first.generation);
        assert_eq!(ev["new_generation"], second.generation);
        assert_eq!(ev["prior_failure_reason"], "backlog_overflow");
        Ok(())
    }
}
