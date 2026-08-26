#![forbid(unsafe_code)]

//! Strict read-only projection of the existing TermDeck SQLite registry.
//!
//! Production code in this crate never creates, migrates, or updates a database.
//! Every refresh opens the canonical registry with SQLite read-only flags, enables
//! `query_only`, validates the required schema, and builds bounded immutable domain
//! snapshots. Optional tmux inventory is supplied by the server through a separate
//! read-only command path.

use rusqlite::{Connection, OpenFlags, TransactionBehavior};
use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;
use termdeck_web_core::{
    HostId, HostSnapshot, IdError, MAX_CWD_BYTES, OwnerId, PaneId, PaneSnapshot, PaneStatus,
    RouteError, RouteRegistry, SessionId, SnapshotError, WindowId, WindowRouteInput,
    WindowSnapshot,
};

pub const DEFAULT_DATABASE_BUSY_TIMEOUT_MS: u64 = 1_000;
pub const MAX_DATABASE_ROWS: usize = 16_384;
pub const MAX_TMUX_SESSION_NAME_BYTES: usize = 512;

const REQUIRED_SESSION_COLUMNS: &[&str] = &[
    "id",
    "name",
    "cwd",
    "tmux_name",
    "status",
    "window_id",
    "window_title",
    "tab_index",
    "visible",
    "pinned",
    "pane_target",
    "last_opened_at",
];
const REQUIRED_WINDOW_COLUMNS: &[&str] = &["window_id", "window_title"];

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TmuxPaneState {
    pane_id: PaneId,
    current_path: String,
    dead: bool,
}

impl TmuxPaneState {
    pub fn new(
        pane_id: PaneId,
        current_path: impl Into<String>,
        dead: bool,
    ) -> Result<Self, StateError> {
        let current_path = current_path.into();
        validate_bounded_text("tmux current path", &current_path, MAX_CWD_BYTES)?;
        Ok(Self {
            pane_id,
            current_path,
            dead,
        })
    }

    #[must_use]
    pub fn pane_id(&self) -> &PaneId {
        &self.pane_id
    }

    #[must_use]
    pub fn current_path(&self) -> &str {
        &self.current_path
    }

    #[must_use]
    pub fn dead(&self) -> bool {
        self.dead
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct TmuxInventory {
    complete: bool,
    by_session: BTreeMap<String, TmuxPaneState>,
}

impl TmuxInventory {
    #[must_use]
    pub fn unavailable() -> Self {
        Self::default()
    }

    #[must_use]
    pub fn complete() -> Self {
        Self {
            complete: true,
            by_session: BTreeMap::new(),
        }
    }

    pub fn insert(
        &mut self,
        session_name: impl Into<String>,
        pane: TmuxPaneState,
    ) -> Result<(), StateError> {
        let session_name = session_name.into();
        validate_bounded_text(
            "tmux session name",
            &session_name,
            MAX_TMUX_SESSION_NAME_BYTES,
        )?;
        if self.by_session.insert(session_name.clone(), pane).is_some() {
            return Err(StateError::DuplicateTmuxSession(session_name));
        }
        Ok(())
    }

    #[must_use]
    pub fn is_complete(&self) -> bool {
        self.complete
    }

    #[must_use]
    pub fn get(&self, session_name: &str) -> Option<&TmuxPaneState> {
        self.by_session.get(session_name)
    }

    #[must_use]
    pub fn len(&self) -> usize {
        self.by_session.len()
    }

    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.by_session.is_empty()
    }
}

#[derive(Debug)]
pub enum StateError {
    Io(std::io::Error),
    Database(rusqlite::Error),
    Id(IdError),
    Route(RouteError),
    Snapshot(SnapshotError),
    DatabasePathIsSymlink(PathBuf),
    DatabasePathIsNotFile(PathBuf),
    ReadOnlyContractUnavailable,
    MissingTable(&'static str),
    MissingColumn {
        table: &'static str,
        column: &'static str,
    },
    TooManyRows {
        actual: usize,
        maximum: usize,
    },
    TextTooLong {
        field: &'static str,
        actual_bytes: usize,
        maximum_bytes: usize,
    },
    ControlCharacter {
        field: &'static str,
    },
    DuplicateTmuxSession(String),
    MalformedDatabaseRow(&'static str),
}

impl fmt::Display for StateError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "state source I/O failed: {error}"),
            Self::Database(error) => write!(formatter, "state source SQLite failed: {error}"),
            Self::Id(error) => write!(formatter, "state source identifier failed: {error}"),
            Self::Route(error) => write!(formatter, "state source route failed: {error}"),
            Self::Snapshot(error) => write!(formatter, "state source snapshot failed: {error}"),
            Self::DatabasePathIsSymlink(path) => {
                write!(formatter, "database path is a symlink: {}", path.display())
            }
            Self::DatabasePathIsNotFile(path) => {
                write!(
                    formatter,
                    "database path is not a regular file: {}",
                    path.display()
                )
            }
            Self::ReadOnlyContractUnavailable => {
                formatter.write_str("SQLite query_only contract could not be established")
            }
            Self::MissingTable(table) => {
                write!(formatter, "required SQLite table is missing: {table}")
            }
            Self::MissingColumn { table, column } => {
                write!(
                    formatter,
                    "required SQLite column is missing: {table}.{column}"
                )
            }
            Self::TooManyRows { actual, maximum } => {
                write!(
                    formatter,
                    "database returned {actual} pane rows; maximum is {maximum}"
                )
            }
            Self::TextTooLong {
                field,
                actual_bytes,
                maximum_bytes,
            } => write!(
                formatter,
                "{field} is {actual_bytes} bytes; maximum is {maximum_bytes}"
            ),
            Self::ControlCharacter { field } => {
                write!(formatter, "{field} contains a control character")
            }
            Self::DuplicateTmuxSession(session) => {
                write!(
                    formatter,
                    "tmux inventory contains duplicate session {session}"
                )
            }
            Self::MalformedDatabaseRow(field) => {
                write!(
                    formatter,
                    "database row has inconsistent nullable field {field}"
                )
            }
        }
    }
}

impl Error for StateError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::Database(error) => Some(error),
            Self::Id(error) => Some(error),
            Self::Route(error) => Some(error),
            Self::Snapshot(error) => Some(error),
            _ => None,
        }
    }
}

impl From<std::io::Error> for StateError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<rusqlite::Error> for StateError {
    fn from(error: rusqlite::Error) -> Self {
        Self::Database(error)
    }
}

impl From<IdError> for StateError {
    fn from(error: IdError) -> Self {
        Self::Id(error)
    }
}

impl From<RouteError> for StateError {
    fn from(error: RouteError) -> Self {
        Self::Route(error)
    }
}

impl From<SnapshotError> for StateError {
    fn from(error: SnapshotError) -> Self {
        Self::Snapshot(error)
    }
}

#[derive(Clone, Debug)]
struct DatabasePane {
    id: String,
    name: String,
    cwd: String,
    tmux_name: String,
    status: String,
    tab_index: i64,
    pinned: bool,
    pane_target: Option<String>,
    last_opened_at: String,
}

#[derive(Clone, Debug)]
struct DatabaseWindow {
    id: String,
    name: String,
    panes: Vec<DatabasePane>,
}

#[derive(Debug)]
pub struct ReadOnlyStateMirror {
    database_path: PathBuf,
    host_id: HostId,
    owner_id: OwnerId,
    routes: RouteRegistry,
}

impl ReadOnlyStateMirror {
    pub fn new(
        database_path: impl AsRef<Path>,
        host_id: HostId,
        owner_id: OwnerId,
    ) -> Result<Self, StateError> {
        let database_path = canonical_database_path(database_path.as_ref())?;
        Ok(Self {
            database_path,
            host_id,
            owner_id,
            routes: RouteRegistry::new(),
        })
    }

    #[must_use]
    pub fn database_path(&self) -> &Path {
        &self.database_path
    }

    pub fn load(
        &mut self,
        inventory: &TmuxInventory,
        generated_at_unix_ms: u64,
    ) -> Result<HostSnapshot, StateError> {
        let windows = load_database_windows(&self.database_path)?;
        let route_inputs: Vec<WindowRouteInput> = windows
            .iter()
            .map(|window| {
                Ok(WindowRouteInput::new(
                    WindowId::new(window.id.clone())?,
                    window.name.clone(),
                ))
            })
            .collect::<Result<_, StateError>>()?;
        let routes = self.routes.reconcile(&route_inputs)?;
        let mut projected = Vec::with_capacity(windows.len());
        for (window, route) in windows.into_iter().zip(routes) {
            let window_id = WindowId::new(window.id)?;
            let mut panes = Vec::with_capacity(window.panes.len());
            for pane in window.panes {
                let live = inventory.get(&pane.tmux_name);
                let cwd = live
                    .map(TmuxPaneState::current_path)
                    .filter(|value| !value.is_empty() && is_safe_live_cwd(value))
                    .unwrap_or(&pane.cwd)
                    .to_owned();
                let target = pane
                    .pane_target
                    .filter(|value| !value.is_empty())
                    .or_else(|| Some(format!("{}:0.0", pane.tmux_name)));
                panes.push(PaneSnapshot::new(
                    PaneId::new(pane.id.clone())?,
                    Some(SessionId::new(pane.id)?),
                    pane.name,
                    cwd,
                    project_status(&pane.status, live, inventory.is_complete()),
                    target,
                    None,
                )?);
            }
            projected.push(WindowSnapshot::new(
                window_id,
                window.name,
                route,
                panes,
                None,
            )?);
        }
        let revision = stable_revision(&self.host_id, &self.owner_id, &projected, inventory);
        Ok(HostSnapshot::new(
            self.host_id.clone(),
            self.owner_id.clone(),
            revision,
            generated_at_unix_ms,
            projected,
        )?)
    }
}

fn canonical_database_path(path: &Path) -> Result<PathBuf, StateError> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let metadata = fs::symlink_metadata(&absolute)?;
    if metadata.file_type().is_symlink() {
        return Err(StateError::DatabasePathIsSymlink(absolute));
    }
    if !metadata.is_file() {
        return Err(StateError::DatabasePathIsNotFile(absolute));
    }
    Ok(absolute.canonicalize()?)
}

fn open_read_only(path: &Path) -> Result<Connection, StateError> {
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    connection.busy_timeout(Duration::from_millis(DEFAULT_DATABASE_BUSY_TIMEOUT_MS))?;
    connection.pragma_update(None, "query_only", "ON")?;
    connection.pragma_update(None, "trusted_schema", "OFF")?;
    let query_only: i64 = connection.pragma_query_value(None, "query_only", |row| row.get(0))?;
    if query_only != 1 {
        return Err(StateError::ReadOnlyContractUnavailable);
    }
    Ok(connection)
}

fn load_database_windows(path: &Path) -> Result<Vec<DatabaseWindow>, StateError> {
    let mut connection = open_read_only(path)?;
    verify_schema(&connection)?;
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Deferred)?;

    let mut windows: BTreeMap<String, DatabaseWindow> = BTreeMap::new();
    {
        let mut statement = transaction
            .prepare("SELECT window_id, window_title FROM remembered_windows ORDER BY window_id")?;
        let rows = statement.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        for row in rows {
            let (id, name) = row?;
            windows.insert(
                id.clone(),
                DatabaseWindow {
                    id,
                    name,
                    panes: Vec::new(),
                },
            );
        }
    }

    let mut row_count = 0usize;
    {
        let mut statement = transaction.prepare(
            "SELECT id, name, cwd, tmux_name, status, window_id, window_title, \
                    tab_index, pinned, pane_target, last_opened_at \
             FROM sessions \
             WHERE visible = 1 AND status != 'archived' AND status != 'stopped' \
             ORDER BY window_id, pinned DESC, tab_index, last_opened_at, id",
        )?;
        let rows = statement.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, i64>(7)?,
                row.get::<_, i64>(8)?,
                row.get::<_, Option<String>>(9)?,
                row.get::<_, String>(10)?,
            ))
        })?;
        for row in rows {
            row_count = row_count.saturating_add(1);
            if row_count > MAX_DATABASE_ROWS {
                return Err(StateError::TooManyRows {
                    actual: row_count,
                    maximum: MAX_DATABASE_ROWS,
                });
            }
            let (
                id,
                name,
                cwd,
                tmux_name,
                status,
                window_id,
                window_title,
                tab_index,
                pinned,
                pane_target,
                last_opened_at,
            ) = row?;
            let window = windows
                .entry(window_id.clone())
                .or_insert_with(|| DatabaseWindow {
                    id: window_id,
                    name: window_title,
                    panes: Vec::new(),
                });
            window.panes.push(DatabasePane {
                id,
                name,
                cwd,
                tmux_name,
                status,
                tab_index,
                pinned: pinned != 0,
                pane_target,
                last_opened_at,
            });
        }
    }

    transaction.rollback()?;
    let mut result: Vec<DatabaseWindow> = windows.into_values().collect();
    result.sort_by(|left, right| {
        (left.name.to_lowercase(), left.id.as_str())
            .cmp(&(right.name.to_lowercase(), right.id.as_str()))
    });
    for window in &mut result {
        window.panes.sort_by(|left, right| {
            (
                !left.pinned,
                left.tab_index,
                left.last_opened_at.as_str(),
                left.id.as_str(),
            )
                .cmp(&(
                    !right.pinned,
                    right.tab_index,
                    right.last_opened_at.as_str(),
                    right.id.as_str(),
                ))
        });
    }
    Ok(result)
}

fn verify_schema(connection: &Connection) -> Result<(), StateError> {
    verify_table(connection, "sessions", REQUIRED_SESSION_COLUMNS)?;
    verify_table(connection, "remembered_windows", REQUIRED_WINDOW_COLUMNS)?;
    Ok(())
}

fn verify_table(
    connection: &Connection,
    table: &'static str,
    required_columns: &[&'static str],
) -> Result<(), StateError> {
    let exists: i64 = connection.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?1)",
        [table],
        |row| row.get(0),
    )?;
    if exists != 1 {
        return Err(StateError::MissingTable(table));
    }
    let sql = format!("PRAGMA table_info({table})");
    let mut statement = connection.prepare(&sql)?;
    let columns = statement.query_map([], |row| row.get::<_, String>(1))?;
    let mut observed = BTreeMap::new();
    for column in columns {
        observed.insert(column?, ());
    }
    for column in required_columns {
        if !observed.contains_key(*column) {
            return Err(StateError::MissingColumn { table, column });
        }
    }
    Ok(())
}

fn project_status(
    database_status: &str,
    live: Option<&TmuxPaneState>,
    inventory_complete: bool,
) -> PaneStatus {
    if let Some(pane) = live {
        if pane.dead() {
            return PaneStatus::Dead;
        }
        return if database_status.eq_ignore_ascii_case("paused") {
            PaneStatus::Paused
        } else {
            PaneStatus::Running
        };
    }
    let normalized = database_status.trim().to_ascii_lowercase();
    if inventory_complete && matches!(normalized.as_str(), "running" | "paused" | "missing") {
        return PaneStatus::Missing;
    }
    match normalized.as_str() {
        "new" => PaneStatus::New,
        "running" => PaneStatus::Running,
        "paused" => PaneStatus::Paused,
        "missing" => PaneStatus::Missing,
        "recovery-pending" => PaneStatus::RecoveryPending,
        "start-error" => PaneStatus::StartError,
        "dead" | "killed" | "terminated" => PaneStatus::Dead,
        "stopped" => PaneStatus::Stopped,
        "archived" => PaneStatus::Archived,
        _ => PaneStatus::Unknown,
    }
}

fn is_safe_live_cwd(value: &str) -> bool {
    value.len() <= MAX_CWD_BYTES && !value.chars().any(char::is_control)
}

fn validate_bounded_text(
    field: &'static str,
    value: &str,
    maximum_bytes: usize,
) -> Result<(), StateError> {
    if value.len() > maximum_bytes {
        return Err(StateError::TextTooLong {
            field,
            actual_bytes: value.len(),
            maximum_bytes,
        });
    }
    if value.chars().any(char::is_control) {
        return Err(StateError::ControlCharacter { field });
    }
    Ok(())
}

fn stable_revision(
    host_id: &HostId,
    owner_id: &OwnerId,
    windows: &[WindowSnapshot],
    inventory: &TmuxInventory,
) -> u64 {
    let mut hash = StableHash::new();
    hash.text(host_id.as_str());
    hash.text(owner_id.as_str());
    hash.byte(u8::from(inventory.is_complete()));
    for window in windows {
        hash.text(window.id().as_str());
        hash.text(window.name());
        hash.text(window.route().slug());
        for pane in window.panes() {
            hash.text(pane.id().as_str());
            hash.text(pane.session_id().map_or("", SessionId::as_str));
            hash.text(pane.title());
            hash.text(pane.cwd());
            hash.text(pane_status_name(pane.status()));
            hash.text(pane.tmux_target().unwrap_or(""));
        }
    }
    hash.finish_nonzero()
}

fn pane_status_name(status: PaneStatus) -> &'static str {
    match status {
        PaneStatus::New => "new",
        PaneStatus::Running => "running",
        PaneStatus::Paused => "paused",
        PaneStatus::Missing => "missing",
        PaneStatus::RecoveryPending => "recovery-pending",
        PaneStatus::StartError => "start-error",
        PaneStatus::Dead => "dead",
        PaneStatus::Stopped => "stopped",
        PaneStatus::Archived => "archived",
        PaneStatus::Unknown => "unknown",
    }
}

struct StableHash(u64);

impl StableHash {
    const OFFSET: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;

    fn new() -> Self {
        Self(Self::OFFSET)
    }

    fn byte(&mut self, byte: u8) {
        self.0 ^= u64::from(byte);
        self.0 = self.0.wrapping_mul(Self::PRIME);
    }

    fn text(&mut self, value: &str) {
        for byte in value.as_bytes() {
            self.byte(*byte);
        }
        self.byte(0xff);
    }

    fn finish_nonzero(self) -> u64 {
        if self.0 == 0 { 1 } else { self.0 }
    }
}

#[cfg(test)]
mod tests {
    use super::{ReadOnlyStateMirror, StateError, TmuxInventory, TmuxPaneState, open_read_only};
    use rusqlite::{Connection, OpenFlags};
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};
    use termdeck_web_core::{HostId, OwnerId, PaneId, PaneStatus};

    static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(1);

    struct Fixture {
        root: PathBuf,
        database: PathBuf,
    }

    impl Fixture {
        fn new(valid_schema: bool) -> Self {
            let nonce = NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed);
            let root = std::env::temp_dir()
                .join(format!("termdeck-web-state-{}-{nonce}", std::process::id()));
            if let Err(error) = fs::create_dir_all(&root) {
                panic!("could not create fixture: {error}");
            }
            let database = root.join("sessions.sqlite3");
            let connection = match Connection::open_with_flags(
                &database,
                OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
            ) {
                Ok(value) => value,
                Err(error) => panic!("could not create fixture database: {error}"),
            };
            let pane_target = if valid_schema {
                ", pane_target TEXT"
            } else {
                ""
            };
            let schema = format!(
                "CREATE TABLE sessions (\
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, cwd TEXT NOT NULL, \
                    tmux_name TEXT NOT NULL, status TEXT NOT NULL, window_id TEXT NOT NULL, \
                    window_title TEXT NOT NULL, tab_index INTEGER NOT NULL, \
                    visible INTEGER NOT NULL, pinned INTEGER NOT NULL, \
                    last_opened_at TEXT NOT NULL{pane_target}\
                 );\
                 CREATE TABLE remembered_windows (\
                    window_id TEXT PRIMARY KEY, window_title TEXT NOT NULL\
                 );"
            );
            if let Err(error) = connection.execute_batch(&schema) {
                panic!("could not initialize fixture database: {error}");
            }
            if valid_schema {
                if let Err(error) = connection.execute_batch(
                    "INSERT INTO remembered_windows VALUES ('window-random', 'Random');\
                     INSERT INTO remembered_windows VALUES ('window-empty', 'Empty');\
                     INSERT INTO sessions VALUES (\
                        'session-pinned', 'Pinned', '/db/pinned', 'tmux-pinned', 'running', \
                        'window-random', 'Random', 5, 1, 1, '2026-08-26T00:00:02Z', 'tmux-pinned:0.0'\
                     );\
                     INSERT INTO sessions VALUES (\
                        'session-paused', 'Paused', '/db/paused', 'tmux-paused', 'paused', \
                        'window-random', 'Random', 0, 1, 0, '2026-08-26T00:00:01Z', NULL\
                     );\
                     INSERT INTO sessions VALUES (\
                        'session-hidden', 'Hidden', '/db/hidden', 'tmux-hidden', 'running', \
                        'window-random', 'Random', 1, 0, 0, '2026-08-26T00:00:03Z', NULL\
                     );\
                     INSERT INTO sessions VALUES (\
                        'session-stopped', 'Stopped', '/db/stopped', 'tmux-stopped', 'stopped', \
                        'window-random', 'Random', 2, 1, 0, '2026-08-26T00:00:04Z', NULL\
                     );",
                ) {
                    panic!("could not populate fixture database: {error}");
                }
            }
            drop(connection);
            Self { root, database }
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _result = fs::remove_dir_all(&self.root);
        }
    }

    fn host_id() -> HostId {
        match HostId::new("host-test") {
            Ok(value) => value,
            Err(error) => panic!("invalid test host id: {error}"),
        }
    }

    fn owner_id() -> OwnerId {
        match OwnerId::new("owner-test") {
            Ok(value) => value,
            Err(error) => panic!("invalid test owner id: {error}"),
        }
    }

    fn mirror(path: &Path) -> ReadOnlyStateMirror {
        match ReadOnlyStateMirror::new(path, host_id(), owner_id()) {
            Ok(value) => value,
            Err(error) => panic!("could not create mirror: {error}"),
        }
    }

    #[test]
    fn read_only_load_preserves_database_and_includes_empty_windows() {
        let fixture = Fixture::new(true);
        let before = match fs::read(&fixture.database) {
            Ok(value) => value,
            Err(error) => panic!("could not hash fixture before load: {error}"),
        };
        let mut inventory = TmuxInventory::complete();
        let live = match TmuxPaneState::new(
            match PaneId::new("%42") {
                Ok(value) => value,
                Err(error) => panic!("invalid pane id: {error}"),
            },
            "/live/pinned",
            false,
        ) {
            Ok(value) => value,
            Err(error) => panic!("invalid tmux state: {error}"),
        };
        if let Err(error) = inventory.insert("tmux-pinned", live) {
            panic!("could not add tmux state: {error}");
        }
        let mut state = mirror(&fixture.database);
        let snapshot = match state.load(&inventory, 100) {
            Ok(value) => value,
            Err(error) => panic!("could not load snapshot: {error}"),
        };
        let after = match fs::read(&fixture.database) {
            Ok(value) => value,
            Err(error) => panic!("could not hash fixture after load: {error}"),
        };
        assert_eq!(before, after);
        assert_eq!(snapshot.windows().len(), 2);
        assert_eq!(snapshot.windows()[0].name(), "Empty");
        assert_eq!(snapshot.windows()[0].panes().len(), 0);
        assert_eq!(snapshot.windows()[1].route().path(), "/random");
        assert_eq!(snapshot.windows()[1].panes().len(), 2);
        assert_eq!(snapshot.windows()[1].panes()[0].title(), "Pinned");
        assert_eq!(snapshot.windows()[1].panes()[0].cwd(), "/live/pinned");
        assert_eq!(
            snapshot.windows()[1].panes()[0].tmux_target(),
            Some("tmux-pinned:0.0")
        );
        assert_eq!(
            snapshot.windows()[1].panes()[0].status(),
            PaneStatus::Running
        );
        assert_eq!(
            snapshot.windows()[1].panes()[1].tmux_target(),
            Some("tmux-paused:0.0")
        );
        assert_eq!(
            snapshot.windows()[1].panes()[1].status(),
            PaneStatus::Missing
        );
    }

    #[test]
    fn production_connection_is_query_only() {
        let fixture = Fixture::new(true);
        let connection = match open_read_only(&fixture.database) {
            Ok(value) => value,
            Err(error) => panic!("could not open read-only fixture: {error}"),
        };
        let result = connection.execute(
            "INSERT INTO remembered_windows VALUES ('forbidden', 'Forbidden')",
            [],
        );
        assert!(result.is_err());
    }

    #[test]
    fn malformed_schema_fails_closed() {
        let fixture = Fixture::new(false);
        let mut state = mirror(&fixture.database);
        assert!(matches!(
            state.load(&TmuxInventory::unavailable(), 100),
            Err(StateError::MissingColumn {
                table: "sessions",
                column: "pane_target"
            })
        ));
    }

    #[test]
    fn existing_route_survives_database_window_rename() {
        let fixture = Fixture::new(true);
        let mut state = mirror(&fixture.database);
        let first = match state.load(&TmuxInventory::unavailable(), 100) {
            Ok(value) => value,
            Err(error) => panic!("could not load first snapshot: {error}"),
        };
        let first_route = match first
            .windows()
            .iter()
            .find(|window| window.name() == "Random")
        {
            Some(window) => window.route().path(),
            None => panic!("missing Random window"),
        };
        let connection = match Connection::open(&fixture.database) {
            Ok(value) => value,
            Err(error) => panic!("could not reopen fixture: {error}"),
        };
        if let Err(error) = connection.execute(
            "UPDATE remembered_windows SET window_title = 'Renamed' WHERE window_id = 'window-random'",
            [],
        ) {
            panic!("could not rename fixture window: {error}");
        }
        drop(connection);
        let second = match state.load(&TmuxInventory::unavailable(), 101) {
            Ok(value) => value,
            Err(error) => panic!("could not load second snapshot: {error}"),
        };
        let renamed = match second
            .windows()
            .iter()
            .find(|window| window.name() == "Renamed")
        {
            Some(window) => window,
            None => panic!("missing renamed window"),
        };
        assert_eq!(first_route, "/random");
        assert_eq!(renamed.route().path(), "/random");
    }

    #[test]
    fn unavailable_tmux_preserves_registry_status_without_false_missing() {
        let fixture = Fixture::new(true);
        let mut state = mirror(&fixture.database);
        let snapshot = match state.load(&TmuxInventory::unavailable(), 100) {
            Ok(value) => value,
            Err(error) => panic!("could not load snapshot: {error}"),
        };
        let random = match snapshot
            .windows()
            .iter()
            .find(|window| window.name() == "Random")
        {
            Some(window) => window,
            None => panic!("missing Random window"),
        };
        assert_eq!(random.panes()[0].status(), PaneStatus::Running);
        assert_eq!(random.panes()[1].status(), PaneStatus::Paused);
    }
}
