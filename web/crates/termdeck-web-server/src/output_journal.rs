//! Append and fsync before terminal output is forwarded. See output_journal.py.
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

const MAGIC: &[u8] = b"T1000J1\n";
const MAX_RECORD: usize = 8 * 1024 * 1024 + 4;
const NOFOLLOW: i32 = 0x20000;

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
        fs::rename(&temporary, path)?;
        File::open(
            path.parent()
                .ok_or_else(|| invalid("missing recording directory"))?,
        )?
        .sync_all()
    })();
    let _cleanup = fs::remove_file(temporary);
    result
}

pub(crate) struct Journal {
    file: File,
    backup_lock: File,
    directory: PathBuf,
    generation: String,
    failed: bool,
}
impl Journal {
    pub(crate) fn new(root: &Path, id: &str) -> io::Result<Self> {
        let directory = tab_directory(root, id)?;
        let backup_lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(NOFOLLOW)
            .open(root.join(".journal-copy.lock"))?;
        let meta = backup_lock.metadata()?;
        if !meta.is_file()
            || meta.nlink() != 1
            || meta.uid() != uid()?
            || meta.permissions().mode() & 0o777 != 0o600
        {
            return Err(invalid("unsafe recording backup lock"));
        }
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
            backup_lock,
            directory,
            generation,
            failed: false,
        })
    }
    fn append(&mut self, data: &[u8], geometry: Option<(u16, u16)>) -> io::Result<()> {
        if self.failed {
            return Err(invalid("terminal recording previously failed"));
        }
        // Independent tabs flush concurrently; the SSD copier takes this
        // dedicated lock exclusively across data and commit sidecars.
        fs4::fs_std::FileExt::lock_shared(&self.backup_lock)?;
        let result = self.append_inner(data, geometry);
        let unlocked = fs4::fs_std::FileExt::unlock(&self.backup_lock);
        let result = result.and(unlocked);
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
        publish(
            &self.directory.join("latest-view"),
            self.generation.as_bytes(),
            &self.generation,
        )
    }
}

pub(crate) type SharedJournal = Arc<Mutex<Journal>>;
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
    let mut file = read_file(&directory.join(&generation))?;
    let mut magic = [0; 8];
    file.read_exact(&mut magic)?;
    let size = file.metadata()?.len();
    let committed = watermark
        .parse::<u64>()
        .map_err(|_| invalid("invalid recording commit boundary"))?;
    if magic != MAGIC || offset > committed || committed > size || committed < 8 {
        return Err(invalid("invalid recording header or position"));
    }
    file.seek(SeekFrom::Start(offset))?;
    let mut page = Page {
        generation,
        generations: vec![],
        next_offset: offset,
        complete: false,
        truncated_tail: size > committed,
        events: vec![],
    };
    let mut total = 0;
    while total < 256 * 1024 {
        let start = file.stream_position()?;
        if start == committed {
            page.complete = true;
            break;
        }
        if committed - start < 5 {
            return Err(invalid("corrupt recording commit boundary"));
        }
        let mut header = [0; 5];
        let count = file.read(&mut header)?;
        if count == 0 {
            page.complete = true;
            break;
        }
        if let Err(error) = file.read_exact(&mut header[count..]) {
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
        if let Err(error) = file
            .read_exact(&mut body[1..])
            .and_then(|()| file.read_exact(&mut digest))
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
        page.next_offset = file.stream_position()?;
    }
    // Bounded memory; explicit generation IDs remain usable even if older.
    let mut names = std::collections::BTreeSet::new();
    for item in fs::read_dir(&directory)? {
        let name = item?.file_name().to_string_lossy().into_owned();
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
}
