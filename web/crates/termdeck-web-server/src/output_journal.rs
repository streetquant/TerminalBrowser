//! Durable terminal output journal with bounded async coalescing for web streams. See output_journal.py.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc as std_mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::oneshot;

const MAGIC: &[u8] = b"T1000J1\n";
const MAX_RECORD: usize = 8 * 1024 * 1024 + 4;
const NOFOLLOW: i32 = 0x20000;
const MAX_JOURNAL_QUEUE_BYTES: usize = 4 * 1024 * 1024;
const COALESCE_WINDOW: Duration = Duration::from_millis(50);
const MAX_COALESCE_BATCH_BYTES: usize = 256 * 1024;
const FINISH_TIMEOUT: Duration = Duration::from_secs(2);

fn invalid(message: &str) -> io::Error {
    io::Error::other(message)
}
fn uid() -> io::Result<u32> {
    Ok(fs::metadata("/proc/self")?.uid())
}
fn directory(path: &Path) -> io::Result<()> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => {
            if let Some(parent) = path.parent() {
                File::open(parent)?.sync_all()?;
            }
        }
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error),
    }
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir()
        || meta.uid() != uid()?
        || meta.permissions().mode() & 0o777 != 0o700
        || fs::canonicalize(path)? != path
    {
        return Err(invalid("unsafe terminal recording directory"));
    }
    Ok(())
}
fn tab_directory(root: &Path, id: &str) -> io::Result<PathBuf> {
    if id.is_empty()
        || id.len() > 128
        || !id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        return Err(invalid("invalid terminal recording identity"));
    }
    directory(root)?;
    let journals = root.join("journals");
    directory(&journals)?;
    let tab = journals.join(id);
    directory(&tab)?;
    Ok(tab)
}
fn generation_valid(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 57
        && bytes[..20].iter().all(u8::is_ascii_digit)
        && bytes[20] == b'-'
        && bytes[21..53]
            .iter()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
        && &bytes[53..] == b".tdj"
}

#[derive(Deserialize)]
struct PipeOwnerMeta {
    session_id: String,
    pid: u32,
    starttime: u64,
    server_pid: u32,
    server_start: u64,
}

fn proc_stat_ppid_start(pid: u32) -> Option<(u32, u64)> {
    let text = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let (_, rest_text) = text.rsplit_once(')')?;
    let rest: Vec<&str> = rest_text.split_whitespace().collect();
    if rest.len() < 20 {
        return None;
    }
    let ppid = rest[1].parse::<u32>().ok()?;
    let starttime = rest[19].parse::<u64>().ok()?;
    Some((ppid, starttime))
}

/// Returns true if a verified primary pipe-pane recorder is actively capturing `id`.
pub(crate) fn primary_pipe_sink_alive(root: &Path, id: &str) -> bool {
    let Ok(directory) = tab_directory(root, id) else {
        return false;
    };
    if read_file(&directory.join("latest")).is_err() {
        return false;
    }
    let Ok(owner_file) = read_file(&directory.join("pipe-owner.json")) else {
        return false;
    };
    let mut raw = Vec::new();
    if owner_file.take(16_385).read_to_end(&mut raw).is_err() || raw.len() > 16_384 {
        return false;
    }
    let Ok(owner) = serde_json::from_slice::<PipeOwnerMeta>(&raw) else {
        return false;
    };
    if owner.session_id != id {
        return false;
    }
    let Ok(expected_uid) = uid() else {
        return false;
    };
    let Ok(proc_meta) = fs::metadata(format!("/proc/{}", owner.pid)) else {
        return false;
    };
    if proc_meta.uid() != expected_uid {
        return false;
    }
    let Some((ppid, starttime)) = proc_stat_ppid_start(owner.pid) else {
        return false;
    };
    let Some((_, server_start)) = proc_stat_ppid_start(owner.server_pid) else {
        return false;
    };
    ppid == owner.server_pid && starttime == owner.starttime && server_start == owner.server_start
}
fn hex(data: &[u8]) -> String {
    const DIGITS: &[u8] = b"0123456789abcdef";
    let mut result = String::with_capacity(data.len() * 2);
    for byte in data {
        result.push(char::from(DIGITS[usize::from(byte >> 4)]));
        result.push(char::from(DIGITS[usize::from(byte & 15)]));
    }
    result
}
fn read_file(path: &Path) -> io::Result<File> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(NOFOLLOW | 0x800)
        .open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file()
        || meta.nlink() > 1
        || meta.uid() != uid()?
        || meta.permissions().mode() & 0o777 != 0o600
    {
        return Err(invalid("unsafe terminal recording file"));
    }
    Ok(file)
}

fn publish(path: &Path, content: &[u8], generation: &str) -> io::Result<()> {
    let temporary = path.with_file_name(format!(
        ".{}-{generation}",
        path.file_name()
            .ok_or_else(|| invalid("missing recording filename"))?
            .to_string_lossy()
    ));
    let result = (|| {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(NOFOLLOW)
            .open(&temporary)?;
        file.write_all(content)?;
        file.sync_all()?;
        fs::rename(&temporary, path)
    })();
    let _cleanup = fs::remove_file(temporary);
    result
}

pub(crate) struct Journal {
    file: File,
    directory: PathBuf,
    generation: String,
    published_initial_commit: bool,
    failed: bool,
}
impl Journal {
    pub(crate) fn new(root: &Path, id: &str) -> io::Result<Self> {
        let directory = tab_directory(root, id)?;
        let mut random = [0_u8; 16];
        getrandom::fill(&mut random).map_err(|_| invalid("recording randomness unavailable"))?;
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| invalid("invalid clock"))?
            .as_millis();
        let generation = format!("{millis:020}-{}.tdj", hex(&random));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(NOFOLLOW)
            .open(directory.join(&generation))?;
        file.write_all(MAGIC)?;
        file.sync_all()?;
        File::open(&directory)?.sync_all()?;
        Ok(Self {
            file,
            directory,
            generation,
            published_initial_commit: false,
            failed: false,
        })
    }
    fn append(&mut self, data: &[u8], geometry: Option<(u16, u16)>) -> io::Result<()> {
        if self.failed {
            return Err(invalid("terminal recording previously failed"));
        }
        // Each writer owns an append-only generation. The SSD mirror reads the
        // atomic watermark first and validates that durable prefix, even while
        // more bytes are appended to this generation.
        let result = self.append_inner(data, geometry);
        if result.is_err() {
            self.failed = true;
        }
        result
    }
    fn append_inner(&mut self, data: &[u8], geometry: Option<(u16, u16)>) -> io::Result<()> {
        let mut body = Vec::with_capacity(data.len() + 5);
        body.push(u8::from(geometry.is_none()));
        if let Some((cols, rows)) = geometry {
            if !(2..=1000).contains(&cols) || !(2..=500).contains(&rows) {
                return Err(invalid("invalid recording geometry"));
            }
            body.extend_from_slice(&cols.to_be_bytes());
            body.extend_from_slice(&rows.to_be_bytes());
        }
        body.extend_from_slice(data);
        if body.len() - 1 > MAX_RECORD {
            return Err(invalid("recording frame too large"));
        }
        let length =
            u32::try_from(body.len() - 1).map_err(|_| invalid("recording frame too large"))?;
        self.file.write_all(&length.to_be_bytes())?;
        self.file.write_all(&body)?;
        self.file.write_all(&Sha256::digest(&body))?;
        self.file.sync_all()?;
        let committed = self.file.stream_position()?;
        publish(
            &self.directory.join(format!("{}.commit", self.generation)),
            committed.to_string().as_bytes(),
            &self.generation,
        )?;
        let directory = File::open(&self.directory)?;
        if !self.published_initial_commit {
            // A new latest-view must always reference a generation with a
            // durable watermark.
            directory.sync_all()?;
        }
        let pointer_path = self.directory.join("latest-view");
        let pointer_current = self.published_initial_commit
            && read_file(&pointer_path).is_ok_and(|pointer| {
                let mut value = String::new();
                pointer.take(128).read_to_string(&mut value).is_ok()
                    && value.as_bytes() == self.generation.as_bytes()
            });
        if !pointer_current {
            publish(
                &pointer_path,
                self.generation.as_bytes(),
                &self.generation,
            )?;
            directory.sync_all()?;
        }
        self.published_initial_commit = true;
        Ok(())
    }
}

enum AsyncJournalCommand {
    Append {
        data: Vec<u8>,
        geometry: Option<(u16, u16)>,
    },
    Finish(oneshot::Sender<io::Result<()>>),
}

/// Bounded asynchronous output journal writer that keeps disk `fsync()` latency
/// off the live WebSocket display path while still failing closed on errors or overflow.
pub(crate) struct AsyncJournal {
    tx: Option<std_mpsc::Sender<AsyncJournalCommand>>,
    queued_bytes: Arc<AtomicUsize>,
    failed: Arc<Mutex<Option<String>>>,
}

impl AsyncJournal {
    pub(crate) fn spawn(root: PathBuf, id: String) -> Self {
        let (tx, rx) = std_mpsc::channel();
        let queued_bytes = Arc::new(AtomicUsize::new(0));
        let failed = Arc::new(Mutex::new(None));
        let worker_queued = Arc::clone(&queued_bytes);
        let worker_failed = Arc::clone(&failed);
        tokio::task::spawn_blocking(move || {
            async_journal_worker(root, id, rx, worker_queued, worker_failed);
        });
        Self {
            tx: Some(tx),
            queued_bytes,
            failed,
        }
    }

    pub(crate) fn enqueue(&self, bytes: &[u8], geometry: Option<(u16, u16)>) -> io::Result<()> {
        if let Some(message) = self
            .failed
            .lock()
            .map_err(|_| invalid("terminal recording state lock failed"))?
            .clone()
        {
            return Err(invalid(&message));
        }
        let cost = bytes.len().max(1);
        let previous = self.queued_bytes.fetch_add(cost, Ordering::AcqRel);
        if previous.saturating_add(cost) > MAX_JOURNAL_QUEUE_BYTES {
            self.queued_bytes.fetch_sub(cost, Ordering::AcqRel);
            let message = String::from("terminal recording queue exceeded 4 MiB");
            record_journal_failure(&self.failed, &message);
            return Err(invalid(&message));
        }
        let Some(tx) = self.tx.as_ref() else {
            self.queued_bytes.fetch_sub(cost, Ordering::AcqRel);
            return Err(invalid("terminal recording writer closed"));
        };
        if tx
            .send(AsyncJournalCommand::Append {
                data: bytes.to_vec(),
                geometry,
            })
            .is_err()
        {
            self.queued_bytes.fetch_sub(cost, Ordering::AcqRel);
            let message = self
                .failed
                .lock()
                .ok()
                .and_then(|guard| guard.clone())
                .unwrap_or_else(|| String::from("terminal recording writer exited"));
            record_journal_failure(&self.failed, &message);
            return Err(invalid(&message));
        }
        Ok(())
    }

    pub(crate) async fn finish(&mut self) {
        let Some(tx) = self.tx.take() else {
            return;
        };
        let (reply_tx, reply_rx) = oneshot::channel();
        if tx.send(AsyncJournalCommand::Finish(reply_tx)).is_ok() {
            let _result = tokio::time::timeout(FINISH_TIMEOUT, reply_rx).await;
        }
    }
}

fn record_journal_failure(slot: &Arc<Mutex<Option<String>>>, message: &str) {
    if let Ok(mut guard) = slot.lock() {
        if guard.is_none() {
            *guard = Some(message.to_owned());
        }
    }
}

fn async_journal_worker(
    root: PathBuf,
    id: String,
    rx: std_mpsc::Receiver<AsyncJournalCommand>,
    queued_bytes: Arc<AtomicUsize>,
    failed: Arc<Mutex<Option<String>>>,
) {
    let mut journal = match Journal::new(&root, &id) {
        Ok(value) => value,
        Err(error) => {
            let message = format!("terminal recording initialization failed: {error}");
            record_journal_failure(&failed, &message);
            while let Ok(command) = rx.try_recv() {
                if let AsyncJournalCommand::Finish(reply) = command {
                    let _sent = reply.send(Err(invalid(&message)));
                }
            }
            return;
        }
    };

    let mut pending: Option<AsyncJournalCommand> = None;
    loop {
        let command = match pending.take() {
            Some(value) => value,
            None => match rx.recv() {
                Ok(value) => value,
                Err(_) => break,
            },
        };
        match command {
            AsyncJournalCommand::Finish(reply) => {
                let _sent = reply.send(Ok(()));
                break;
            }
            AsyncJournalCommand::Append { data, geometry } => {
                queued_bytes.fetch_sub(data.len().max(1), Ordering::AcqRel);
                let result = if geometry.is_some() {
                    journal.append(&data, geometry)
                } else {
                    let mut batch = data;
                    let mut finish_reply = None;
                    let deadline = Instant::now() + COALESCE_WINDOW;
                    while batch.len() < MAX_COALESCE_BATCH_BYTES {
                        let remaining = deadline.saturating_duration_since(Instant::now());
                        if remaining.is_zero() {
                            break;
                        }
                        match rx.recv_timeout(remaining) {
                            Ok(AsyncJournalCommand::Append {
                                data: next,
                                geometry: None,
                            }) if batch.len().saturating_add(next.len())
                                <= MAX_COALESCE_BATCH_BYTES =>
                            {
                                queued_bytes.fetch_sub(next.len().max(1), Ordering::AcqRel);
                                batch.extend_from_slice(&next);
                            }
                            Ok(other) => {
                                match other {
                                    AsyncJournalCommand::Finish(reply) => {
                                        finish_reply = Some(reply);
                                    }
                                    item => pending = Some(item),
                                }
                                break;
                            }
                            Err(std_mpsc::RecvTimeoutError::Timeout) => break,
                            Err(std_mpsc::RecvTimeoutError::Disconnected) => break,
                        }
                    }
                    let append_result = journal.append(&batch, None);
                    if let Some(reply) = finish_reply {
                        let status = match &append_result {
                            Ok(()) => Ok(()),
                            Err(error) => Err(invalid(&error.to_string())),
                        };
                        let _sent = reply.send(status);
                        if append_result.is_ok() {
                            return;
                        }
                    }
                    append_result
                };
                if let Err(error) = result {
                    let message = format!("terminal recording failed: {error}");
                    record_journal_failure(&failed, &message);
                    if let Some(AsyncJournalCommand::Finish(reply)) = pending.take() {
                        let _sent = reply.send(Err(invalid(&message)));
                    }
                    while let Ok(next) = rx.try_recv() {
                        if let AsyncJournalCommand::Finish(reply) = next {
                            let _sent = reply.send(Err(invalid(&message)));
                        }
                    }
                    break;
                }
            }
        }
    }
}

#[cfg(test)]
pub(crate) type SharedJournal = Arc<Mutex<Journal>>;
#[cfg(test)]
pub(crate) async fn append(
    journal: &SharedJournal,
    bytes: &[u8],
    geometry: Option<(u16, u16)>,
) -> io::Result<()> {
    let journal = Arc::clone(journal);
    let bytes = bytes.to_vec();
    tokio::task::spawn_blocking(move || {
        journal
            .lock()
            .map_err(|_| invalid("terminal recording lock failed"))?
            .append(&bytes, geometry)
    })
    .await
    .map_err(|_| invalid("terminal recording worker failed"))?
}

#[derive(Serialize)]
pub(crate) struct Event {
    kind: u8,
    #[serde(skip_serializing_if = "Option::is_none")]
    cols: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    rows: Option<u16>,
    data_hex: String,
}
#[derive(Serialize)]
pub(crate) struct Page {
    generation: String,
    generations: Vec<String>,
    next_offset: u64,
    complete: bool,
    truncated_tail: bool,
    events: Vec<Event>,
}
enum JournalStream {
    Plain { file: File, size: u64 },
    Zstd { stdout: ChildStdout, child: Child },
}

impl Read for JournalStream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            Self::Plain { file, .. } => file.read(buf),
            Self::Zstd { stdout, .. } => stdout.read(buf),
        }
    }
}

impl Drop for JournalStream {
    fn drop(&mut self) {
        if let Self::Zstd { child, .. } = self {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn open_journal_stream(directory: &Path, generation: &str) -> io::Result<JournalStream> {
    match read_file(&directory.join(generation)) {
        Ok(file) => {
            let size = file.metadata()?.len();
            Ok(JournalStream::Plain { file, size })
        }
        Err(err) if err.kind() == io::ErrorKind::NotFound => {
            let compressed = read_file(&directory.join(format!("{generation}.zst")))?;
            let mut child = Command::new("/usr/bin/zstd")
                .args(["-dc", "-q"])
                .stdin(Stdio::from(compressed))
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()?;
            let stdout = child
                .stdout
                .take()
                .ok_or_else(|| invalid("zstd stdout unavailable"))?;
            Ok(JournalStream::Zstd { stdout, child })
        }
        Err(err) => Err(err),
    }
}

pub(crate) fn read_page(
    root: &Path,
    id: &str,
    generation: Option<String>,
    offset: u64,
) -> io::Result<Page> {
    let directory = tab_directory(root, id)?;
    let generation = match generation {
        Some(value) => value,
        None => {
            let mut value = String::new();
            let mut candidates = Vec::new();
            for name in ["latest", "latest-view"] {
                match read_file(&directory.join(name)) {
                    Ok(file) => candidates.push((file.metadata()?.modified()?, file)),
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error),
                }
            }
            let pointer = candidates
                .into_iter()
                .max_by_key(|item| item.0)
                .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no saved output"))?
                .1;
            pointer.take(128).read_to_string(&mut value)?;
            value
        }
    };
    if !generation_valid(&generation) || offset < 8 {
        return Err(invalid("invalid recording position"));
    }
    let mut watermark = String::new();
    read_file(&directory.join(format!("{generation}.commit")))?
        .take(32)
        .read_to_string(&mut watermark)?;
    let mut stream = open_journal_stream(&directory, &generation)?;
    let mut magic = [0; 8];
    stream.read_exact(&mut magic)?;
    let committed = watermark
        .parse::<u64>()
        .map_err(|_| invalid("invalid recording commit boundary"))?;
    let (truncated_tail, mut pos) = match &mut stream {
        JournalStream::Plain { file, size } => {
            if magic != MAGIC || offset > committed || committed > *size || committed < 8 {
                return Err(invalid("invalid recording header or position"));
            }
            file.seek(SeekFrom::Start(offset))?;
            (*size > committed, offset)
        }
        JournalStream::Zstd { stdout, .. } => {
            if magic != MAGIC || offset > committed || committed < 8 {
                return Err(invalid("invalid recording header or position"));
            }
            let mut skip = offset - 8;
            let mut scratch = [0_u8; 8192];
            while skip > 0 {
                let want = usize::try_from(skip.min(scratch.len() as u64)).unwrap_or(scratch.len());
                let n = stdout.read(&mut scratch[..want])?;
                if n == 0 {
                    return Err(invalid("truncated compressed recording"));
                }
                skip -= n as u64;
            }
            (false, offset)
        }
    };
    let mut page = Page {
        generation,
        generations: vec![],
        next_offset: offset,
        complete: false,
        truncated_tail,
        events: vec![],
    };
    let mut total = 0;
    while total < 256 * 1024 {
        let start = pos;
        if start == committed {
            page.complete = true;
            break;
        }
        if committed - start < 5 {
            return Err(invalid("corrupt recording commit boundary"));
        }
        let mut header = [0; 5];
        let count = stream.read(&mut header)?;
        if count == 0 {
            page.complete = true;
            break;
        }
        if let Err(error) = stream.read_exact(&mut header[count..]) {
            if error.kind() != io::ErrorKind::UnexpectedEof {
                return Err(error);
            }
            page.complete = true;
            page.truncated_tail = true;
            break;
        }
        let length = u32::from_be_bytes([header[0], header[1], header[2], header[3]]) as usize;
        let kind = header[4];
        if length > MAX_RECORD || kind > 2 || (kind != 1 && length < 4) {
            return Err(invalid("corrupt recording frame"));
        }
        if start + 5 + length as u64 + 32 > committed {
            return Err(invalid("corrupt recording commit boundary"));
        }
        let mut body = vec![0; length + 1];
        body[0] = kind;
        let mut digest = [0; 32];
        if let Err(error) = stream
            .read_exact(&mut body[1..])
            .and_then(|()| stream.read_exact(&mut digest))
        {
            if error.kind() != io::ErrorKind::UnexpectedEof {
                return Err(error);
            }
            page.complete = true;
            page.truncated_tail = true;
            break;
        }
        if Sha256::digest(&body)[..] != digest {
            return Err(invalid("terminal recording checksum mismatch"));
        }
        let (cols, rows, start) = if kind != 1 {
            let cols = u16::from_be_bytes([body[1], body[2]]);
            let rows = u16::from_be_bytes([body[3], body[4]]);
            if !(2..=1000).contains(&cols) || !(2..=500).contains(&rows) {
                return Err(invalid("corrupt recording geometry"));
            }
            (Some(cols), Some(rows), 5)
        } else {
            (None, None, 1)
        };
        page.events.push(Event {
            kind,
            cols,
            rows,
            data_hex: hex(&body[start..]),
        });
        total += length;
        pos += 5 + length as u64 + 32;
        page.next_offset = pos;
    }
    // Bounded memory; explicit generation IDs remain usable even if older.
    let mut names = std::collections::BTreeSet::new();
    for item in fs::read_dir(&directory)? {
        let raw_name = item?.file_name().to_string_lossy().into_owned();
        let name = raw_name
            .strip_suffix(".zst")
            .map_or(raw_name.as_str(), |base| base)
            .to_owned();
        if generation_valid(&name) {
            names.insert(name);
            if names.len() > 64 {
                names.pop_first();
            }
        }
    }
    page.generations = names.into_iter().rev().collect();
    Ok(page)
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> io::Result<Self> {
            let mut random = [0; 16];
            getrandom::fill(&mut random).map_err(|_| invalid("test random failed"))?;
            let path = std::env::temp_dir().join(format!("termdeck-journal-{}", hex(&random)));
            directory(&path)?;
            Ok(Self(path))
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _cleanup = fs::remove_dir_all(&self.0);
        }
    }
    #[test]
    fn committed_generations_retain_reset_and_exact_bytes() -> io::Result<()> {
        let fixture = Fixture::new()?;
        let mut first = Journal::new(&fixture.0, "tab")?;
        first.append(b"old screen", Some((91, 31)))?;
        first.append(b"\x00\xff\r\n", None)?;
        publish(
            &first.directory.join("latest"),
            first.generation.as_bytes(),
            &first.generation,
        )?;
        File::open(&first.directory)?.sync_all()?;
        let mut second = Journal::new(&fixture.0, "tab")?;
        second.append(b"new shell", Some((80, 24)))?;
        let old = read_page(&fixture.0, "tab", Some(first.generation.clone()), 8)?;
        assert!(old.complete);
        assert_eq!(old.events.len(), 2);
        assert_eq!(old.events[0].cols, Some(91));
        assert_eq!(old.events[1].data_hex, "00ff0d0a");
        let new = read_page(&fixture.0, "tab", None, 8)?;
        assert_eq!(new.generation, second.generation);
        assert_eq!(new.generations.len(), 2);
        assert_eq!(
            fs::read_to_string(first.directory.join("latest"))?,
            first.generation
        );
        Ok(())
    }
    #[test]
    fn uncommitted_tail_is_never_returned_and_corruption_is_rejected() -> io::Result<()> {
        let fixture = Fixture::new()?;
        let mut journal = Journal::new(&fixture.0, "tab")?;
        journal.append(b"durable", None)?;
        journal.file.write_all(&[0, 0, 0, 50, 1, 65])?;
        let page = read_page(&fixture.0, "tab", None, 8)?;
        assert!(page.truncated_tail);
        assert_eq!(page.events.len(), 1);
        journal.file.seek(SeekFrom::Start(13))?;
        journal.file.write_all(b"X")?;
        assert!(read_page(&fixture.0, "tab", None, 8).is_err());
        Ok(())
    }
    #[test]
    fn failed_publication_poisons_writer_and_rejects_path_escape() -> io::Result<()> {
        let fixture = Fixture::new()?;
        let mut journal = Journal::new(&fixture.0, "tab")?;
        journal.append(b"first", None)?;
        fs::remove_file(journal.directory.join("latest-view"))?;
        fs::create_dir(journal.directory.join("latest-view"))?;
        assert!(journal.append(b"not forwarded", None).is_err());
        assert!(journal.append(b"cannot retry silently", None).is_err());
        assert!(Journal::new(&fixture.0, "../escape").is_err());
        assert!(read_page(&fixture.0, "tab", Some("../../secret".into()), 8).is_err());
        Ok(())
    }

    #[test]
    fn async_journal_coalesces_bursts_and_drains_on_finish() -> io::Result<()> {
        let fixture = Fixture::new()?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        runtime.block_on(async {
            let mut journal = AsyncJournal::spawn(fixture.0.clone(), "async-tab".to_owned());
            journal.enqueue(b"reset", Some((80, 24)))?;
            for index in 0..50_usize {
                journal.enqueue(format!("chunk-{index}-").as_bytes(), None)?;
            }
            journal.finish().await;
            let page = read_page(&fixture.0, "async-tab", None, 8)?;
            assert!(page.complete);
            assert!(!page.truncated_tail);
            assert!(page.events.len() < 51, "burst should coalesce frames");
            let mut combined = Vec::new();
            for event in &page.events {
                combined.extend_from_slice(&decode_hex(&event.data_hex));
            }
            let mut expected = b"reset".to_vec();
            for index in 0..50_usize {
                expected.extend_from_slice(format!("chunk-{index}-").as_bytes());
            }
            assert_eq!(combined, expected);
            Ok::<(), io::Error>(())
        })
    }

    fn decode_hex(hex: &str) -> Vec<u8> {
        let mut out = Vec::with_capacity(hex.len() / 2);
        let bytes = hex.as_bytes();
        let mut index = 0;
        while index + 1 < bytes.len() {
            let pair = std::str::from_utf8(&bytes[index..index + 2]).unwrap_or("00");
            out.push(u8::from_str_radix(pair, 16).unwrap_or(0));
            index += 2;
        }
        out
    }

    #[test]
    fn async_journal_fails_closed_on_queue_overflow() -> io::Result<()> {
        let fixture = Fixture::new()?;
        let mut journal = AsyncJournal {
            tx: None,
            queued_bytes: Arc::new(AtomicUsize::new(MAX_JOURNAL_QUEUE_BYTES)),
            failed: Arc::new(Mutex::new(None)),
        };
        assert!(journal.enqueue(b"overflow", None).is_err());
        assert!(journal.enqueue(b"poisoned", None).is_err());
        let _unused = &fixture;
        drop(journal.tx.take());
        Ok(())
    }

    #[test]
    fn primary_pipe_sink_alive_verifies_live_owner_and_rejects_missing_or_stale() -> io::Result<()> {
        let fixture = Fixture::new()?;
        assert!(!primary_pipe_sink_alive(&fixture.0, "tab-x"));
        let mut journal = Journal::new(&fixture.0, "tab-x")?;
        journal.append(b"initial", Some((80, 24)))?;
        let pid = std::process::id();
        let (ppid, starttime) = proc_stat_ppid_start(pid)
            .ok_or_else(|| invalid("self stat unavailable"))?;
        let (_, server_start) = proc_stat_ppid_start(ppid)
            .ok_or_else(|| invalid("parent stat unavailable"))?;
        let owner = serde_json::json!({
            "session_id": "tab-x",
            "pid": pid,
            "starttime": starttime,
            "server_pid": ppid,
            "server_start": server_start,
        });
        publish(
            &journal.directory.join("pipe-owner.json"),
            owner.to_string().as_bytes(),
            "owner-missing-latest",
        )?;
        // No `latest` pointer yet -> must still return false.
        assert!(!primary_pipe_sink_alive(&fixture.0, "tab-x"));

        publish(
            &journal.directory.join("latest"),
            journal.generation.as_bytes(),
            "latest",
        )?;
        assert!(primary_pipe_sink_alive(&fixture.0, "tab-x"));

        let stale_owner = serde_json::json!({
            "session_id": "tab-x",
            "pid": pid,
            "starttime": starttime.wrapping_add(1),
            "server_pid": ppid,
            "server_start": server_start,
        });
        publish(
            &journal.directory.join("pipe-owner.json"),
            stale_owner.to_string().as_bytes(),
            "owner-stale",
        )?;
        assert!(!primary_pipe_sink_alive(&fixture.0, "tab-x"));
        Ok(())
    }

    #[test]
    fn read_page_supports_zstd_archived_generations() -> io::Result<()> {
        let fixture = Fixture::new()?;
        let mut journal = Journal::new(&fixture.0, "tab-zst")?;
        journal.append(b"archived-hello", Some((90, 30)))?;
        journal.append(b"-world", None)?;
        let tdj_path = journal.directory.join(&journal.generation);
        let zst_path = journal.directory.join(format!("{}.zst", journal.generation));
        let zst_out = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&zst_path)?;
        let tdj_in = File::open(&tdj_path)?;
        let status = Command::new("/usr/bin/zstd")
            .args(["-3", "-q"])
            .stdin(Stdio::from(tdj_in))
            .stdout(Stdio::from(zst_out))
            .status()?;
        assert!(status.success());
        fs::remove_file(&tdj_path)?;

        let page = read_page(&fixture.0, "tab-zst", Some(journal.generation.clone()), 8)?;
        assert!(page.complete && !page.truncated_tail);
        assert_eq!(page.events.len(), 2);
        assert!(page.generations.contains(&journal.generation));
        assert_eq!(decode_hex(&page.events[0].data_hex), b"archived-hello");
        assert_eq!(decode_hex(&page.events[1].data_hex), b"-world");
        Ok(())
    }
}
