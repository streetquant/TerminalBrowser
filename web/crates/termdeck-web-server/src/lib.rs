#![forbid(unsafe_code)]

//! Bounded loopback HTTP vertical slice for the read-only t1000 mirror.
//!
//! The server always refuses non-loopback binds and never accepts terminal or
//! tmux mutation. Its original unauthenticated read-only mode remains available;
//! an explicit loopback-development authentication mode adds one-time pairing,
//! opaque device sessions, CSRF-protected session management, and protected
//! mirrored state routes.

mod auth_http;
mod machine_http;
mod staging_http;
mod tab_actions;
mod terminal_http;
mod terminal_stream;
mod tmux_runtime;

use auth_http::{
    DEVICES_PATH, HttpAuth, HttpAuthError, LOGIN_PATH, LOGOUT_PATH, MAX_AUTH_REQUEST_BYTES,
    PAIRING_PATH, SESSION_PATH, SETUP_PATH,
};
use axum::Json;
use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::ws::WebSocketUpgrade;
use axum::extract::{DefaultBodyLimit, Extension, Path as AxumPath, Request, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::error::Error;
use std::ffi::OsString;
use std::fmt;
use std::fs::{self, DirBuilder};
use std::net::{IpAddr, SocketAddr};
use std::path::{Component, Path, PathBuf};
use std::str::FromStr;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tab_actions::{ExistingTabAction, TabActionError, TabActionService};
use termdeck_web_auth::{
    AuthError, AuthLimits, AuthStore, AuthenticatedDeviceId, AuthenticatedSession,
    MAX_OWNER_PASSWORD_BYTES, MIN_OWNER_PASSWORD_BYTES, SessionContext,
};
use termdeck_web_core::{
    ControllerView, HostId, HostSnapshot, OwnerId, PaneSnapshot, PaneStatus, WindowSnapshot,
};
use terminal_http::{MAX_TERMINAL_INPUT_BYTES, TerminalError, TerminalKey, TerminalService};
use terminal_stream::{StreamPane, run_terminal_socket};
use zeroize::Zeroizing;

#[cfg(unix)]
use std::os::unix::fs::DirBuilderExt;
use termdeck_web_state::{
    MAX_DATABASE_ROWS, ReadOnlyStateMirror, StateError, TmuxInventory, TmuxPaneState,
};
use tokio::net::TcpListener;
use tokio::sync::Semaphore;
use tokio::time::{MissedTickBehavior, timeout};

pub const DEFAULT_LISTEN: &str = "127.0.0.1:7391";
pub const DEFAULT_REFRESH_MS: u64 = 1_000;
pub const DEFAULT_MAX_STALE_MS: u64 = 30_000;
pub const DEFAULT_TMUX_TIMEOUT_MS: u64 = 750;
pub const DEFAULT_TAB_ACTION_TIMEOUT_MS: u64 = 120_000;
pub const MAX_TAB_ACTION_TIMEOUT_MS: u64 = 900_000;
pub const DEFAULT_MAX_CONNECTIONS: usize = 128;
pub const DEFAULT_INVITATION_TTL_MS: u64 = 10 * 60 * 1_000;
pub const DEFAULT_SESSION_TTL_MS: u64 = 30 * 24 * 60 * 60 * 1_000;
pub const MAX_CONNECTIONS: usize = 1_024;
pub const MAX_PASSWORD_OPERATIONS: usize = 1;
pub const MAX_DEVICE_LABEL_BYTES: usize = 128;
pub const MAX_INVITATION_TTL_MS: u64 = 7 * 24 * 60 * 60 * 1_000;
pub const MAX_SESSION_TTL_MS: u64 = 180 * 24 * 60 * 60 * 1_000;
pub const MAX_RENDERED_RESPONSE_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_TMUX_OUTPUT_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_TMUX_ROWS: usize = MAX_DATABASE_ROWS * 4;
pub const MAX_FRONTEND_ASSET_BYTES: usize = 64 * 1024;
pub const MAX_TERMINAL_ASSET_BYTES: usize = 1024 * 1024;
pub const MAX_HTTP_REQUEST_BYTES: usize = MAX_TERMINAL_INPUT_BYTES + 1_024;

const FRONTEND_CSS_PATH: &str = "/_termdeck/assets/v7/app.css";
const FRONTEND_JS_PATH: &str = "/_termdeck/assets/v7/app.js";
const CLIENT_FILES_PATH: &str = "/_termdeck/assets/v7/client-files.js";
const ACCOUNTS_PATH: &str = "/_termdeck/assets/v7/accounts.js";
const WORKSPACE_UI_PATH: &str = "/_termdeck/assets/v7/workspace-ui.js";
const TERMINAL_CSS_PATH: &str = "/_termdeck/assets/v7/terminal.css";
const TERMINAL_JS_PATH: &str = "/_termdeck/assets/v7/terminal.js";
const EXTERNAL_BASE_PATH: &str = "/t1000";
const ROOT_COOKIE_PATH: &str = "; Path=/;";
const EXTERNAL_COOKIE_PATH: &str = "; Path=/t1000;";
const FORWARDED_PREFIX_HEADER: &str = "x-forwarded-prefix";
const FORWARDED_PROTO_HEADER: &str = "x-forwarded-proto";
const FORWARDED_HOST_HEADER: &str = "x-forwarded-host";
const BASE_PATH_PLACEHOLDER: &str = "__TERMDECK_BASE_PATH__";
const APP_CSS: &[u8] = include_bytes!("../assets/app.css");
const APP_JS: &[u8] = include_bytes!("../assets/app.js");
const CLIENT_FILES_JS: &[u8] = include_bytes!("../assets/client-files.js");
const ACCOUNTS_JS: &[u8] = include_bytes!("../assets/accounts.js");
const WORKSPACE_UI_JS: &[u8] = include_bytes!("../assets/workspace-ui.js");
const TERMINAL_CSS: &[u8] = include_bytes!("../assets/terminal.css");
const TERMINAL_JS: &[u8] = include_bytes!("../assets/terminal.js");
const _: () = {
    assert!(APP_CSS.len() <= MAX_FRONTEND_ASSET_BYTES);
    assert!(APP_JS.len() <= MAX_FRONTEND_ASSET_BYTES);
    assert!(CLIENT_FILES_JS.len() <= MAX_FRONTEND_ASSET_BYTES);
    assert!(ACCOUNTS_JS.len() <= MAX_FRONTEND_ASSET_BYTES);
    assert!(WORKSPACE_UI_JS.len() <= MAX_FRONTEND_ASSET_BYTES);
    assert!(TERMINAL_CSS.len() <= MAX_TERMINAL_ASSET_BYTES);
    assert!(TERMINAL_JS.len() <= MAX_TERMINAL_ASSET_BYTES);
};
const CONTENT_SECURITY_POLICY: &str = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; manifest-src 'none'; worker-src 'none'";
const USAGE: &str = "t1000 read-only web mirror\n\n\
Usage: termdeck-web-server [options]\n\n\
Options:\n\
  --db PATH                t1000 sessions.sqlite3 path\n\
  --listen IP:PORT         Loopback address (default 127.0.0.1:7391)\n\
  --refresh-ms N           Refresh interval, 250..60000\n\
  --max-stale-ms N         Maximum stale serving window, 1000..600000\n\
  --max-connections N      Concurrent request cap, 1..1024\n\
  --tmux PATH              tmux executable (default tmux)\n\
  --tmux-timeout-ms N      Read-only inventory timeout, 50..5000\n\
  --t1000 PATH             Fixed t1000 executable for authenticated tab actions\n\
  --tab-action-timeout-ms N\n\
                           Tab action timeout, 1000..900000\n\
  --host-id ID             Opaque local host identity\n\
  --owner-id ID            Opaque single-owner identity\n\
  --auth-dir PATH          Separate owner-only authentication directory\n\
  --allow-insecure-loopback-auth\n\
                           Explicitly permit auth cookies over loopback HTTP\n\
  --session-ttl-ms N       Device-session lifetime, 1000..15552000000\n\
  --invitation-ttl-ms N    Pairing-invitation lifetime, 1000..604800000\n\
  --create-invitation LABEL\n\
                           Create one local pairing invitation and exit\n\
  -h, --help               Show this help\n\n\
Non-loopback listening remains refused. Authentication over plain HTTP is\n\
available only with the explicit loopback-development opt-in.\n";

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
struct RequestBasePath(&'static str);

impl RequestBasePath {
    fn as_str(self) -> &'static str {
        self.0
    }

    fn prefixed(self, path: &str) -> String {
        debug_assert!(path.starts_with('/'));
        let mut output = String::with_capacity(self.0.len() + path.len());
        output.push_str(self.0);
        output.push_str(path);
        output
    }
}

#[derive(Clone, Debug)]
pub struct ServerConfig {
    database_path: PathBuf,
    listen: SocketAddr,
    refresh_interval: Duration,
    max_stale: Duration,
    max_connections: usize,
    tmux_executable: PathBuf,
    tmux_timeout: Duration,
    t1000_executable: PathBuf,
    tab_action_timeout: Duration,
    host_id: HostId,
    owner_id: OwnerId,
    auth_directory: Option<PathBuf>,
    allow_insecure_loopback_auth: bool,
    session_ttl: Duration,
}

#[derive(Clone, Debug)]
pub struct InvitationConfig {
    auth_directory: PathBuf,
    host_id: HostId,
    owner_id: OwnerId,
    device_label: String,
    invitation_ttl: Duration,
}

impl ServerConfig {
    pub fn parse(args: impl IntoIterator<Item = OsString>) -> Result<ConfigAction, ServerError> {
        let home = std::env::var_os("HOME").ok_or(ServerError::MissingHome)?;
        Self::parse_with_home(args, PathBuf::from(home))
    }

    fn parse_with_home(
        args: impl IntoIterator<Item = OsString>,
        home: PathBuf,
    ) -> Result<ConfigAction, ServerError> {
        let mut config = Self {
            database_path: home.join(".local/share/termdeck/sessions.sqlite3"),
            listen: parse_socket(DEFAULT_LISTEN)?,
            refresh_interval: Duration::from_millis(DEFAULT_REFRESH_MS),
            max_stale: Duration::from_millis(DEFAULT_MAX_STALE_MS),
            max_connections: DEFAULT_MAX_CONNECTIONS,
            tmux_executable: PathBuf::from("tmux"),
            tmux_timeout: Duration::from_millis(DEFAULT_TMUX_TIMEOUT_MS),
            t1000_executable: home.join(".local/bin/t1000"),
            tab_action_timeout: Duration::from_millis(DEFAULT_TAB_ACTION_TIMEOUT_MS),
            host_id: HostId::new("termdeck-local-host")?,
            owner_id: OwnerId::new("termdeck-local-owner")?,
            auth_directory: None,
            allow_insecure_loopback_auth: false,
            session_ttl: Duration::from_millis(DEFAULT_SESSION_TTL_MS),
        };
        let mut invitation_ttl = Duration::from_millis(DEFAULT_INVITATION_TTL_MS);
        let mut create_invitation_label: Option<String> = None;
        let mut arguments = args.into_iter();
        while let Some(argument) = arguments.next() {
            let argument = argument
                .into_string()
                .map_err(|_| ServerError::NonUtf8Argument)?;
            match argument.as_str() {
                "-h" | "--help" => return Ok(ConfigAction::Help),
                "--db" => config.database_path = PathBuf::from(next_value(&mut arguments, "--db")?),
                "--listen" => {
                    config.listen = parse_socket(&next_value(&mut arguments, "--listen")?)?
                }
                "--refresh-ms" => {
                    let value =
                        parse_u64(&next_value(&mut arguments, "--refresh-ms")?, "--refresh-ms")?;
                    if !(250..=60_000).contains(&value) {
                        return Err(ServerError::InvalidRange("--refresh-ms", 250, 60_000));
                    }
                    config.refresh_interval = Duration::from_millis(value);
                }
                "--max-stale-ms" => {
                    let value = parse_u64(
                        &next_value(&mut arguments, "--max-stale-ms")?,
                        "--max-stale-ms",
                    )?;
                    if !(1_000..=600_000).contains(&value) {
                        return Err(ServerError::InvalidRange("--max-stale-ms", 1_000, 600_000));
                    }
                    config.max_stale = Duration::from_millis(value);
                }
                "--max-connections" => {
                    let value = parse_usize(
                        &next_value(&mut arguments, "--max-connections")?,
                        "--max-connections",
                    )?;
                    if value == 0 || value > MAX_CONNECTIONS {
                        return Err(ServerError::InvalidRange(
                            "--max-connections",
                            1,
                            u64::try_from(MAX_CONNECTIONS)
                                .map_err(|_| ServerError::NumericOverflow)?,
                        ));
                    }
                    config.max_connections = value;
                }
                "--tmux" => {
                    config.tmux_executable = PathBuf::from(next_value(&mut arguments, "--tmux")?);
                }
                "--tmux-timeout-ms" => {
                    let value = parse_u64(
                        &next_value(&mut arguments, "--tmux-timeout-ms")?,
                        "--tmux-timeout-ms",
                    )?;
                    if !(50..=5_000).contains(&value) {
                        return Err(ServerError::InvalidRange("--tmux-timeout-ms", 50, 5_000));
                    }
                    config.tmux_timeout = Duration::from_millis(value);
                }
                "--t1000" => {
                    config.t1000_executable = PathBuf::from(next_value(&mut arguments, "--t1000")?);
                }
                "--tab-action-timeout-ms" => {
                    let value = parse_u64(
                        &next_value(&mut arguments, "--tab-action-timeout-ms")?,
                        "--tab-action-timeout-ms",
                    )?;
                    if !(1_000..=MAX_TAB_ACTION_TIMEOUT_MS).contains(&value) {
                        return Err(ServerError::InvalidRange(
                            "--tab-action-timeout-ms",
                            1_000,
                            MAX_TAB_ACTION_TIMEOUT_MS,
                        ));
                    }
                    config.tab_action_timeout = Duration::from_millis(value);
                }
                "--host-id" => {
                    config.host_id = HostId::new(next_value(&mut arguments, "--host-id")?)?;
                }
                "--owner-id" => {
                    config.owner_id = OwnerId::new(next_value(&mut arguments, "--owner-id")?)?;
                }
                "--auth-dir" => {
                    config.auth_directory =
                        Some(PathBuf::from(next_value(&mut arguments, "--auth-dir")?));
                }
                "--allow-insecure-loopback-auth" => {
                    config.allow_insecure_loopback_auth = true;
                }
                "--session-ttl-ms" => {
                    let value = parse_u64(
                        &next_value(&mut arguments, "--session-ttl-ms")?,
                        "--session-ttl-ms",
                    )?;
                    if !(1_000..=MAX_SESSION_TTL_MS).contains(&value) {
                        return Err(ServerError::InvalidRange(
                            "--session-ttl-ms",
                            1_000,
                            MAX_SESSION_TTL_MS,
                        ));
                    }
                    config.session_ttl = Duration::from_millis(value);
                }
                "--invitation-ttl-ms" => {
                    let value = parse_u64(
                        &next_value(&mut arguments, "--invitation-ttl-ms")?,
                        "--invitation-ttl-ms",
                    )?;
                    if !(1_000..=MAX_INVITATION_TTL_MS).contains(&value) {
                        return Err(ServerError::InvalidRange(
                            "--invitation-ttl-ms",
                            1_000,
                            MAX_INVITATION_TTL_MS,
                        ));
                    }
                    invitation_ttl = Duration::from_millis(value);
                }
                "--create-invitation" => {
                    if create_invitation_label.is_some() {
                        return Err(ServerError::DuplicateArgument("--create-invitation"));
                    }
                    create_invitation_label =
                        Some(next_value(&mut arguments, "--create-invitation")?);
                }
                _ => return Err(ServerError::UnknownArgument(argument)),
            }
        }
        if !config.listen.ip().is_loopback() {
            return Err(ServerError::NonLoopbackRequiresAuthentication(
                config.listen,
            ));
        }
        if config.max_stale < config.refresh_interval {
            return Err(ServerError::StaleWindowShorterThanRefresh);
        }
        if let Some(device_label) = create_invitation_label {
            let auth_directory = config
                .auth_directory
                .ok_or(ServerError::AuthDirectoryRequired)?;
            return Ok(ConfigAction::CreateInvitation(InvitationConfig {
                auth_directory,
                host_id: config.host_id,
                owner_id: config.owner_id,
                device_label,
                invitation_ttl,
            }));
        }
        match (&config.auth_directory, config.allow_insecure_loopback_auth) {
            (Some(_), false) => return Err(ServerError::InsecureLoopbackAuthRequiresOptIn),
            (None, true) => return Err(ServerError::AuthDirectoryRequired),
            _ => {}
        }
        Ok(ConfigAction::Run(config))
    }

    #[must_use]
    pub fn database_path(&self) -> &Path {
        &self.database_path
    }

    #[must_use]
    pub fn listen(&self) -> SocketAddr {
        self.listen
    }

    #[must_use]
    pub fn authentication_enabled(&self) -> bool {
        self.auth_directory.is_some()
    }
}

pub enum ConfigAction {
    Run(ServerConfig),
    CreateInvitation(InvitationConfig),
    Help,
}

#[derive(Debug)]
pub enum ServerError {
    Auth(AuthError),
    HttpAuth(HttpAuthError),
    Io(std::io::Error),
    State(StateError),
    TabAction(TabActionError),
    Json(serde_json::Error),
    Id(termdeck_web_core::IdError),
    Join(tokio::task::JoinError),
    AuthDirectoryRequired,
    CacheLockPoisoned,
    DuplicateArgument(&'static str),
    InsecureLoopbackAuthRequiresOptIn,
    InvalidAuthDirectory(&'static str),
    MissingHome,
    NonUtf8Argument,
    MissingArgumentValue(&'static str),
    UnknownArgument(String),
    InvalidNumber { option: &'static str, value: String },
    InvalidRange(&'static str, u64, u64),
    InvalidSocket(String),
    NonLoopbackRequiresAuthentication(SocketAddr),
    StaleWindowShorterThanRefresh,
    NumericOverflow,
    MirrorLockPoisoned,
    ResponseTooLarge { actual: usize, maximum: usize },
    TmuxTimeout,
    TmuxFailed(String),
    TmuxOutputTooLarge(usize),
    Terminal(TerminalError),
    MalformedTmuxRow(String),
    TooManyTmuxRows(usize),
}

impl fmt::Display for ServerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Auth(error) => write!(formatter, "web authentication store failed: {error}"),
            Self::HttpAuth(error) => write!(formatter, "web authentication failed: {error}"),
            Self::Io(error) => write!(formatter, "web server I/O failed: {error}"),
            Self::State(error) => write!(formatter, "web state refresh failed: {error}"),
            Self::TabAction(error) => write!(formatter, "web tab action failed: {error}"),
            Self::Json(error) => write!(formatter, "web JSON rendering failed: {error}"),
            Self::Id(error) => write!(formatter, "web identity failed: {error}"),
            Self::Join(error) => write!(formatter, "web refresh worker failed: {error}"),
            Self::AuthDirectoryRequired => {
                formatter.write_str("--auth-dir is required for this authentication action")
            }
            Self::CacheLockPoisoned => formatter.write_str("snapshot cache lock is poisoned"),
            Self::DuplicateArgument(option) => write!(formatter, "duplicate argument {option}"),
            Self::InsecureLoopbackAuthRequiresOptIn => formatter
                .write_str("loopback HTTP authentication requires --allow-insecure-loopback-auth"),
            Self::InvalidAuthDirectory(detail) => {
                write!(formatter, "invalid authentication directory: {detail}")
            }
            Self::MissingHome => formatter.write_str("HOME is unavailable"),
            Self::NonUtf8Argument => formatter.write_str("command-line argument is not UTF-8"),
            Self::MissingArgumentValue(option) => write!(formatter, "missing value for {option}"),
            Self::UnknownArgument(argument) => write!(formatter, "unknown argument {argument}"),
            Self::InvalidNumber { option, value } => {
                write!(formatter, "invalid numeric value for {option}: {value}")
            }
            Self::InvalidRange(option, minimum, maximum) => write!(
                formatter,
                "{option} must be between {minimum} and {maximum}"
            ),
            Self::InvalidSocket(value) => write!(formatter, "invalid listen address: {value}"),
            Self::NonLoopbackRequiresAuthentication(address) => write!(
                formatter,
                "non-loopback listen {address} remains refused until a secure authenticated origin is implemented"
            ),
            Self::StaleWindowShorterThanRefresh => {
                formatter.write_str("max stale window must not be shorter than refresh interval")
            }
            Self::NumericOverflow => formatter.write_str("numeric conversion overflow"),
            Self::MirrorLockPoisoned => formatter.write_str("state mirror lock is poisoned"),
            Self::ResponseTooLarge { actual, maximum } => write!(
                formatter,
                "rendered response is {actual} bytes; maximum is {maximum}"
            ),
            Self::TmuxTimeout => formatter.write_str("read-only tmux inventory timed out"),
            Self::TmuxFailed(detail) => {
                write!(formatter, "read-only tmux inventory failed: {detail}")
            }
            Self::TmuxOutputTooLarge(actual) => write!(
                formatter,
                "tmux inventory output is {actual} bytes; maximum is {MAX_TMUX_OUTPUT_BYTES}"
            ),
            Self::Terminal(error) => write!(formatter, "terminal operation failed: {error}"),
            Self::MalformedTmuxRow(row) => write!(formatter, "malformed tmux inventory row: {row}"),
            Self::TooManyTmuxRows(actual) => write!(
                formatter,
                "tmux inventory returned {actual} rows; maximum is {MAX_TMUX_ROWS}"
            ),
        }
    }
}

impl Error for ServerError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Auth(error) => Some(error),
            Self::HttpAuth(error) => Some(error),
            Self::Io(error) => Some(error),
            Self::State(error) => Some(error),
            Self::TabAction(error) => Some(error),
            Self::Json(error) => Some(error),
            Self::Id(error) => Some(error),
            Self::Join(error) => Some(error),
            Self::Terminal(error) => Some(error),
            _ => None,
        }
    }
}

impl From<AuthError> for ServerError {
    fn from(error: AuthError) -> Self {
        Self::Auth(error)
    }
}

impl From<HttpAuthError> for ServerError {
    fn from(error: HttpAuthError) -> Self {
        Self::HttpAuth(error)
    }
}

impl From<std::io::Error> for ServerError {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<StateError> for ServerError {
    fn from(error: StateError) -> Self {
        Self::State(error)
    }
}

impl From<TabActionError> for ServerError {
    fn from(error: TabActionError) -> Self {
        Self::TabAction(error)
    }
}

impl From<serde_json::Error> for ServerError {
    fn from(error: serde_json::Error) -> Self {
        Self::Json(error)
    }
}

impl From<termdeck_web_core::IdError> for ServerError {
    fn from(error: termdeck_web_core::IdError) -> Self {
        Self::Id(error)
    }
}

impl From<tokio::task::JoinError> for ServerError {
    fn from(error: tokio::task::JoinError) -> Self {
        Self::Join(error)
    }
}

impl From<TerminalError> for ServerError {
    fn from(error: TerminalError) -> Self {
        Self::Terminal(error)
    }
}

#[derive(Clone)]
struct RefreshEngine {
    mirror: Arc<Mutex<ReadOnlyStateMirror>>,
    cache: SnapshotCache,
    tmux_executable: PathBuf,
    tmux_timeout: Duration,
}

impl RefreshEngine {
    async fn refresh(&self) -> Result<(), ServerError> {
        let (inventory, tmux_warning) =
            match load_tmux_inventory(&self.tmux_executable, self.tmux_timeout).await {
                Ok(value) => (value, None),
                Err(error) => (TmuxInventory::unavailable(), Some(error.to_string())),
            };
        let generated_at = unix_ms();
        let mirror = Arc::clone(&self.mirror);
        let snapshot = tokio::task::spawn_blocking(move || {
            let mut guard = mirror.lock().map_err(|_| ServerError::MirrorLockPoisoned)?;
            guard
                .load(&inventory, generated_at)
                .map_err(ServerError::from)
        })
        .await??;
        let publication = RenderedSnapshot::new(snapshot, tmux_warning)?;
        self.cache.publish(publication)?;
        Ok(())
    }

    async fn run_periodic(self, refresh_interval: Duration) {
        let mut interval = tokio::time::interval(refresh_interval);
        interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
        interval.tick().await;
        loop {
            interval.tick().await;
            if let Err(error) = self.refresh().await {
                match self.cache.record_error(error.to_string(), unix_ms()) {
                    Ok(true) => eprintln!("t1000 web refresh failed: {error}"),
                    Ok(false) => {}
                    Err(cache_error) => eprintln!(
                        "t1000 web refresh failed: {error}; cache update failed: {cache_error}"
                    ),
                }
            }
        }
    }
}

#[derive(Clone)]
struct SnapshotCache {
    inner: Arc<RwLock<CacheInner>>,
    max_stale_ms: u64,
}

#[derive(Default)]
struct CacheInner {
    current: Option<Arc<RenderedSnapshot>>,
    last_error: Option<CacheFailure>,
}

struct CacheFailure {
    message: String,
    at_unix_ms: u64,
}

#[derive(Clone)]
enum CacheAvailability {
    Ready(Arc<RenderedSnapshot>),
    Degraded {
        snapshot: Arc<RenderedSnapshot>,
        error: Arc<str>,
    },
    Expired,
    Unavailable,
}

impl SnapshotCache {
    fn new(max_stale: Duration) -> Result<Self, ServerError> {
        Ok(Self {
            inner: Arc::new(RwLock::new(CacheInner::default())),
            max_stale_ms: duration_ms(max_stale)?,
        })
    }

    fn publish(&self, snapshot: RenderedSnapshot) -> Result<(), ServerError> {
        let mut guard = self
            .inner
            .write()
            .map_err(|_| ServerError::CacheLockPoisoned)?;
        guard.current = Some(Arc::new(snapshot));
        guard.last_error = None;
        Ok(())
    }

    fn record_error(&self, message: String, at_unix_ms: u64) -> Result<bool, ServerError> {
        let mut guard = self
            .inner
            .write()
            .map_err(|_| ServerError::CacheLockPoisoned)?;
        let first_failure = guard.last_error.is_none();
        let first_failure_at = guard
            .last_error
            .as_ref()
            .map_or(at_unix_ms, |failure| failure.at_unix_ms);
        guard.last_error = Some(CacheFailure {
            message,
            at_unix_ms: first_failure_at,
        });
        Ok(first_failure)
    }

    fn availability(&self, now_unix_ms: u64) -> CacheAvailability {
        let Ok(guard) = self.inner.read() else {
            return CacheAvailability::Unavailable;
        };
        let Some(snapshot) = guard.current.as_ref().cloned() else {
            return CacheAvailability::Unavailable;
        };
        let Some(error) = guard.last_error.as_ref() else {
            return CacheAvailability::Ready(snapshot);
        };
        let age = now_unix_ms.saturating_sub(error.at_unix_ms);
        if age > self.max_stale_ms {
            CacheAvailability::Expired
        } else {
            CacheAvailability::Degraded {
                snapshot,
                error: Arc::<str>::from(error.message.as_str()),
            }
        }
    }

    fn readiness(&self, now_unix_ms: u64) -> (&'static str, StatusCode) {
        match self.availability(now_unix_ms) {
            CacheAvailability::Ready(_) => ("ready", StatusCode::OK),
            CacheAvailability::Degraded { .. } => ("degraded", StatusCode::SERVICE_UNAVAILABLE),
            CacheAvailability::Expired => ("stale-expired", StatusCode::SERVICE_UNAVAILABLE),
            CacheAvailability::Unavailable => ("unavailable", StatusCode::SERVICE_UNAVAILABLE),
        }
    }

    #[cfg(test)]
    fn last_error_time(&self) -> Option<u64> {
        let Ok(guard) = self.inner.read() else {
            return None;
        };
        guard.last_error.as_ref().map(|error| error.at_unix_ms)
    }
}

struct RenderedSnapshot {
    snapshot: Arc<HostSnapshot>,
    full_json: Bytes,
    homepage_html: Bytes,
    windows: BTreeMap<String, RenderedWindow>,
}

struct RenderedWindow {
    html: Bytes,
    json: Bytes,
}

impl RenderedSnapshot {
    fn new(snapshot: HostSnapshot, tmux_warning: Option<String>) -> Result<Self, ServerError> {
        let snapshot = Arc::new(snapshot);
        let full_json = serde_json::to_vec(&host_dto(snapshot.as_ref()))?;
        ensure_response_bound(full_json.len())?;
        let homepage_html = render_homepage(snapshot.as_ref(), tmux_warning.as_deref())?;
        let mut windows = BTreeMap::new();
        for window in snapshot.windows() {
            let json = serde_json::to_vec(&WindowEnvelopeDto {
                schema: "termdeck.window-snapshot.v1",
                revision: snapshot.revision(),
                revision_text: snapshot.revision().to_string(),
                generated_at_unix_ms: snapshot.generated_at_unix_ms(),
                window: window_dto(window),
            })?;
            ensure_response_bound(json.len())?;
            let html = render_window_page(snapshot.as_ref(), window, tmux_warning.as_deref())?;
            windows.insert(
                window.route().slug().to_owned(),
                RenderedWindow {
                    html: Bytes::from(html),
                    json: Bytes::from(json),
                },
            );
        }
        Ok(Self {
            snapshot,
            full_json: Bytes::from(full_json),
            homepage_html: Bytes::from(homepage_html),
            windows,
        })
    }
}

#[derive(Clone)]
struct AppState {
    cache: SnapshotCache,
    request_slots: Arc<Semaphore>,
    password_slots: Arc<Semaphore>,
    auth: Option<Arc<HttpAuth>>,
    terminal: Option<Arc<TerminalService>>,
    tab_actions: Option<Arc<TabActionService>>,
}

#[derive(Serialize)]
struct InvitationOutput<'a> {
    schema: &'static str,
    invitation_id: &'a str,
    invitation_secret: &'a str,
    expires_at_unix_ms: u64,
}

pub fn run_from_env() -> Result<(), ServerError> {
    match ServerConfig::parse(std::env::args_os().skip(1))? {
        ConfigAction::Help => {
            println!("{USAGE}");
            Ok(())
        }
        ConfigAction::CreateInvitation(config) => create_pairing_invitation(config),
        ConfigAction::Run(config) => {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            runtime.block_on(serve(config))
        }
    }
}

fn create_pairing_invitation(config: InvitationConfig) -> Result<(), ServerError> {
    ensure_auth_directory(&config.auth_directory)?;
    let now_ms = unix_ms();
    let store = AuthStore::open_or_create(
        &config.auth_directory,
        config.host_id,
        config.owner_id,
        now_ms,
        AuthLimits::default(),
    )?;
    let invitation = store.create_invitation(
        config.device_label.as_str(),
        now_ms,
        duration_ms(config.invitation_ttl)?,
    )?;
    let output = InvitationOutput {
        schema: "termdeck.pairing-invitation.v1",
        invitation_id: invitation.invitation_id().as_str(),
        invitation_secret: invitation.secret().expose_secret(),
        expires_at_unix_ms: invitation.expires_at_ms(),
    };
    println!("{}", serde_json::to_string(&output)?);
    Ok(())
}

pub async fn serve(config: ServerConfig) -> Result<(), ServerError> {
    tmux_runtime::initialize()?;
    if !config.listen.ip().is_loopback() {
        return Err(ServerError::NonLoopbackRequiresAuthentication(
            config.listen,
        ));
    }
    let mirror = ReadOnlyStateMirror::new(
        &config.database_path,
        config.host_id.clone(),
        config.owner_id.clone(),
    )?;
    let auth = match config.auth_directory.as_ref() {
        Some(auth_directory) => {
            if !config.allow_insecure_loopback_auth {
                return Err(ServerError::InsecureLoopbackAuthRequiresOptIn);
            }
            ensure_auth_directory(auth_directory)?;
            let store = AuthStore::open_or_create(
                auth_directory,
                config.host_id.clone(),
                config.owner_id.clone(),
                unix_ms(),
                AuthLimits::default(),
            )?;
            Some(Arc::new(HttpAuth::new(
                store,
                duration_ms(config.session_ttl)?,
                false,
            )?))
        }
        None => None,
    };
    let terminal = if auth.is_some() {
        Some(Arc::new(TerminalService::new(
            config.owner_id.clone(),
            config.tmux_executable.clone(),
            config.tmux_timeout,
        )?))
    } else {
        None
    };
    let tab_actions = if auth.is_some() {
        Some(Arc::new(TabActionService::new(
            config.t1000_executable.clone(),
            config.database_path.clone(),
            config.tab_action_timeout,
        )?))
    } else {
        None
    };
    let cache = SnapshotCache::new(config.max_stale)?;
    let engine = RefreshEngine {
        mirror: Arc::new(Mutex::new(mirror)),
        cache: cache.clone(),
        tmux_executable: config.tmux_executable.clone(),
        tmux_timeout: config.tmux_timeout,
    };
    engine.refresh().await?;
    let state = AppState {
        cache,
        request_slots: Arc::new(Semaphore::new(config.max_connections)),
        password_slots: Arc::new(Semaphore::new(MAX_PASSWORD_OPERATIONS)),
        auth,
        terminal,
        tab_actions,
    };
    let application = build_router(state);
    let listener = TcpListener::bind(config.listen).await?;
    println!(
        "t1000 read-only web mirror listening on http://{}",
        listener.local_addr()?
    );
    let refresher = tokio::spawn(engine.run_periodic(config.refresh_interval));
    let result = axum::serve(listener, application)
        .with_graceful_shutdown(shutdown_signal())
        .await;
    refresher.abort();
    result.map_err(ServerError::Io)
}

fn ensure_auth_directory(directory: &Path) -> Result<(), ServerError> {
    if !directory.is_absolute()
        || directory
            .components()
            .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
    {
        return Err(ServerError::InvalidAuthDirectory(
            "path must be absolute and normalized",
        ));
    }
    let parent = directory
        .parent()
        .ok_or(ServerError::InvalidAuthDirectory("path has no parent"))?;
    let parent_metadata = fs::symlink_metadata(parent)?;
    if parent_metadata.file_type().is_symlink() || !parent_metadata.is_dir() {
        return Err(ServerError::InvalidAuthDirectory(
            "parent must be a non-symlink directory",
        ));
    }
    if fs::canonicalize(parent)? != parent {
        return Err(ServerError::InvalidAuthDirectory(
            "parent cannot contain symlink components",
        ));
    }
    match fs::symlink_metadata(directory) {
        Ok(_metadata) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let mut builder = DirBuilder::new();
            #[cfg(unix)]
            builder.mode(0o700);
            builder.create(directory)?;
            fs::File::open(parent)?.sync_all()?;
        }
        Err(error) => return Err(ServerError::Io(error)),
    }
    Ok(())
}

fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/", get(homepage))
        .route(FRONTEND_CSS_PATH, get(frontend_css))
        .route(FRONTEND_JS_PATH, get(frontend_js))
        .route(CLIENT_FILES_PATH, get(client_files_js))
        .route(ACCOUNTS_PATH, get(accounts_js))
        .route(WORKSPACE_UI_PATH, get(workspace_ui_js))
        .route(TERMINAL_CSS_PATH, get(terminal_css))
        .route(TERMINAL_JS_PATH, get(terminal_js))
        .route("/_termdeck/healthz", get(healthz))
        .route("/_termdeck/readyz", get(readyz))
        .route(SETUP_PATH, get(setup_page).post(setup_submit))
        .route(LOGIN_PATH, get(login_page).post(login_submit))
        .route(PAIRING_PATH, get(pairing_page).post(pairing_submit))
        .route(SESSION_PATH, get(session_info))
        .route(LOGOUT_PATH, axum::routing::post(logout))
        .route(DEVICES_PATH, get(devices))
        .route(
            "/_termdeck/api/v1/machines",
            get(machine_http::list).post(machine_http::mutate),
        )
        .route(
            "/_termdeck/api/v1/machines/current",
            get(machine_http::current),
        )
        .route(
            "/_termdeck/api/v1/client-files",
            axum::routing::post(staging_http::operate),
        )
        .route(
            "/_termdeck/api/v1/devices/{device_id}/revoke",
            axum::routing::post(revoke_device),
        )
        .route("/_termdeck/api/v1/snapshot", get(api_snapshot))
        .route(
            "/_termdeck/api/v1/management",
            axum::routing::post(management),
        )
        .route("/_termdeck/api/v1/windows/{slug}", get(api_window))
        .route(
            "/_termdeck/api/v1/windows/{slug}/hidden-tabs",
            get(tab_hidden),
        )
        .route(
            "/_termdeck/api/v1/windows/{slug}/hidden-tabs/{session_id}/restore",
            axum::routing::post(tab_restore),
        )
        .route(
            "/_termdeck/api/v1/windows/{slug}/tabs",
            axum::routing::post(tab_create),
        )
        .route(
            "/_termdeck/api/v1/windows/{slug}/panes/{pane_id}/tab",
            axum::routing::post(tab_mutate),
        )
        .route(
            "/_termdeck/api/v1/windows/{slug}/panes/{pane_id}/capture",
            get(terminal_capture),
        )
        .route(
            "/_termdeck/api/v1/windows/{slug}/panes/{pane_id}/control",
            get(terminal_control_status).post(terminal_control_acquire),
        )
        .route(
            "/_termdeck/api/v1/windows/{slug}/panes/{pane_id}/control/heartbeat",
            axum::routing::post(terminal_control_heartbeat),
        )
        .route(
            "/_termdeck/api/v1/windows/{slug}/panes/{pane_id}/control/release",
            axum::routing::post(terminal_control_release),
        )
        .route(
            "/_termdeck/api/v1/windows/{slug}/panes/{pane_id}/input/text",
            axum::routing::post(terminal_input_text),
        )
        .route(
            "/_termdeck/api/v1/windows/{slug}/panes/{pane_id}/input/key",
            axum::routing::post(terminal_input_key),
        )
        .route(
            "/_termdeck/ws/v1/windows/{slug}/panes/{pane_id}",
            get(terminal_websocket),
        )
        .route("/{slug}/t/{pane_id}", get(terminal_page))
        .route("/{slug}", get(window_page))
        .fallback(not_found)
        .layer(DefaultBodyLimit::max(MAX_HTTP_REQUEST_BYTES))
        .layer(middleware::from_fn_with_state(state.clone(), auth_gate))
        .layer(middleware::from_fn_with_state(state.clone(), request_gate))
        .layer(middleware::from_fn(security_headers))
        .layer(middleware::from_fn(forwarded_prefix_gate))
        .with_state(state)
}

async fn forwarded_prefix_gate(mut request: Request, next: Next) -> Response {
    let forwarded_prefix_present = request.headers().contains_key(FORWARDED_PREFIX_HEADER);
    let base_path = match single_header(request.headers(), FORWARDED_PREFIX_HEADER) {
        None if forwarded_prefix_present => {
            return bad_request_response("forwarded prefix must appear exactly once");
        }
        None => RequestBasePath::default(),
        Some(value) => {
            let prefix = match value.to_str() {
                Ok(value) => value,
                Err(_) => return bad_request_response("invalid forwarded prefix"),
            };
            let proto = single_header(request.headers(), FORWARDED_PROTO_HEADER)
                .and_then(|value| value.to_str().ok());
            let host = single_header(request.headers(), FORWARDED_HOST_HEADER)
                .and_then(|value| value.to_str().ok());
            if prefix != EXTERNAL_BASE_PATH
                || proto != Some("https")
                || !matches!(host, Some("maxchat.semiotech.org") | Some("semiotech.org"))
            {
                return forbidden_response("forwarded prefix is not trusted");
            }
            RequestBasePath(EXTERNAL_BASE_PATH)
        }
    };
    request.extensions_mut().insert(base_path);
    let mut response = next.run(request).await;
    if !base_path.as_str().is_empty() {
        if let Some(location) = response.headers().get(header::LOCATION).cloned()
            && let Ok(location) = location.to_str()
            && location.starts_with('/')
            && !location.starts_with("//")
            && !location.starts_with(EXTERNAL_BASE_PATH)
            && let Ok(prefixed) = HeaderValue::from_str(base_path.prefixed(location).as_str())
        {
            response.headers_mut().insert(header::LOCATION, prefixed);
        }
        scope_response_cookie_paths(&mut response);
        response.headers_mut().insert(
            HeaderName::from_static("x-termdeck-base-path"),
            HeaderValue::from_static(EXTERNAL_BASE_PATH),
        );
    }
    response
}

fn scope_response_cookie_paths(response: &mut Response) {
    let cookies: Vec<HeaderValue> = response
        .headers()
        .get_all(header::SET_COOKIE)
        .iter()
        .cloned()
        .collect();
    if cookies.is_empty() {
        return;
    }
    response.headers_mut().remove(header::SET_COOKIE);
    for cookie in cookies {
        let scoped = cookie
            .to_str()
            .ok()
            .and_then(|value| {
                HeaderValue::from_str(
                    value
                        .replacen(ROOT_COOKIE_PATH, EXTERNAL_COOKIE_PATH, 1)
                        .as_str(),
                )
                .ok()
            })
            .unwrap_or(cookie);
        response.headers_mut().append(header::SET_COOKIE, scoped);
    }
}

async fn request_gate(State(state): State<AppState>, request: Request, next: Next) -> Response {
    if !host_is_allowed(request.headers()) || !origin_matches_host_when_present(request.headers()) {
        return text_response(
            StatusCode::MISDIRECTED_REQUEST,
            "text/plain; charset=utf-8",
            Bytes::from_static(b"request authority is not allowed\n"),
        );
    }
    let permit = match Arc::clone(&state.request_slots).try_acquire_owned() {
        Ok(value) => value,
        Err(_) => {
            let mut response = text_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "text/plain; charset=utf-8",
                Bytes::from_static(b"request capacity reached\n"),
            );
            response
                .headers_mut()
                .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
            return response;
        }
    };
    let response = next.run(request).await;
    drop(permit);
    response
}

async fn auth_gate(State(state): State<AppState>, mut request: Request, next: Next) -> Response {
    let Some(auth) = state.auth.as_ref() else {
        return next.run(request).await;
    };
    let path = request.uri().path().to_owned();
    let method = request.method().clone();
    if public_auth_route(path.as_str()) {
        if method != Method::GET
            && method != Method::HEAD
            && !unsafe_request_metadata_allowed(request.headers(), auth.secure_cookies())
        {
            return forbidden_response("request metadata is not allowed");
        }
        return next.run(request).await;
    }
    let context = match auth.authenticate(request.headers(), unix_ms()) {
        Ok(value) => value,
        Err(error) => return authentication_error_response(error, path.as_str()),
    };
    if method != Method::GET && method != Method::HEAD {
        if !unsafe_request_metadata_allowed(request.headers(), auth.secure_cookies()) {
            return forbidden_response("request metadata is not allowed");
        }
        if let Err(error) = auth.verify_csrf(request.headers(), &context, unix_ms()) {
            return authentication_error_response(error, path.as_str());
        }
    }
    request.extensions_mut().insert(context);
    next.run(request).await
}

fn public_auth_route(path: &str) -> bool {
    matches!(
        path,
        FRONTEND_CSS_PATH
            | FRONTEND_JS_PATH
            | CLIENT_FILES_PATH
            | ACCOUNTS_PATH
            | WORKSPACE_UI_PATH
            | TERMINAL_CSS_PATH
            | TERMINAL_JS_PATH
            | "/_termdeck/healthz"
            | "/_termdeck/readyz"
            | SETUP_PATH
            | LOGIN_PATH
            | PAIRING_PATH
    )
}

async fn security_headers(request: Request, next: Next) -> Response {
    let versioned_asset = matches!(
        request.uri().path(),
        FRONTEND_CSS_PATH
            | FRONTEND_JS_PATH
            | CLIENT_FILES_PATH
            | ACCOUNTS_PATH
            | WORKSPACE_UI_PATH
            | TERMINAL_CSS_PATH
            | TERMINAL_JS_PATH
    );
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.insert(
        HeaderName::from_static("content-security-policy"),
        HeaderValue::from_static(CONTENT_SECURITY_POLICY),
    );
    headers.insert(
        HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        HeaderName::from_static("x-frame-options"),
        HeaderValue::from_static("DENY"),
    );
    headers.insert(
        HeaderName::from_static("cross-origin-opener-policy"),
        HeaderValue::from_static("same-origin"),
    );
    headers.insert(
        HeaderName::from_static("cross-origin-resource-policy"),
        HeaderValue::from_static("same-origin"),
    );
    headers.insert(
        HeaderName::from_static("referrer-policy"),
        // Chromium also strips the Origin on native POST forms under
        // no-referrer. Keep same-origin form provenance without disclosing
        // paths to other origins; the request/CSRF gates remain strict.
        HeaderValue::from_static("same-origin"),
    );
    headers.insert(
        HeaderName::from_static("permissions-policy"),
        HeaderValue::from_static("camera=(), microphone=(), geolocation=(), payment=()"),
    );
    headers.insert(
        header::CACHE_CONTROL,
        if versioned_asset {
            HeaderValue::from_static("public, max-age=86400, immutable")
        } else {
            HeaderValue::from_static("no-store, max-age=0")
        },
    );
    response
}

async fn homepage(
    State(state): State<AppState>,
    Extension(base_path): Extension<RequestBasePath>,
) -> Response {
    publication_response(&state.cache, |publication| {
        (
            "text/html; charset=utf-8",
            html_bytes_for_base(&publication.homepage_html, base_path),
        )
    })
}

async fn frontend_css() -> Response {
    text_response(
        StatusCode::OK,
        "text/css; charset=utf-8",
        Bytes::from_static(APP_CSS),
    )
}

async fn frontend_js() -> Response {
    text_response(
        StatusCode::OK,
        "text/javascript; charset=utf-8",
        Bytes::from_static(APP_JS),
    )
}

async fn client_files_js() -> Response {
    text_response(
        StatusCode::OK,
        "text/javascript; charset=utf-8",
        Bytes::from_static(CLIENT_FILES_JS),
    )
}

async fn accounts_js() -> Response {
    text_response(
        StatusCode::OK,
        "text/javascript; charset=utf-8",
        Bytes::from_static(ACCOUNTS_JS),
    )
}

async fn workspace_ui_js() -> Response {
    text_response(
        StatusCode::OK,
        "text/javascript; charset=utf-8",
        Bytes::from_static(WORKSPACE_UI_JS),
    )
}

async fn terminal_css() -> Response {
    text_response(
        StatusCode::OK,
        "text/css; charset=utf-8",
        Bytes::from_static(TERMINAL_CSS),
    )
}

async fn terminal_js() -> Response {
    text_response(
        StatusCode::OK,
        "text/javascript; charset=utf-8",
        Bytes::from_static(TERMINAL_JS),
    )
}

async fn setup_page(
    State(state): State<AppState>,
    Extension(base_path): Extension<RequestBasePath>,
) -> Response {
    let Some(auth) = state.auth.as_ref() else {
        return not_found_response(false);
    };
    match auth.password_configured() {
        Ok(true) => redirect_response(LOGIN_PATH),
        Ok(false) => password_page_response(StatusCode::OK, PasswordPage::Setup, None, base_path),
        Err(_error) => internal_error_response(),
    }
}

async fn login_page(
    State(state): State<AppState>,
    Extension(base_path): Extension<RequestBasePath>,
) -> Response {
    let Some(auth) = state.auth.as_ref() else {
        return not_found_response(false);
    };
    match auth.password_configured() {
        Ok(true) => password_page_response(StatusCode::OK, PasswordPage::Login, None, base_path),
        Ok(false) => redirect_response(SETUP_PATH),
        Err(_error) => internal_error_response(),
    }
}

async fn setup_submit(
    State(state): State<AppState>,
    Extension(base_path): Extension<RequestBasePath>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    password_submit(state, headers, body, PasswordPage::Setup, base_path).await
}

async fn login_submit(
    State(state): State<AppState>,
    Extension(base_path): Extension<RequestBasePath>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    password_submit(state, headers, body, PasswordPage::Login, base_path).await
}

async fn password_submit(
    state: AppState,
    headers: HeaderMap,
    body: Bytes,
    page: PasswordPage,
    base_path: RequestBasePath,
) -> Response {
    let Some(auth) = state.auth.as_ref() else {
        return not_found_response(false);
    };
    let configured = match auth.password_configured() {
        Ok(value) => value,
        Err(_error) => return internal_error_response(),
    };
    if page == PasswordPage::Setup && configured {
        return redirect_response(LOGIN_PATH);
    }
    if page == PasswordPage::Login && !configured {
        return redirect_response(SETUP_PATH);
    }
    if !form_content_type(&headers) {
        return password_page_response(
            StatusCode::BAD_REQUEST,
            page,
            Some("The submitted form was invalid."),
            base_path,
        );
    }
    let form = match parse_password_form(&body, page) {
        Ok(value) => value,
        Err(()) => {
            return password_page_response(
                StatusCode::BAD_REQUEST,
                page,
                Some("Enter a valid password."),
                base_path,
            );
        }
    };
    if page == PasswordPage::Setup
        && form
            .confirmation
            .as_ref()
            .is_none_or(|confirmation| confirmation.as_str() != form.password.as_str())
    {
        return password_page_response(
            StatusCode::BAD_REQUEST,
            page,
            Some("The two passwords do not match."),
            base_path,
        );
    }
    let permit = match Arc::clone(&state.password_slots).try_acquire_owned() {
        Ok(value) => value,
        Err(_) => return password_capacity_response(),
    };
    let operation_auth = Arc::clone(auth);
    let cookie_auth = Arc::clone(auth);
    let password = form.password;
    let device_label = form.device_label;
    let now_ms = unix_ms();
    let operation = tokio::task::spawn_blocking(move || {
        let _permit = permit;
        match page {
            PasswordPage::Setup => {
                operation_auth.setup_password(password.as_str(), device_label.as_str(), now_ms)
            }
            PasswordPage::Login => {
                operation_auth.login_password(password.as_str(), device_label.as_str(), now_ms)
            }
        }
    })
    .await;
    let session = match operation {
        Ok(Ok(value)) => value,
        Ok(Err(error)) => return password_error_response(error, page, base_path),
        Err(_error) => return internal_error_response(),
    };
    authenticated_session_redirect(cookie_auth.as_ref(), &session, "/")
}

fn authenticated_session_redirect(
    auth: &HttpAuth,
    session: &AuthenticatedSession,
    location: &'static str,
) -> Response {
    let cookies = match auth.session_cookie_headers(session, unix_ms()) {
        Ok(value) => value,
        Err(_error) => return internal_error_response(),
    };
    let mut response = redirect_response(location);
    for cookie in cookies {
        response.headers_mut().append(header::SET_COOKIE, cookie);
    }
    response
}

async fn pairing_page(
    State(state): State<AppState>,
    Extension(_base_path): Extension<RequestBasePath>,
) -> Response {
    let Some(auth) = state.auth.as_ref() else {
        return not_found_response(false);
    };
    match auth.password_configured() {
        Ok(true) => redirect_response(LOGIN_PATH),
        Ok(false) => redirect_response(SETUP_PATH),
        Err(_error) => internal_error_response(),
    }
}

async fn pairing_submit(
    State(state): State<AppState>,
    Extension(base_path): Extension<RequestBasePath>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let Some(auth) = state.auth.as_ref() else {
        return not_found_response(false);
    };
    if !form_content_type(&headers) {
        return bad_request_response("invalid pairing request");
    }
    let invitation_secret = match parse_pairing_form(&body) {
        Ok(value) => value,
        Err(()) => return bad_request_response("invalid pairing request"),
    };
    let session = match auth.pair(invitation_secret.as_str(), unix_ms()) {
        Ok(value) => value,
        Err(error) => return pairing_error_response(error, base_path),
    };
    let cookies = match auth.session_cookie_headers(&session, unix_ms()) {
        Ok(value) => value,
        Err(error) => return authentication_error_response(error, PAIRING_PATH),
    };
    let mut response = redirect_response("/");
    for cookie in cookies {
        response.headers_mut().append(header::SET_COOKIE, cookie);
    }
    response
}

async fn session_info(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
) -> Response {
    if state.auth.is_none() {
        return not_found_response(true);
    }
    let Some(Extension(context)) = context else {
        return internal_error_response();
    };
    json_response(
        StatusCode::OK,
        &SessionDto {
            schema: "termdeck.auth-session.v1",
            session_id: context.session_id().as_str(),
            device_id: context.device_id().as_str(),
            device_label: context.device_label(),
            created_at_unix_ms: context.created_at_ms(),
            expires_at_unix_ms: context.expires_at_ms(),
        },
    )
}

async fn logout(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
) -> Response {
    let Some(auth) = state.auth.as_ref() else {
        return not_found_response(true);
    };
    let Some(Extension(context)) = context else {
        return internal_error_response();
    };
    if let Err(error) = auth.revoke_session(context.session_id(), unix_ms()) {
        return authentication_error_response(error, LOGOUT_PATH);
    }
    let cookies = match auth.clearing_cookie_headers() {
        Ok(value) => value,
        Err(error) => return authentication_error_response(error, LOGOUT_PATH),
    };
    let mut response = json_response(
        StatusCode::OK,
        &MutationResultDto {
            ok: true,
            result: "session-revoked",
        },
    );
    for cookie in cookies {
        response.headers_mut().append(header::SET_COOKIE, cookie);
    }
    response
}

async fn devices(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
) -> Response {
    let Some(auth) = state.auth.as_ref() else {
        return not_found_response(true);
    };
    let Some(Extension(context)) = context else {
        return internal_error_response();
    };
    let records = match auth.devices() {
        Ok(value) => value,
        Err(error) => return authentication_error_response(error, DEVICES_PATH),
    };
    let devices = records
        .iter()
        .map(|record| DeviceDto {
            device_id: record.device_id().as_str(),
            label: record.label(),
            created_at_unix_ms: record.created_at_ms(),
            revoked_at_unix_ms: record.revoked_at_ms(),
            current: record.device_id() == context.device_id(),
        })
        .collect::<Vec<_>>();
    json_response(
        StatusCode::OK,
        &DevicesEnvelopeDto {
            schema: "termdeck.auth-devices.v1",
            devices,
        },
    )
}

async fn revoke_device(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath(device_id): AxumPath<String>,
) -> Response {
    let Some(auth) = state.auth.as_ref() else {
        return not_found_response(true);
    };
    let Some(Extension(context)) = context else {
        return internal_error_response();
    };
    let device_id = match AuthenticatedDeviceId::parse(device_id.as_str()) {
        Ok(value) => value,
        Err(_error) => return not_found_response(true),
    };
    let changed = match auth.revoke_device(&device_id, unix_ms()) {
        Ok(value) => value,
        Err(error) => return authentication_error_response(error, DEVICES_PATH),
    };
    if !changed {
        return not_found_response(true);
    }
    let self_revoked = &device_id == context.device_id();
    let mut response = json_response(
        StatusCode::OK,
        &MutationResultDto {
            ok: true,
            result: if self_revoked {
                "current-device-revoked"
            } else {
                "device-revoked"
            },
        },
    );
    if self_revoked {
        let cookies = match auth.clearing_cookie_headers() {
            Ok(value) => value,
            Err(error) => return authentication_error_response(error, DEVICES_PATH),
        };
        for cookie in cookies {
            response.headers_mut().append(header::SET_COOKIE, cookie);
        }
    }
    response
}

async fn window_page(
    State(state): State<AppState>,
    Extension(base_path): Extension<RequestBasePath>,
    AxumPath(slug): AxumPath<String>,
) -> Response {
    publication_response(&state.cache, |publication| {
        publication.windows.get(&slug).map_or_else(
            || {
                (
                    "text/html; charset=utf-8",
                    Bytes::from(render_not_found_page(&slug, base_path)),
                )
            },
            |window| {
                (
                    "text/html; charset=utf-8",
                    html_bytes_for_base(&window.html, base_path),
                )
            },
        )
    })
    .with_status_if_missing(&state.cache, &slug, false)
}

async fn api_snapshot(State(state): State<AppState>) -> Response {
    publication_response(&state.cache, |publication| {
        (
            "application/json; charset=utf-8",
            publication.full_json.clone(),
        )
    })
}

async fn api_window(State(state): State<AppState>, AxumPath(slug): AxumPath<String>) -> Response {
    publication_response(&state.cache, |publication| {
        publication.windows.get(&slug).map_or_else(
            || {
                (
                    "application/json; charset=utf-8",
                    Bytes::from_static(b"{\"error\":\"window-not-found\"}\n"),
                )
            },
            |window| ("application/json; charset=utf-8", window.json.clone()),
        )
    })
    .with_status_if_missing(&state.cache, &slug, true)
}

#[derive(Deserialize)]
struct TerminalControlRequest {
    #[serde(default)]
    takeover: bool,
}

#[derive(Deserialize)]
struct TerminalTextRequest {
    sequence: u64,
    text: String,
}

#[derive(Deserialize)]
struct TerminalKeyRequest {
    sequence: u64,
    key: String,
}

#[derive(Clone)]
struct ResolvedWindow {
    id: String,
    name: String,
}

#[derive(Clone)]
struct ResolvedPane {
    window_name: String,
    session_id: Option<String>,
    pane_title: String,
    cwd: String,
    tmux_target: String,
}

fn terminal_context(
    state: &AppState,
    context: Option<Extension<SessionContext>>,
) -> Result<(Arc<TerminalService>, SessionContext), Box<Response>> {
    let Some(terminal) = state.terminal.as_ref() else {
        return Err(Box::new(not_found_response(true)));
    };
    let Some(Extension(context)) = context else {
        return Err(Box::new(internal_error_response()));
    };
    Ok((Arc::clone(terminal), context))
}

fn controller_actor(context: &SessionContext) -> String {
    format!(
        "{}:{}",
        context.device_id().as_str(),
        context.session_id().as_str()
    )
}

fn stream_controller_actor(context: &SessionContext) -> Result<String, Box<Response>> {
    let mut nonce = [0_u8; 16];
    if getrandom::fill(&mut nonce).is_err() {
        return Err(Box::new(internal_error_response()));
    }
    let mut actor = controller_actor(context);
    actor.push_str(":ws-");
    for byte in nonce {
        use std::fmt::Write as _;
        if write!(&mut actor, "{byte:02x}").is_err() {
            return Err(Box::new(internal_error_response()));
        }
    }
    Ok(actor)
}

fn resolve_pane(
    cache: &SnapshotCache,
    slug: &str,
    pane_id: &str,
) -> Result<ResolvedPane, Box<Response>> {
    let publication = match cache.availability(unix_ms()) {
        CacheAvailability::Ready(value)
        | CacheAvailability::Degraded {
            snapshot: value, ..
        } => value,
        CacheAvailability::Expired => {
            return Err(Box::new(json_error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "snapshot-stale-expired",
            )));
        }
        CacheAvailability::Unavailable => {
            return Err(Box::new(json_error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "snapshot-unavailable",
            )));
        }
    };
    let Some(window) = publication
        .snapshot
        .windows()
        .iter()
        .find(|window| window.route().slug() == slug)
    else {
        return Err(Box::new(json_error_response(
            StatusCode::NOT_FOUND,
            "window-not-found",
        )));
    };
    let Some(pane) = window
        .panes()
        .iter()
        .find(|pane| pane.id().as_str() == pane_id)
    else {
        return Err(Box::new(json_error_response(
            StatusCode::NOT_FOUND,
            "pane-not-found",
        )));
    };
    let Some(tmux_target) = pane.tmux_target() else {
        return Err(Box::new(json_error_response(
            StatusCode::CONFLICT,
            "pane-has-no-tmux-target",
        )));
    };
    Ok(ResolvedPane {
        window_name: window.name().to_owned(),
        session_id: pane.session_id().map(|value| value.as_str().to_owned()),
        pane_title: pane.title().to_owned(),
        cwd: pane.cwd().to_owned(),
        tmux_target: tmux_target.to_owned(),
    })
}

fn resolve_window(cache: &SnapshotCache, slug: &str) -> Result<ResolvedWindow, Box<Response>> {
    let publication = match cache.availability(unix_ms()) {
        CacheAvailability::Ready(value)
        | CacheAvailability::Degraded {
            snapshot: value, ..
        } => value,
        CacheAvailability::Expired => {
            return Err(Box::new(json_error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "snapshot-stale-expired",
            )));
        }
        CacheAvailability::Unavailable => {
            return Err(Box::new(json_error_response(
                StatusCode::SERVICE_UNAVAILABLE,
                "snapshot-unavailable",
            )));
        }
    };
    let Some(window) = publication
        .snapshot
        .windows()
        .iter()
        .find(|window| window.route().slug() == slug)
    else {
        return Err(Box::new(json_error_response(
            StatusCode::NOT_FOUND,
            "window-not-found",
        )));
    };
    Ok(ResolvedWindow {
        id: window.id().as_str().to_owned(),
        name: window.name().to_owned(),
    })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NewTabRequest {
    cwd: String,
    name: Option<String>,
    command: Option<String>,
    #[serde(default)]
    pinned: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TabMutationRequest {
    action: String,
    name: Option<String>,
    confirmation: Option<String>,
}

fn tab_action_context(
    state: &AppState,
    context: Option<Extension<SessionContext>>,
) -> Result<Arc<TabActionService>, Box<Response>> {
    if context.is_none() {
        return Err(Box::new(internal_error_response()));
    }
    let Some(actions) = state.tab_actions.as_ref() else {
        return Err(Box::new(not_found_response(true)));
    };
    Ok(Arc::clone(actions))
}

async fn management(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    headers: HeaderMap,
    Json(mut request): Json<serde_json::Value>,
) -> Response {
    if request.get("_actor_session").is_some() {
        return json_response(
            StatusCode::BAD_REQUEST,
            &serde_json::json!({"ok":false,"error":"invalid request field"}),
        );
    }
    if request.get("operation").and_then(serde_json::Value::as_str) == Some("launch-options")
        || request.get("operation").and_then(serde_json::Value::as_str) == Some("create-window")
            && (request.get("workspace_id").is_some() || request.get("workspace_path").is_some())
    {
        let mut bridge = Request::new(Body::from(request.to_string()));
        *bridge.headers_mut() = headers;
        return staging_http::operate(State(state), context, bridge).await;
    }
    if matches!(
        request.get("operation").and_then(serde_json::Value::as_str),
        Some("create-window" | "switch-account" | "switch-all-accounts")
    ) {
        let valid_id = request
            .get("request_id")
            .and_then(serde_json::Value::as_str)
            .is_some_and(|s| {
                s.len() == 32
                    && s.bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            });
        if !valid_id {
            return json_response(
                StatusCode::BAD_REQUEST,
                &serde_json::json!({"ok":false,"error":"creation request ID required"}),
            );
        }
        let Some(Extension(ref session)) = context else {
            return forbidden_response("authentication is required");
        };
        request["_actor_session"] = session.session_id().as_str().into();
    }
    let actions = match tab_action_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    match actions.manage(request).await {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => tab_action_error_response(error),
    }
}

async fn tab_create(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath(slug): AxumPath<String>,
    Json(request): Json<NewTabRequest>,
) -> Response {
    let actions = match tab_action_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let window = match resolve_window(&state.cache, slug.as_str()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    match actions
        .create(
            window.id.as_str(),
            window.name.as_str(),
            request.cwd.as_str(),
            request.name.as_deref(),
            request.command.as_deref(),
            request.pinned,
        )
        .await
    {
        Ok(value) => json_response(StatusCode::CREATED, &value),
        Err(error) => tab_action_error_response(error),
    }
}

async fn tab_hidden(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath(slug): AxumPath<String>,
) -> Response {
    let actions = match tab_action_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let window = match resolve_window(&state.cache, &slug) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    match actions.hidden(&window.id).await {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => tab_action_error_response(error),
    }
}

async fn tab_restore(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, session_id)): AxumPath<(String, String)>,
) -> Response {
    let actions = match tab_action_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let window = match resolve_window(&state.cache, &slug) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    match actions.restore(&window.id, &window.name, &session_id).await {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => tab_action_error_response(error),
    }
}

async fn tab_mutate(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, pane_id)): AxumPath<(String, String)>,
    Json(request): Json<TabMutationRequest>,
) -> Response {
    let actions = match tab_action_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let pane = match resolve_pane(&state.cache, slug.as_str(), pane_id.as_str()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let Some(session_id) = pane.session_id.as_deref() else {
        return json_error_response(StatusCode::CONFLICT, "pane-has-no-session-identity");
    };
    if request.action == "rename" {
        let Some(name) = request.name.as_deref() else {
            return json_error_response(StatusCode::BAD_REQUEST, "tab-name-required");
        };
        return match actions.rename(session_id, name).await {
            Ok(value) => json_response(StatusCode::OK, &value),
            Err(error) => tab_action_error_response(error),
        };
    }
    let action = match ExistingTabAction::parse(request.action.as_str()) {
        Ok(value) => value,
        Err(error) => return tab_action_error_response(error),
    };
    match actions
        .existing(session_id, action, request.confirmation.as_deref())
        .await
    {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => tab_action_error_response(error),
    }
}

fn tab_action_error_response(error: TabActionError) -> Response {
    let (status, code) = match error {
        TabActionError::Busy => (StatusCode::SERVICE_UNAVAILABLE, "tab-action-busy"),
        TabActionError::InvalidAction => (StatusCode::BAD_REQUEST, "tab-action-invalid"),
        TabActionError::InvalidCommand => (StatusCode::BAD_REQUEST, "tab-command-invalid"),
        TabActionError::InvalidConfirmation => (StatusCode::CONFLICT, "tab-confirmation-invalid"),
        TabActionError::InvalidCwd => (StatusCode::BAD_REQUEST, "tab-cwd-invalid"),
        TabActionError::InvalidIdentity => (StatusCode::BAD_REQUEST, "tab-identity-invalid"),
        TabActionError::InvalidName => (StatusCode::BAD_REQUEST, "tab-name-invalid"),
        TabActionError::InvalidPath(_) => {
            (StatusCode::INTERNAL_SERVER_ERROR, "tab-service-invalid")
        }
        TabActionError::OutputTooLarge { .. } => {
            (StatusCode::BAD_GATEWAY, "tab-action-output-too-large")
        }
        TabActionError::ProcessFailed { .. } => (StatusCode::CONFLICT, "tab-action-failed"),
        TabActionError::Timeout => (StatusCode::GATEWAY_TIMEOUT, "tab-action-timeout"),
        TabActionError::Io(_) | TabActionError::Json(_) => {
            (StatusCode::INTERNAL_SERVER_ERROR, "tab-action-internal")
        }
    };
    let mut response = json_error_response(status, code);
    if status == StatusCode::SERVICE_UNAVAILABLE {
        response
            .headers_mut()
            .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
    }
    response
}

async fn terminal_page(
    State(state): State<AppState>,
    Extension(base_path): Extension<RequestBasePath>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, pane_id)): AxumPath<(String, String)>,
) -> Response {
    if terminal_context(&state, context).is_err() {
        return not_found_response(false);
    }
    let pane = match resolve_pane(&state.cache, slug.as_str(), pane_id.as_str()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let mut output = String::with_capacity(4_096);
    output.push_str("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,interactive-widget=resizes-content,viewport-fit=cover\"><meta name=\"theme-color\" content=\"#070a10\"><title>");
    escape_html_into(&mut output, pane.pane_title.as_str());
    output.push_str(" · t1000</title><link rel=\"stylesheet\" href=\"");
    output.push_str(FRONTEND_CSS_PATH);
    output.push_str("\"><link rel=\"stylesheet\" href=\"");
    output.push_str(TERMINAL_CSS_PATH);
    output.push_str("\"><script defer src=\"");
    output.push_str(TERMINAL_JS_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(CLIENT_FILES_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(ACCOUNTS_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(WORKSPACE_UI_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(FRONTEND_JS_PATH);
    output.push_str("\"></script></head><body class=\"terminal-page\"><main id=\"termdeck-app\" class=\"terminal-page-main\" data-termdeck-page=\"terminal\" data-termdeck-slug=\"");
    escape_html_into(&mut output, slug.as_str());
    output.push_str("\" data-termdeck-pane=\"");
    escape_html_into(&mut output, pane_id.as_str());
    output.push_str("\"><header class=\"terminal-page-header\"><nav><a href=\"/");
    escape_html_into(&mut output, slug.as_str());
    output.push_str("\">← ");
    escape_html_into(&mut output, pane.window_name.as_str());
    output.push_str("</a></nav><div class=\"terminal-page-identity\"><strong>");
    escape_html_into(&mut output, pane.pane_title.as_str());
    output.push_str("</strong><span class=\"path\">");
    escape_html_into(&mut output, pane.cwd.as_str());
    output.push_str("</span></div><span id=\"terminal-status\" class=\"terminal-status\" role=\"status\" aria-live=\"polite\">Connecting…</span></header><section id=\"terminal-shell\" class=\"xterm-shell\" aria-label=\"Interactive terminal\"><div id=\"terminal-xterm\" class=\"xterm-host\" data-termdeck-terminal=\"1\"></div><div id=\"terminal-overlay\" class=\"terminal-overlay\" hidden></div></section></main></body></html>");
    let output = html_for_base(prefix_html_template(output), base_path);
    if ensure_response_bound(output.len()).is_err() {
        return internal_error_response();
    }
    text_response(
        StatusCode::OK,
        "text/html; charset=utf-8",
        Bytes::from(output),
    )
}

fn websocket_protocol_allowed(headers: &HeaderMap) -> bool {
    headers
        .get(header::SEC_WEBSOCKET_PROTOCOL)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| {
            value
                .split(',')
                .any(|protocol| protocol.trim() == "termdeck.terminal.v1")
        })
}

async fn terminal_websocket(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, pane_id)): AxumPath<(String, String)>,
    headers: HeaderMap,
    websocket: WebSocketUpgrade,
) -> Response {
    if !websocket_protocol_allowed(&headers) {
        return json_error_response(StatusCode::BAD_REQUEST, "terminal-protocol-required");
    }
    let (terminal, context) = match terminal_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let pane = match resolve_pane(&state.cache, slug.as_str(), pane_id.as_str()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let actor = match stream_controller_actor(&context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    websocket
        .protocols(["termdeck.terminal.v1"])
        .on_upgrade(move |socket| {
            run_terminal_socket(
                socket,
                terminal,
                StreamPane {
                    pane_id,
                    tmux_target: pane.tmux_target,
                },
                actor,
            )
        })
}

async fn terminal_capture(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, pane_id)): AxumPath<(String, String)>,
) -> Response {
    let (terminal, _context) = match terminal_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let pane = match resolve_pane(&state.cache, slug.as_str(), pane_id.as_str()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    match terminal
        .capture(pane_id.as_str(), pane.tmux_target.as_str())
        .await
    {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => terminal_error_response(error),
    }
}

async fn terminal_control_status(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, pane_id)): AxumPath<(String, String)>,
) -> Response {
    let (terminal, context) = match terminal_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    if let Err(response) = resolve_pane(&state.cache, slug.as_str(), pane_id.as_str()) {
        return *response;
    }
    let actor = controller_actor(&context);
    match terminal
        .status(pane_id.as_str(), actor.as_str(), unix_ms())
        .await
    {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => terminal_error_response(error),
    }
}

async fn terminal_control_acquire(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, pane_id)): AxumPath<(String, String)>,
    Json(request): Json<TerminalControlRequest>,
) -> Response {
    let (terminal, context) = match terminal_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    if let Err(response) = resolve_pane(&state.cache, slug.as_str(), pane_id.as_str()) {
        return *response;
    }
    let actor = controller_actor(&context);
    match terminal
        .acquire(
            pane_id.as_str(),
            actor.as_str(),
            unix_ms(),
            request.takeover,
        )
        .await
    {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => terminal_error_response(error),
    }
}

async fn terminal_control_heartbeat(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, pane_id)): AxumPath<(String, String)>,
) -> Response {
    let (terminal, context) = match terminal_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    if let Err(response) = resolve_pane(&state.cache, slug.as_str(), pane_id.as_str()) {
        return *response;
    }
    let actor = controller_actor(&context);
    match terminal
        .heartbeat(pane_id.as_str(), actor.as_str(), unix_ms())
        .await
    {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => terminal_error_response(error),
    }
}

async fn terminal_control_release(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, pane_id)): AxumPath<(String, String)>,
) -> Response {
    let (terminal, context) = match terminal_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    if let Err(response) = resolve_pane(&state.cache, slug.as_str(), pane_id.as_str()) {
        return *response;
    }
    let actor = controller_actor(&context);
    match terminal
        .release(pane_id.as_str(), actor.as_str(), unix_ms())
        .await
    {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => terminal_error_response(error),
    }
}

async fn terminal_input_text(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, pane_id)): AxumPath<(String, String)>,
    Json(request): Json<TerminalTextRequest>,
) -> Response {
    let (terminal, context) = match terminal_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let pane = match resolve_pane(&state.cache, slug.as_str(), pane_id.as_str()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let actor = controller_actor(&context);
    match terminal
        .send_text(
            pane_id.as_str(),
            actor.as_str(),
            pane.tmux_target.as_str(),
            request.sequence,
            request.text.as_str(),
            unix_ms(),
        )
        .await
    {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => terminal_error_response(error),
    }
}

async fn terminal_input_key(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    AxumPath((slug, pane_id)): AxumPath<(String, String)>,
    Json(request): Json<TerminalKeyRequest>,
) -> Response {
    let (terminal, context) = match terminal_context(&state, context) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let pane = match resolve_pane(&state.cache, slug.as_str(), pane_id.as_str()) {
        Ok(value) => value,
        Err(response) => return *response,
    };
    let key = match TerminalKey::parse(request.key.as_str()) {
        Ok(value) => value,
        Err(error) => return terminal_error_response(error),
    };
    let actor = controller_actor(&context);
    match terminal
        .send_key(
            pane_id.as_str(),
            actor.as_str(),
            pane.tmux_target.as_str(),
            request.sequence,
            key,
            unix_ms(),
        )
        .await
    {
        Ok(value) => json_response(StatusCode::OK, &value),
        Err(error) => terminal_error_response(error),
    }
}

fn terminal_error_response(error: TerminalError) -> Response {
    match error {
        TerminalError::Busy => {
            let mut response =
                json_error_response(StatusCode::SERVICE_UNAVAILABLE, "terminal-capacity-reached");
            response
                .headers_mut()
                .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
            response
        }
        TerminalError::Control(termdeck_web_core::ControlError::PaneAlreadyControlled {
            ..
        }) => json_error_response(StatusCode::CONFLICT, "pane-already-controlled"),
        TerminalError::Control(_)
        | TerminalError::NotController
        | TerminalError::InvalidSequence { .. }
        | TerminalError::Indeterminate => {
            json_error_response(StatusCode::CONFLICT, "controller-state-conflict")
        }
        TerminalError::Id(_) | TerminalError::InvalidKey | TerminalError::InputTooLarge { .. } => {
            json_error_response(StatusCode::BAD_REQUEST, "invalid-terminal-request")
        }
        TerminalError::TmuxFailed(_) | TerminalError::TmuxTimeout => {
            json_error_response(StatusCode::BAD_GATEWAY, "tmux-operation-failed")
        }
        TerminalError::OutputTooLarge { .. } => {
            json_error_response(StatusCode::PAYLOAD_TOO_LARGE, "terminal-output-too-large")
        }
        TerminalError::Io(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::ConnectionRefused
            ) =>
        {
            json_error_response(StatusCode::SERVICE_UNAVAILABLE, "tmux-unavailable")
        }
        TerminalError::Utf8(_) | TerminalError::Io(_) | TerminalError::SequenceOverflow => {
            internal_error_response()
        }
    }
}

async fn healthz() -> Response {
    text_response(
        StatusCode::OK,
        "application/json; charset=utf-8",
        Bytes::from_static(b"{\"ok\":true,\"service\":\"termdeck-web\"}\n"),
    )
}

async fn readyz(State(state): State<AppState>) -> Response {
    let (label, status) = state.cache.readiness(unix_ms());
    let body = format!(
        "{{\"ok\":{},\"state\":\"{label}\"}}\n",
        status == StatusCode::OK
    );
    text_response(status, "application/json; charset=utf-8", Bytes::from(body))
}

async fn not_found(Extension(base_path): Extension<RequestBasePath>, method: Method) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return text_response(
            StatusCode::METHOD_NOT_ALLOWED,
            "text/plain; charset=utf-8",
            Bytes::from_static(b"method not allowed\n"),
        );
    }
    text_response(
        StatusCode::NOT_FOUND,
        "text/html; charset=utf-8",
        Bytes::from(render_not_found_page("", base_path)),
    )
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PasswordPage {
    Setup,
    Login,
}

struct PasswordForm {
    password: Zeroizing<String>,
    confirmation: Option<Zeroizing<String>>,
    device_label: String,
}

fn parse_password_form(body: &[u8], page: PasswordPage) -> Result<PasswordForm, ()> {
    if body.is_empty() || body.len() > MAX_AUTH_REQUEST_BYTES {
        return Err(());
    }
    let text = std::str::from_utf8(body).map_err(|_error| ())?;
    let mut password: Option<Zeroizing<String>> = None;
    let mut confirmation: Option<Zeroizing<String>> = None;
    let mut device_label: Option<String> = None;
    let mut fields = 0_usize;
    for field in text.split('&') {
        fields = fields.saturating_add(1);
        if fields > 3 {
            return Err(());
        }
        let (name, encoded) = field.split_once('=').ok_or(())?;
        let value = percent_decode_form(encoded)?;
        match name {
            "password" if password.is_none() => password = Some(value),
            "confirm" if page == PasswordPage::Setup && confirmation.is_none() => {
                confirmation = Some(value);
            }
            "label" if device_label.is_none() => {
                let label = value.as_str().trim();
                if !label.is_empty() {
                    if label.len() > MAX_DEVICE_LABEL_BYTES || label.chars().any(char::is_control) {
                        return Err(());
                    }
                    device_label = Some(label.to_owned());
                }
            }
            _ => return Err(()),
        }
    }
    let password = password.ok_or(())?;
    if password.len() > MAX_OWNER_PASSWORD_BYTES || password.chars().any(char::is_control) {
        return Err(());
    }
    if page == PasswordPage::Setup && password.len() < MIN_OWNER_PASSWORD_BYTES {
        return Err(());
    }
    if let Some(value) = confirmation.as_ref()
        && (value.len() > MAX_OWNER_PASSWORD_BYTES || value.chars().any(char::is_control))
    {
        return Err(());
    }
    if page == PasswordPage::Setup && confirmation.is_none() {
        return Err(());
    }
    Ok(PasswordForm {
        password,
        confirmation,
        device_label: device_label.unwrap_or_else(|| String::from("Browser")),
    })
}

fn password_page_response(
    status: StatusCode,
    page: PasswordPage,
    error: Option<&str>,
    base_path: RequestBasePath,
) -> Response {
    let (title, heading, description, action, autocomplete, confirmation) = match page {
        PasswordPage::Setup => (
            "Set t1000 password",
            "Choose your t1000 password",
            "This password signs in every browser you authorize on your LAN.",
            SETUP_PATH,
            "new-password",
            true,
        ),
        PasswordPage::Login => (
            "Sign in to t1000",
            "Sign in to t1000",
            "Enter the owner password to open this browser session.",
            LOGIN_PATH,
            "current-password",
            false,
        ),
    };
    let mut output = String::with_capacity(2_048);
    output.push_str("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\"><meta name=\"theme-color\" content=\"#090c12\"><title>");
    escape_html_into(&mut output, title);
    output.push_str("</title><link rel=\"stylesheet\" href=\"/_termdeck/assets/v7/app.css\"></head><body class=\"auth-page\"><main class=\"auth-main\"><p class=\"auth-brand\"><span class=\"auth-mark\" aria-hidden=\"true\">&gt;_</span>t1000</p><h1>");
    escape_html_into(&mut output, heading);
    output.push_str("</h1><p class=\"subtle\">");
    escape_html_into(&mut output, description);
    output.push_str("</p>");
    if let Some(error) = error {
        output.push_str("<p id=\"auth-error\" class=\"auth-error\" role=\"alert\"><strong>");
        escape_html_into(&mut output, error);
        output.push_str("</strong></p>");
    }
    output.push_str("<form method=\"post\" action=\"");
    escape_html_into(&mut output, action);
    output.push_str("\" autocomplete=\"on\"><label>Password<input name=\"password\" type=\"password\" required autofocus autocomplete=\"");
    escape_html_into(&mut output, autocomplete);
    output.push_str("\" maxlength=\"");
    output.push_str(MAX_OWNER_PASSWORD_BYTES.to_string().as_str());
    output.push('"');
    if error.is_some() {
        output.push_str(" aria-invalid=\"true\" aria-describedby=\"auth-error\"");
    }
    if page == PasswordPage::Setup {
        output.push_str(" minlength=\"");
        output.push_str(MIN_OWNER_PASSWORD_BYTES.to_string().as_str());
        output.push('"');
    }
    output.push_str("></label>");
    if confirmation {
        output.push_str("<label>Confirm password<input name=\"confirm\" type=\"password\" required autocomplete=\"new-password\" minlength=\"");
        output.push_str(MIN_OWNER_PASSWORD_BYTES.to_string().as_str());
        output.push_str("\" maxlength=\"");
        output.push_str(MAX_OWNER_PASSWORD_BYTES.to_string().as_str());
        output.push_str("\"></label>");
    }
    output.push_str("<button type=\"submit\">");
    output.push_str(if page == PasswordPage::Setup {
        "Set password and open t1000"
    } else {
        "Sign in"
    });
    output.push_str("</button></form></main></body></html>");
    let output = html_for_base(prefix_html_template(output), base_path);
    text_response(status, "text/html; charset=utf-8", Bytes::from(output))
}

fn password_capacity_response() -> Response {
    let mut response = json_error_response(StatusCode::SERVICE_UNAVAILABLE, "password-busy");
    response
        .headers_mut()
        .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
    response
}

fn password_error_response(
    error: HttpAuthError,
    page: PasswordPage,
    base_path: RequestBasePath,
) -> Response {
    if let Some(retry_after_ms) = error.retry_after_ms() {
        let mut response = password_page_response(
            StatusCode::TOO_MANY_REQUESTS,
            page,
            Some("Too many attempts. Try again shortly."),
            base_path,
        );
        let seconds = retry_after_ms.div_ceil(1_000).max(1);
        if let Ok(value) = HeaderValue::from_str(seconds.to_string().as_str()) {
            response.headers_mut().insert(header::RETRY_AFTER, value);
        }
        return response;
    }
    match error {
        HttpAuthError::InvalidPassword => password_page_response(
            StatusCode::BAD_REQUEST,
            page,
            Some("Use at least eight characters without control characters."),
            base_path,
        ),
        HttpAuthError::PasswordAlreadyConfigured => redirect_response(LOGIN_PATH),
        HttpAuthError::Unauthenticated => password_page_response(
            StatusCode::UNAUTHORIZED,
            PasswordPage::Login,
            Some("The password was not accepted."),
            base_path,
        ),
        HttpAuthError::RateLimited { .. } => password_capacity_response(),
        HttpAuthError::Auth(_)
        | HttpAuthError::CsrfRejected
        | HttpAuthError::ExpiredSession
        | HttpAuthError::InvalidConfiguration
        | HttpAuthError::LockPoisoned
        | HttpAuthError::MalformedCookie
        | HttpAuthError::MalformedHeader
        | HttpAuthError::Password(_)
        | HttpAuthError::Rate(_)
        | HttpAuthError::Secret(_) => internal_error_response(),
    }
}

fn render_pairing_page(base_path: RequestBasePath) -> String {
    let output = String::from(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\"><meta name=\"theme-color\" content=\"#090c12\"><title>Recovery pairing · t1000</title><link rel=\"stylesheet\" href=\"/_termdeck/assets/v7/app.css\"></head><body><main><p class=\"eyebrow\">t1000 recovery</p><h1>Recovery pairing</h1><p class=\"subtle\">Normal browser access uses the owner password. A locally generated recovery invitation may be entered here.</p><form method=\"post\" action=\"/_termdeck/pair\"><label>Recovery invitation<input name=\"invitation\" type=\"password\" required autocomplete=\"off\" maxlength=\"128\"></label><button type=\"submit\">Pair recovery device</button></form></main></body></html>",
    );
    html_for_base(prefix_html_template(output), base_path)
}

#[derive(Serialize)]
struct SessionDto<'a> {
    schema: &'static str,
    session_id: &'a str,
    device_id: &'a str,
    device_label: &'a str,
    created_at_unix_ms: u64,
    expires_at_unix_ms: u64,
}

#[derive(Serialize)]
struct DevicesEnvelopeDto<'a> {
    schema: &'static str,
    devices: Vec<DeviceDto<'a>>,
}

#[derive(Serialize)]
struct DeviceDto<'a> {
    device_id: &'a str,
    label: &'a str,
    created_at_unix_ms: u64,
    revoked_at_unix_ms: Option<u64>,
    current: bool,
}

#[derive(Serialize)]
struct MutationResultDto {
    ok: bool,
    result: &'static str,
}

fn form_content_type(headers: &HeaderMap) -> bool {
    single_header(headers, header::CONTENT_TYPE.as_str()).is_some_and(|value| {
        value.to_str().is_ok_and(|value| {
            value.split(';').next().is_some_and(|kind| {
                kind.trim()
                    .eq_ignore_ascii_case("application/x-www-form-urlencoded")
            })
        })
    })
}

fn parse_pairing_form(body: &[u8]) -> Result<Zeroizing<String>, ()> {
    if body.is_empty() || body.len() > MAX_AUTH_REQUEST_BYTES {
        return Err(());
    }
    let text = std::str::from_utf8(body).map_err(|_error| ())?;
    let mut invitation: Option<Zeroizing<String>> = None;
    for field in text.split('&') {
        let (name, value) = field.split_once('=').ok_or(())?;
        if name != "invitation" || invitation.is_some() {
            return Err(());
        }
        let value = percent_decode_form(value)?;
        if value.is_empty() || value.len() > 128 {
            return Err(());
        }
        invitation = Some(value);
    }
    invitation.ok_or(())
}

fn percent_decode_form(value: &str) -> Result<Zeroizing<String>, ()> {
    let mut bytes = Zeroizing::new(Vec::with_capacity(value.len()));
    let raw = value.as_bytes();
    let mut index = 0_usize;
    while index < raw.len() {
        match raw[index] {
            b'+' => {
                bytes.push(b' ');
                index = index.saturating_add(1);
            }
            b'%' => {
                if index.saturating_add(2) >= raw.len() {
                    return Err(());
                }
                let high = hex_value(raw[index + 1]).ok_or(())?;
                let low = hex_value(raw[index + 2]).ok_or(())?;
                bytes.push((high << 4) | low);
                index = index.saturating_add(3);
            }
            byte if byte.is_ascii() && !byte.is_ascii_control() => {
                bytes.push(byte);
                index = index.saturating_add(1);
            }
            _ => return Err(()),
        }
    }
    let decoded = String::from_utf8(bytes.to_vec()).map_err(|_error| ())?;
    Ok(Zeroizing::new(decoded))
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn unsafe_request_metadata_allowed(headers: &HeaderMap, secure: bool) -> bool {
    let Some(host) =
        single_header(headers, header::HOST.as_str()).and_then(|value| value.to_str().ok())
    else {
        return false;
    };
    let Some(origin) =
        single_header(headers, header::ORIGIN.as_str()).and_then(|value| value.to_str().ok())
    else {
        return false;
    };
    let expected_prefix = if secure { "https://" } else { "http://" };
    let Some(origin_authority) = origin.strip_prefix(expected_prefix) else {
        return false;
    };
    if origin_authority.contains('/') || !origin_authority.eq_ignore_ascii_case(host) {
        return false;
    }
    let fetch_site = single_header(headers, "sec-fetch-site").and_then(|value| value.to_str().ok());
    let fetch_mode = single_header(headers, "sec-fetch-mode").and_then(|value| value.to_str().ok());
    fetch_site == Some("same-origin")
        && matches!(fetch_mode, Some("navigate" | "cors" | "same-origin"))
}

fn pairing_error_response(error: HttpAuthError, base_path: RequestBasePath) -> Response {
    if let Some(retry_after_ms) = error.retry_after_ms() {
        let mut response = json_error_response(StatusCode::TOO_MANY_REQUESTS, "rate-limited");
        let seconds = retry_after_ms.div_ceil(1_000).max(1);
        if let Ok(value) = HeaderValue::from_str(seconds.to_string().as_str()) {
            response.headers_mut().insert(header::RETRY_AFTER, value);
        }
        return response;
    }
    match error {
        HttpAuthError::Unauthenticated
        | HttpAuthError::MalformedCookie
        | HttpAuthError::MalformedHeader
        | HttpAuthError::ExpiredSession
        | HttpAuthError::CsrfRejected => text_response(
            StatusCode::UNAUTHORIZED,
            "text/html; charset=utf-8",
            Bytes::from(render_pairing_page(base_path)),
        ),
        HttpAuthError::RateLimited { .. } => {
            json_error_response(StatusCode::TOO_MANY_REQUESTS, "rate-limited")
        }
        HttpAuthError::Auth(_)
        | HttpAuthError::InvalidConfiguration
        | HttpAuthError::InvalidPassword
        | HttpAuthError::LockPoisoned
        | HttpAuthError::Password(_)
        | HttpAuthError::PasswordAlreadyConfigured
        | HttpAuthError::Rate(_)
        | HttpAuthError::Secret(_) => internal_error_response(),
    }
}

fn authentication_error_response(error: HttpAuthError, path: &str) -> Response {
    if let Some(retry_after_ms) = error.retry_after_ms() {
        let mut response = json_error_response(StatusCode::TOO_MANY_REQUESTS, "rate-limited");
        let seconds = retry_after_ms.div_ceil(1_000).max(1);
        if let Ok(value) = HeaderValue::from_str(seconds.to_string().as_str()) {
            response.headers_mut().insert(header::RETRY_AFTER, value);
        }
        return response;
    }
    match error {
        HttpAuthError::CsrfRejected => forbidden_response("CSRF validation failed"),
        HttpAuthError::Unauthenticated
        | HttpAuthError::MalformedCookie
        | HttpAuthError::MalformedHeader
        | HttpAuthError::ExpiredSession => unauthorized_response(path),
        HttpAuthError::RateLimited { .. } => {
            json_error_response(StatusCode::TOO_MANY_REQUESTS, "rate-limited")
        }
        HttpAuthError::Auth(_)
        | HttpAuthError::InvalidConfiguration
        | HttpAuthError::InvalidPassword
        | HttpAuthError::LockPoisoned
        | HttpAuthError::Password(_)
        | HttpAuthError::PasswordAlreadyConfigured
        | HttpAuthError::Rate(_)
        | HttpAuthError::Secret(_) => internal_error_response(),
    }
}

fn unauthorized_response(path: &str) -> Response {
    if path.starts_with("/_termdeck/api/") {
        let mut response = json_error_response(StatusCode::UNAUTHORIZED, "authentication-required");
        response.headers_mut().insert(
            header::WWW_AUTHENTICATE,
            HeaderValue::from_static("TermDeckSession"),
        );
        response
    } else {
        redirect_response(LOGIN_PATH)
    }
}

fn forbidden_response(_detail: &str) -> Response {
    json_error_response(StatusCode::FORBIDDEN, "request-forbidden")
}

fn bad_request_response(_detail: &str) -> Response {
    json_error_response(StatusCode::BAD_REQUEST, "invalid-request")
}

fn internal_error_response() -> Response {
    json_error_response(StatusCode::INTERNAL_SERVER_ERROR, "internal-error")
}

fn not_found_response(json: bool) -> Response {
    if json {
        json_error_response(StatusCode::NOT_FOUND, "not-found")
    } else {
        text_response(
            StatusCode::NOT_FOUND,
            "text/html; charset=utf-8",
            Bytes::from(render_not_found_page("", RequestBasePath::default())),
        )
    }
}

fn prefix_html_template(mut output: String) -> String {
    let prefix = BASE_PATH_PLACEHOLDER;
    for marker in ["href=\"/", "src=\"/", "action=\"/"] {
        let replacement = format!("{}{prefix}/", &marker[..marker.len() - 1]);
        output = output.replace(marker, replacement.as_str());
    }
    output.replacen(
        "id=\"termdeck-app\"",
        format!("id=\"termdeck-app\" data-termdeck-base=\"{BASE_PATH_PLACEHOLDER}\"").as_str(),
        1,
    )
}

fn html_for_base(template: String, base_path: RequestBasePath) -> String {
    template.replace(BASE_PATH_PLACEHOLDER, base_path.as_str())
}

fn html_bytes_for_base(template: &Bytes, base_path: RequestBasePath) -> Bytes {
    match std::str::from_utf8(template.as_ref()) {
        Ok(value) => Bytes::from(value.replace(BASE_PATH_PLACEHOLDER, base_path.as_str())),
        Err(_) => template.clone(),
    }
}

fn redirect_response(location: &'static str) -> Response {
    let mut response = text_response(
        StatusCode::SEE_OTHER,
        "text/plain; charset=utf-8",
        Bytes::new(),
    );
    response
        .headers_mut()
        .insert(header::LOCATION, HeaderValue::from_static(location));
    response
}

fn json_error_response(status: StatusCode, error: &'static str) -> Response {
    let body = format!("{{\"error\":\"{error}\"}}\n");
    text_response(status, "application/json; charset=utf-8", Bytes::from(body))
}

fn json_response(status: StatusCode, value: &impl Serialize) -> Response {
    match serde_json::to_vec(value) {
        Ok(mut body) => {
            body.push(b'\n');
            text_response(status, "application/json; charset=utf-8", Bytes::from(body))
        }
        Err(_error) => internal_error_response(),
    }
}

fn single_header<'a>(headers: &'a HeaderMap, name: &str) -> Option<&'a HeaderValue> {
    let mut values = headers.get_all(name).iter();
    let first = values.next()?;
    if values.next().is_some() {
        return None;
    }
    Some(first)
}

fn host_is_allowed(headers: &HeaderMap) -> bool {
    let Some(host) =
        single_header(headers, header::HOST.as_str()).and_then(|value| value.to_str().ok())
    else {
        return false;
    };
    loopback_authority(host)
}

fn origin_matches_host_when_present(headers: &HeaderMap) -> bool {
    let Some(origin_header) = headers.get(header::ORIGIN) else {
        return true;
    };
    let Some(origin) =
        single_header(headers, header::ORIGIN.as_str()).and_then(|value| value.to_str().ok())
    else {
        return false;
    };
    let Some(host) =
        single_header(headers, header::HOST.as_str()).and_then(|value| value.to_str().ok())
    else {
        return false;
    };
    let Some(authority) = origin
        .strip_prefix("http://")
        .or_else(|| origin.strip_prefix("https://"))
    else {
        return false;
    };
    let _present = origin_header;
    !authority.contains('/')
        && authority.eq_ignore_ascii_case(host)
        && loopback_authority(authority)
}

fn loopback_authority(authority: &str) -> bool {
    if authority.is_empty()
        || authority
            .as_bytes()
            .iter()
            .any(|byte| byte.is_ascii_whitespace() || matches!(*byte, b'/' | b'@'))
    {
        return false;
    }
    let host = if let Some(rest) = authority.strip_prefix('[') {
        let Some((host, suffix)) = rest.split_once(']') else {
            return false;
        };
        if !suffix.is_empty() {
            let Some(port) = suffix.strip_prefix(':') else {
                return false;
            };
            if port.parse::<u16>().is_err() {
                return false;
            }
        }
        host
    } else if let Some((host, port)) = authority.rsplit_once(':') {
        if host.is_empty() || host.contains(':') || port.parse::<u16>().is_err() {
            return false;
        }
        host
    } else {
        authority
    };
    host.eq_ignore_ascii_case("localhost")
        || IpAddr::from_str(host).is_ok_and(|address| address.is_loopback())
}

trait MissingStatus {
    fn with_status_if_missing(self, cache: &SnapshotCache, slug: &str, json: bool) -> Response;
}

impl MissingStatus for Response {
    fn with_status_if_missing(
        mut self,
        cache: &SnapshotCache,
        slug: &str,
        _json: bool,
    ) -> Response {
        match cache.availability(unix_ms()) {
            CacheAvailability::Ready(publication)
            | CacheAvailability::Degraded {
                snapshot: publication,
                ..
            } => {
                if !publication.windows.contains_key(slug) {
                    *self.status_mut() = StatusCode::NOT_FOUND;
                }
            }
            CacheAvailability::Expired | CacheAvailability::Unavailable => {}
        }
        self
    }
}

fn publication_response(
    cache: &SnapshotCache,
    select: impl FnOnce(&RenderedSnapshot) -> (&'static str, Bytes),
) -> Response {
    match cache.availability(unix_ms()) {
        CacheAvailability::Ready(publication) => {
            let (content_type, body) = select(&publication);
            text_response(StatusCode::OK, content_type, body)
        }
        CacheAvailability::Degraded { snapshot, error } => {
            let (content_type, body) = select(&snapshot);
            let mut response = text_response(StatusCode::OK, content_type, body);
            response.headers_mut().insert(
                HeaderName::from_static("x-termdeck-stale"),
                HeaderValue::from_static("1"),
            );
            let _error_detail = error;
            response.headers_mut().insert(
                HeaderName::from_static("warning"),
                HeaderValue::from_static("199 termdeck \"snapshot refresh failed\""),
            );
            response
        }
        CacheAvailability::Expired => text_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "application/json; charset=utf-8",
            Bytes::from_static(b"{\"error\":\"snapshot-stale-expired\"}\n"),
        ),
        CacheAvailability::Unavailable => text_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "application/json; charset=utf-8",
            Bytes::from_static(b"{\"error\":\"snapshot-unavailable\"}\n"),
        ),
    }
}

fn text_response(status: StatusCode, content_type: &'static str, body: Bytes) -> Response {
    let mut response = Body::from(body).into_response();
    *response.status_mut() = status;
    let value = match HeaderValue::from_str(content_type) {
        Ok(value) => value,
        Err(_) => HeaderValue::from_static("application/octet-stream"),
    };
    response.headers_mut().insert(header::CONTENT_TYPE, value);
    response
}

#[derive(Serialize)]
struct HostDto<'a> {
    schema: &'static str,
    host_id: &'a str,
    owner_id: &'a str,
    revision: u64,
    revision_text: String,
    generated_at_unix_ms: u64,
    total_panes: usize,
    windows: Vec<WindowDto<'a>>,
}

#[derive(Serialize)]
struct WindowEnvelopeDto<'a> {
    schema: &'static str,
    revision: u64,
    revision_text: String,
    generated_at_unix_ms: u64,
    window: WindowDto<'a>,
}

#[derive(Serialize)]
struct WindowDto<'a> {
    id: &'a str,
    name: &'a str,
    slug: &'a str,
    path: String,
    active_pane_id: Option<&'a str>,
    panes: Vec<PaneDto<'a>>,
}

#[derive(Serialize)]
struct PaneDto<'a> {
    id: &'a str,
    session_id: Option<&'a str>,
    title: &'a str,
    cwd: &'a str,
    status: &'static str,
    pinned: bool,
    tmux_target: Option<&'a str>,
    controller: Option<ControllerDto<'a>>,
}

#[derive(Serialize)]
struct ControllerDto<'a> {
    device_id: &'a str,
    epoch: u64,
    expires_at_unix_ms: u64,
}

fn host_dto(snapshot: &HostSnapshot) -> HostDto<'_> {
    HostDto {
        schema: "termdeck.host-snapshot.v1",
        host_id: snapshot.host_id().as_str(),
        owner_id: snapshot.owner_id().as_str(),
        revision: snapshot.revision(),
        revision_text: snapshot.revision().to_string(),
        generated_at_unix_ms: snapshot.generated_at_unix_ms(),
        total_panes: snapshot.total_panes(),
        windows: snapshot.windows().iter().map(window_dto).collect(),
    }
}

fn window_dto(window: &WindowSnapshot) -> WindowDto<'_> {
    WindowDto {
        id: window.id().as_str(),
        name: window.name(),
        slug: window.route().slug(),
        path: window.route().path(),
        active_pane_id: window
            .active_pane_id()
            .map(termdeck_web_core::PaneId::as_str),
        panes: window.panes().iter().map(pane_dto).collect(),
    }
}

fn pane_dto(pane: &PaneSnapshot) -> PaneDto<'_> {
    PaneDto {
        id: pane.id().as_str(),
        session_id: pane.session_id().map(termdeck_web_core::SessionId::as_str),
        title: pane.title(),
        cwd: pane.cwd(),
        status: pane_status_name(pane.status()),
        pinned: pane.pinned(),
        tmux_target: pane.tmux_target(),
        controller: pane.controller().map(controller_dto),
    }
}

fn controller_dto(controller: &ControllerView) -> ControllerDto<'_> {
    ControllerDto {
        device_id: controller.device_id().as_str(),
        epoch: controller.epoch(),
        expires_at_unix_ms: controller.expires_at_unix_ms(),
    }
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

fn render_homepage(
    snapshot: &HostSnapshot,
    tmux_warning: Option<&str>,
) -> Result<String, ServerError> {
    let mut output = String::with_capacity(8_192 + snapshot.total_panes().saturating_mul(32));
    output.push_str("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\"><meta name=\"theme-color\" content=\"#090c12\"><title>t1000</title><link rel=\"stylesheet\" href=\"");
    output.push_str(FRONTEND_CSS_PATH);
    output.push_str("\"><script defer src=\"");
    output.push_str(CLIENT_FILES_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(ACCOUNTS_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(WORKSPACE_UI_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(FRONTEND_JS_PATH);
    output.push_str("\"></script></head><body><main id=\"termdeck-app\" aria-busy=\"false\" data-termdeck-page=\"home\"><header class=\"host-header\"><p class=\"eyebrow\">t1000 host</p><h1>Windows</h1><p class=\"subtle\">");
    output.push_str(&snapshot.windows().len().to_string());
    output.push_str(" windows · ");
    output.push_str(&snapshot.total_panes().to_string());
    output.push_str(" visible panes · revision ");
    output.push_str(&snapshot.revision().to_string());
    output.push_str(
        "</p><p class=\"identity\">Open any window to work with its live terminals.</p></header>",
    );
    if tmux_warning.is_some() {
        output.push_str("<div class=\"notice\">tmux inventory is temporarily unavailable; registry status is shown without claiming live-process certainty.</div>");
    }
    output.push_str("<section class=\"grid\" aria-label=\"t1000 windows\">");
    for window in snapshot.windows() {
        let running = window
            .panes()
            .iter()
            .filter(|pane| pane.status() == PaneStatus::Running)
            .count();
        output.push_str("<a class=\"card\" href=\"");
        output.push_str(window.route().path().as_str());
        output.push_str("\"><span class=\"card-title\">");
        escape_html_into(&mut output, window.name());
        output.push_str("</span><span class=\"card-meta\">");
        output.push_str(&window.panes().len().to_string());
        output.push_str(" panes · ");
        output.push_str(&running.to_string());
        output.push_str(" running</span><span class=\"identity\">Window ");
        escape_html_into(&mut output, window.id().as_str());
        output.push_str("</span><code>");
        output.push_str(window.route().path().as_str());
        output.push_str("</code></a>");
    }
    if snapshot.windows().is_empty() {
        output.push_str(
            "<div class=\"empty\">No remembered t1000 windows are currently available.</div>",
        );
    }
    output.push_str("</section><noscript><div class=\"notice\">Window navigation remains available; live refresh and terminal control require JavaScript.</div></noscript><footer class=\"footer-row\"><a class=\"footer-link\" href=\"/_termdeck/api/v1/snapshot\">Versioned JSON</a><p class=\"refresh-state\">Server-rendered snapshot</p></footer></main></body></html>");
    let output = prefix_html_template(output);
    ensure_response_bound(output.len())?;
    Ok(output)
}

fn render_window_page(
    snapshot: &HostSnapshot,
    window: &WindowSnapshot,
    tmux_warning: Option<&str>,
) -> Result<String, ServerError> {
    let mut output = String::with_capacity(8_192 + window.panes().len().saturating_mul(512));
    output.push_str("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\"><meta name=\"theme-color\" content=\"#090c12\"><title>");
    escape_html_into(&mut output, window.name());
    output.push_str(" · t1000</title><link rel=\"stylesheet\" href=\"");
    output.push_str(FRONTEND_CSS_PATH);
    output.push_str("\"><link rel=\"stylesheet\" href=\"");
    output.push_str(TERMINAL_CSS_PATH);
    output.push_str("\"><script defer src=\"");
    output.push_str(TERMINAL_JS_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(CLIENT_FILES_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(ACCOUNTS_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(WORKSPACE_UI_PATH);
    output.push_str("\"></script><script defer src=\"");
    output.push_str(FRONTEND_JS_PATH);
    output.push_str("\"></script></head><body><main id=\"termdeck-app\" aria-busy=\"false\" data-termdeck-page=\"window\" data-termdeck-slug=\"");
    escape_html_into(&mut output, window.route().slug());
    if let Some(initial_pane) = window
        .active_pane_id()
        .or_else(|| window.panes().first().map(PaneSnapshot::id))
    {
        output.push_str("\" data-termdeck-pane=\"");
        escape_html_into(&mut output, initial_pane.as_str());
    }
    output.push_str("\"><nav class=\"page-nav\"><a class=\"back-link\" href=\"/\">← All windows</a></nav><header class=\"host-header\"><p class=\"eyebrow\">t1000 window</p><h1>");
    escape_html_into(&mut output, window.name());
    output.push_str("</h1><p class=\"subtle\">");
    output.push_str(&window.panes().len().to_string());
    output.push_str(" visible panes · revision ");
    output.push_str(&snapshot.revision().to_string());
    output.push_str("</p><p class=\"identity\">Window ");
    escape_html_into(&mut output, window.id().as_str());
    output.push_str(" · ");
    output.push_str(window.route().path().as_str());
    output.push_str("</p></header>");
    if tmux_warning.is_some() {
        output.push_str("<div class=\"notice\">Live tmux observation is degraded; no process state has been changed.</div>");
    }
    output.push_str("<section class=\"panes\" aria-label=\"Terminal panes\">");
    if window.panes().is_empty() {
        output.push_str(
            "<div class=\"empty\">This remembered window currently has no visible panes.</div>",
        );
    }
    for pane in window.panes() {
        output.push_str("<a class=\"pane pane-link\" href=\"/");
        output.push_str(window.route().slug());
        output.push_str("/t/");
        append_percent_encoded_path_segment(&mut output, pane.id().as_str());
        output.push_str("\"><div class=\"pane-main\"><h2>");
        escape_html_into(&mut output, pane.title());
        output.push_str("</h2><p class=\"path\">");
        escape_html_into(&mut output, pane.cwd());
        output.push_str("</p><p class=\"identity\">Pane ");
        escape_html_into(&mut output, pane.id().as_str());
        if let Some(session_id) = pane.session_id() {
            output.push_str(" · session ");
            escape_html_into(&mut output, session_id.as_str());
        }
        output.push_str("</p></div><div class=\"pane-aside\"><span class=\"status status-");
        output.push_str(pane_status_name(pane.status()));
        output.push_str("\">");
        output.push_str(pane_status_name(pane.status()));
        output.push_str("</span><span class=\"mode\">Open terminal</span>");
        output.push_str("</div></a>");
    }
    output.push_str("</section><noscript><div class=\"notice\">Pane navigation remains available; live refresh and terminal control require JavaScript.</div></noscript><footer class=\"footer-row\"><a class=\"footer-link\" href=\"/_termdeck/api/v1/windows/");
    output.push_str(window.route().slug());
    output.push_str("\">Versioned JSON</a><p class=\"refresh-state\">Server-rendered snapshot</p></footer></main></body></html>");
    let output = prefix_html_template(output);
    ensure_response_bound(output.len())?;
    Ok(output)
}

fn render_not_found_page(slug: &str, base_path: RequestBasePath) -> String {
    let mut output = String::from(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\"><meta name=\"theme-color\" content=\"#090c12\"><title>Not found · t1000</title><link rel=\"stylesheet\" href=\"/_termdeck/assets/v7/app.css\"></head><body><main><h1>Window not found</h1><p class=\"subtle\">No mirrored t1000 window owns this route",
    );
    if !slug.is_empty() {
        output.push_str(": <code>/");
        escape_html_into(&mut output, slug);
        output.push_str("</code>");
    }
    output.push_str(".</p><p><a href=\"/\">Return to all windows</a></p></main></body></html>");
    html_for_base(prefix_html_template(output), base_path)
}

fn append_percent_encoded_path_segment(output: &mut String, value: &str) {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            output.push(char::from(byte));
        } else {
            output.push('%');
            output.push(char::from(HEX[usize::from(byte >> 4)]));
            output.push(char::from(HEX[usize::from(byte & 0x0f)]));
        }
    }
}

fn escape_html_into(output: &mut String, value: &str) {
    for character in value.chars() {
        match character {
            '&' => output.push_str("&amp;"),
            '<' => output.push_str("&lt;"),
            '>' => output.push_str("&gt;"),
            '"' => output.push_str("&quot;"),
            '\'' => output.push_str("&#39;"),
            _ => output.push(character),
        }
    }
}

fn ensure_response_bound(actual: usize) -> Result<(), ServerError> {
    if actual > MAX_RENDERED_RESPONSE_BYTES {
        return Err(ServerError::ResponseTooLarge {
            actual,
            maximum: MAX_RENDERED_RESPONSE_BYTES,
        });
    }
    Ok(())
}

#[derive(Debug)]
struct TmuxCandidate {
    session_name: String,
    pane_id: String,
    dead: bool,
    window_active: bool,
    pane_active: bool,
    current_path: String,
}

async fn load_tmux_inventory(
    executable: &Path,
    command_timeout: Duration,
) -> Result<TmuxInventory, ServerError> {
    let mut command = tmux_runtime::command(executable).await?;
    command
        .arg("list-panes")
        .arg("-a")
        .arg("-F")
        .arg("#{session_name}\t#{pane_id}\t#{pane_dead}\t#{window_active}\t#{pane_active}\t#{pane_current_path}")
        .kill_on_drop(true);
    let output = timeout(command_timeout, command.output())
        .await
        .map_err(|_| ServerError::TmuxTimeout)??;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr);
        return Err(ServerError::TmuxFailed(
            detail.trim().chars().take(512).collect(),
        ));
    }
    if output.stdout.len() > MAX_TMUX_OUTPUT_BYTES {
        return Err(ServerError::TmuxOutputTooLarge(output.stdout.len()));
    }
    let text = std::str::from_utf8(&output.stdout)
        .map_err(|_| ServerError::MalformedTmuxRow(String::from("non-UTF-8 output")))?;
    let mut grouped: BTreeMap<String, Vec<TmuxCandidate>> = BTreeMap::new();
    for (index, line) in text.lines().enumerate() {
        if index >= MAX_TMUX_ROWS {
            return Err(ServerError::TooManyTmuxRows(index + 1));
        }
        let mut fields = line.splitn(6, '\t');
        let candidate = TmuxCandidate {
            session_name: required_field(&mut fields, line)?.to_owned(),
            pane_id: required_field(&mut fields, line)?.to_owned(),
            dead: parse_tmux_bool(required_field(&mut fields, line)?, line)?,
            window_active: parse_tmux_bool(required_field(&mut fields, line)?, line)?,
            pane_active: parse_tmux_bool(required_field(&mut fields, line)?, line)?,
            current_path: required_field(&mut fields, line)?.to_owned(),
        };
        grouped
            .entry(candidate.session_name.clone())
            .or_default()
            .push(candidate);
    }
    let mut inventory = TmuxInventory::complete();
    for (session_name, mut candidates) in grouped {
        candidates.sort_by(|left, right| {
            (
                !left.window_active,
                !left.pane_active,
                left.pane_id.as_str(),
            )
                .cmp(&(
                    !right.window_active,
                    !right.pane_active,
                    right.pane_id.as_str(),
                ))
        });
        let Some(selected) = candidates.into_iter().next() else {
            continue;
        };
        inventory.insert(
            session_name,
            TmuxPaneState::new(
                termdeck_web_core::PaneId::new(selected.pane_id)?,
                selected.current_path,
                selected.dead,
            )?,
        )?;
    }
    Ok(inventory)
}

fn required_field<'a>(
    fields: &mut impl Iterator<Item = &'a str>,
    line: &str,
) -> Result<&'a str, ServerError> {
    fields
        .next()
        .ok_or_else(|| ServerError::MalformedTmuxRow(line.chars().take(256).collect()))
}

fn parse_tmux_bool(value: &str, line: &str) -> Result<bool, ServerError> {
    match value {
        "0" => Ok(false),
        "1" => Ok(true),
        _ => Err(ServerError::MalformedTmuxRow(
            line.chars().take(256).collect(),
        )),
    }
}

async fn shutdown_signal() {
    if let Err(error) = tokio::signal::ctrl_c().await {
        eprintln!("t1000 web shutdown signal failed: {error}");
    }
}

fn next_value(
    arguments: &mut impl Iterator<Item = OsString>,
    option: &'static str,
) -> Result<String, ServerError> {
    arguments
        .next()
        .ok_or(ServerError::MissingArgumentValue(option))?
        .into_string()
        .map_err(|_| ServerError::NonUtf8Argument)
}

fn parse_socket(value: &str) -> Result<SocketAddr, ServerError> {
    SocketAddr::from_str(value).map_err(|_| ServerError::InvalidSocket(value.to_owned()))
}

fn parse_u64(value: &str, option: &'static str) -> Result<u64, ServerError> {
    value.parse().map_err(|_| ServerError::InvalidNumber {
        option,
        value: value.to_owned(),
    })
}

fn parse_usize(value: &str, option: &'static str) -> Result<usize, ServerError> {
    value.parse().map_err(|_| ServerError::InvalidNumber {
        option,
        value: value.to_owned(),
    })
}

fn unix_ms() -> u64 {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO);
    u64::try_from(duration.as_millis()).unwrap_or(u64::MAX)
}

fn duration_ms(duration: Duration) -> Result<u64, ServerError> {
    u64::try_from(duration.as_millis()).map_err(|_| ServerError::NumericOverflow)
}

#[cfg(test)]
mod tests {
    use super::{
        APP_CSS, APP_JS, AppState, CLIENT_FILES_JS, CLIENT_FILES_PATH, CONTENT_SECURITY_POLICY,
        CacheAvailability, ConfigAction, EXTERNAL_BASE_PATH, FORWARDED_HOST_HEADER,
        FORWARDED_PREFIX_HEADER, FORWARDED_PROTO_HEADER, FRONTEND_CSS_PATH, FRONTEND_JS_PATH,
        MAX_FRONTEND_ASSET_BYTES, MAX_PASSWORD_OPERATIONS, RenderedSnapshot, ServerConfig,
        ServerError, SnapshotCache, TERMINAL_JS_PATH, WORKSPACE_UI_JS, WORKSPACE_UI_PATH,
        build_router, unix_ms,
    };
    use axum::body::{Body, to_bytes};
    use axum::http::{HeaderValue, Method, Request, StatusCode, header};
    use std::ffi::OsString;
    use std::path::PathBuf;
    use std::sync::Arc;
    use std::time::Duration;
    use termdeck_web_core::{
        HostId, HostSnapshot, OwnerId, PaneId, PaneSnapshot, PaneStatus, SessionId, WindowId,
        WindowRoute, WindowSnapshot,
    };
    use tokio::runtime::{Builder, Runtime};
    use tokio::sync::Semaphore;
    use tokio::task::JoinSet;
    use tower::ServiceExt;

    fn runtime() -> Runtime {
        match Builder::new_current_thread().enable_all().build() {
            Ok(value) => value,
            Err(error) => panic!("could not create test runtime: {error}"),
        }
    }

    fn id<T>(result: Result<T, termdeck_web_core::IdError>) -> T {
        match result {
            Ok(value) => value,
            Err(error) => panic!("invalid test id: {error}"),
        }
    }

    fn test_snapshot(generated_at_unix_ms: u64) -> HostSnapshot {
        test_snapshot_with_revision(generated_at_unix_ms, 42)
    }

    fn test_snapshot_with_revision(generated_at_unix_ms: u64, revision: u64) -> HostSnapshot {
        let window_id = id(WindowId::new("window-random"));
        let route = match WindowRoute::restored(window_id.clone(), "random") {
            Ok(value) => value,
            Err(error) => panic!("invalid test route: {error}"),
        };
        let pane = match PaneSnapshot::new(
            id(PaneId::new("session-one")),
            Some(id(SessionId::new("session-one"))),
            "Shell <unsafe>",
            "/tmp/&work",
            PaneStatus::Running,
            Some(String::from("%1")),
            None,
        ) {
            Ok(value) => value,
            Err(error) => panic!("invalid test pane: {error}"),
        };
        let window =
            match WindowSnapshot::new(window_id, "Random & Research", route, vec![pane], None) {
                Ok(value) => value,
                Err(error) => panic!("invalid test window: {error}"),
            };
        match HostSnapshot::new(
            id(HostId::new("host-test")),
            id(OwnerId::new("owner-test")),
            revision,
            generated_at_unix_ms,
            vec![window],
        ) {
            Ok(value) => value,
            Err(error) => panic!("invalid test snapshot: {error}"),
        }
    }

    fn state(max_stale_ms: u64, generated_at_unix_ms: u64) -> AppState {
        let cache = match SnapshotCache::new(Duration::from_millis(max_stale_ms)) {
            Ok(value) => value,
            Err(error) => panic!("could not create cache: {error}"),
        };
        let rendered = match RenderedSnapshot::new(test_snapshot(generated_at_unix_ms), None) {
            Ok(value) => value,
            Err(error) => panic!("could not render snapshot: {error}"),
        };
        if let Err(error) = cache.publish(rendered) {
            panic!("could not publish snapshot: {error}");
        }
        AppState {
            cache,
            request_slots: Arc::new(Semaphore::new(64)),
            password_slots: Arc::new(Semaphore::new(MAX_PASSWORD_OPERATIONS)),
            auth: None,
            terminal: None,
            tab_actions: None,
        }
    }

    fn request(method: Method, uri: &str, host: &str) -> Request<Body> {
        match Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, host)
            .body(Body::empty())
        {
            Ok(value) => value,
            Err(error) => panic!("could not build test request: {error}"),
        }
    }

    fn prefixed_request(method: Method, uri: &str) -> Request<Body> {
        let mut request = request(method, uri, "127.0.0.1:7391");
        request.headers_mut().insert(
            FORWARDED_PREFIX_HEADER,
            HeaderValue::from_static(EXTERNAL_BASE_PATH),
        );
        request
            .headers_mut()
            .insert(FORWARDED_PROTO_HEADER, HeaderValue::from_static("https"));
        request.headers_mut().insert(
            FORWARDED_HOST_HEADER,
            HeaderValue::from_static("maxchat.semiotech.org"),
        );
        request
    }

    async fn body_text(response: axum::response::Response) -> String {
        let bytes = match to_bytes(response.into_body(), 2 * 1024 * 1024).await {
            Ok(value) => value,
            Err(error) => panic!("could not read response body: {error}"),
        };
        match String::from_utf8(bytes.to_vec()) {
            Ok(value) => value,
            Err(error) => panic!("response was not UTF-8: {error}"),
        }
    }

    #[test]
    fn homepage_and_direct_window_route_render_escaped_state() {
        runtime().block_on(async {
            let router = build_router(state(30_000, unix_ms()));
            let home = match router
                .clone()
                .oneshot(request(Method::GET, "/", "127.0.0.1:7391"))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(home.status(), StatusCode::OK);
            assert_eq!(
                home.headers()
                    .get("content-security-policy")
                    .and_then(|value| value.to_str().ok()),
                Some(CONTENT_SECURITY_POLICY)
            );
            assert_eq!(
                home.headers()
                    .get("x-frame-options")
                    .and_then(|value| value.to_str().ok()),
                Some("DENY")
            );
            assert_eq!(
                home.headers()
                    .get("cross-origin-opener-policy")
                    .and_then(|value| value.to_str().ok()),
                Some("same-origin")
            );
            assert_eq!(
                home.headers()
                    .get("cross-origin-resource-policy")
                    .and_then(|value| value.to_str().ok()),
                Some("same-origin")
            );
            assert_eq!(
                home.headers()
                    .get("referrer-policy")
                    .and_then(|value| value.to_str().ok()),
                Some("same-origin")
            );
            assert_eq!(
                home.headers()
                    .get(header::CACHE_CONTROL)
                    .and_then(|value| value.to_str().ok()),
                Some("no-store, max-age=0")
            );
            assert_eq!(
                home.headers()
                    .get(header::CONTENT_TYPE)
                    .and_then(|value| value.to_str().ok()),
                Some("text/html; charset=utf-8")
            );
            let home_text = body_text(home).await;
            assert!(home_text.contains("<title>t1000</title>"));
            assert!(home_text.contains("t1000 host"));
            assert!(!home_text.contains("TermDeck"));
            assert!(home_text.contains("href=\"/random\""));
            assert!(home_text.contains("Random &amp; Research"));
            assert!(home_text.contains("id=\"termdeck-app\""));
            assert!(home_text.contains("data-termdeck-page=\"home\""));
            assert!(home_text.contains(FRONTEND_CSS_PATH));
            assert!(home_text.contains(FRONTEND_JS_PATH));
            assert!(home_text.contains("Open any window to work with its live terminals."));
            assert!(!home_text.contains("read-only host mirror"));
            assert!(!home_text.contains("<style>"));

            let window = match router
                .oneshot(request(Method::GET, "/random", "localhost:7391"))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(window.status(), StatusCode::OK);
            let window_text = body_text(window).await;
            assert!(window_text.contains(" · t1000</title>"));
            assert!(!window_text.contains("TermDeck"));
            assert!(window_text.contains("Shell &lt;unsafe&gt;"));
            assert!(window_text.contains("/tmp/&amp;work"));
            assert!(window_text.contains("data-termdeck-page=\"window\""));
            assert!(window_text.contains("data-termdeck-slug=\"random\""));
            assert!(window_text.contains("Pane session-one"));
            assert!(window_text.contains("href=\"/random/t/session-one\""));
            assert!(window_text.contains("Open terminal"));
            assert!(!window_text.contains("Viewer · read-only"));
            assert!(!window_text.contains("<style>"));
        });
    }

    #[test]
    fn trusted_forwarded_prefix_is_exact_and_rewrites_all_browser_paths() {
        runtime().block_on(async {
            let router = build_router(state(30_000, unix_ms()));
            let home = router
                .clone()
                .oneshot(prefixed_request(Method::GET, "/"))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(home.status(), StatusCode::OK);
            assert_eq!(
                home.headers()
                    .get("x-termdeck-base-path")
                    .and_then(|value| value.to_str().ok()),
                Some(EXTERNAL_BASE_PATH)
            );
            let home_text = body_text(home).await;
            assert!(home_text.contains(r#"data-termdeck-base="/t1000""#));
            assert!(home_text.contains(r#"href="/t1000/_termdeck/assets/v7/app.css""#));
            assert!(home_text.contains(r#"src="/t1000/_termdeck/assets/v7/app.js""#));
            assert!(home_text.contains(r#"href="/t1000/random""#));
            assert!(!home_text.contains("__TERMDECK_BASE_PATH__"));
            assert!(!home_text.contains(r#"href="/_termdeck/"#));

            let window = router
                .clone()
                .oneshot(prefixed_request(Method::GET, "/random"))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(window.status(), StatusCode::OK);
            let window_text = body_text(window).await;
            assert!(window_text.contains(r#"data-termdeck-base="/t1000""#));
            assert!(window_text.contains(r#"href="/t1000/random/t/session-one""#));
            assert!(window_text.contains(r#"href="/t1000/_termdeck/assets/v7/app.css""#));

            let asset = router
                .clone()
                .oneshot(prefixed_request(Method::GET, FRONTEND_CSS_PATH))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(asset.status(), StatusCode::OK);
            assert_eq!(body_text(asset).await.as_bytes(), APP_CSS);

            let mut untrusted_host = prefixed_request(Method::GET, "/");
            untrusted_host.headers_mut().insert(
                FORWARDED_HOST_HEADER,
                HeaderValue::from_static("evil.example"),
            );
            let untrusted_host = router
                .clone()
                .oneshot(untrusted_host)
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(untrusted_host.status(), StatusCode::FORBIDDEN);

            let mut untrusted_proto = prefixed_request(Method::GET, "/");
            untrusted_proto
                .headers_mut()
                .insert(FORWARDED_PROTO_HEADER, HeaderValue::from_static("http"));
            let untrusted_proto = router
                .clone()
                .oneshot(untrusted_proto)
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(untrusted_proto.status(), StatusCode::FORBIDDEN);

            let mut duplicate = prefixed_request(Method::GET, "/");
            duplicate.headers_mut().append(
                FORWARDED_PREFIX_HEADER,
                HeaderValue::from_static(EXTERNAL_BASE_PATH),
            );
            let duplicate = router
                .oneshot(duplicate)
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(duplicate.status(), StatusCode::BAD_REQUEST);
        });
    }

    #[test]
    fn frontend_assets_are_bounded_api_driven_and_sink_safe() {
        runtime().block_on(async {
            assert!(APP_CSS.len() <= MAX_FRONTEND_ASSET_BYTES);
            assert!(APP_JS.len() <= MAX_FRONTEND_ASSET_BYTES);
            assert!(WORKSPACE_UI_JS.len() <= MAX_FRONTEND_ASSET_BYTES);
            assert!(CLIENT_FILES_JS.len() <= MAX_FRONTEND_ASSET_BYTES);
            assert!(CONTENT_SECURITY_POLICY.contains("script-src 'self'"));
            assert!(CONTENT_SECURITY_POLICY.contains("style-src 'self' 'unsafe-inline'"));
            assert!(CONTENT_SECURITY_POLICY.contains("connect-src 'self'"));
            assert!(!CONTENT_SECURITY_POLICY.contains("script-src 'self' 'unsafe-inline'"));
            assert!(!CONTENT_SECURITY_POLICY.contains("unsafe-eval"));

            let router = build_router(state(30_000, unix_ms()));
            let css = match router
                .clone()
                .oneshot(request(Method::GET, FRONTEND_CSS_PATH, "127.0.0.1"))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(css.status(), StatusCode::OK);
            assert_eq!(
                css.headers()
                    .get(header::CONTENT_TYPE)
                    .and_then(|value| value.to_str().ok()),
                Some("text/css; charset=utf-8")
            );
            assert_eq!(
                css.headers()
                    .get(header::CACHE_CONTROL)
                    .and_then(|value| value.to_str().ok()),
                Some("public, max-age=86400, immutable")
            );
            let css_text = body_text(css).await;
            assert_eq!(css_text.as_bytes(), APP_CSS);

            let javascript = match router
                .clone()
                .oneshot(request(Method::GET, FRONTEND_JS_PATH, "localhost:7391"))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(javascript.status(), StatusCode::OK);
            assert_eq!(
                javascript
                    .headers()
                    .get(header::CONTENT_TYPE)
                    .and_then(|value| value.to_str().ok()),
                Some("text/javascript; charset=utf-8")
            );
            let script = body_text(javascript).await;
            assert_eq!(script.as_bytes(), APP_JS);
            let files = router
                .clone()
                .oneshot(request(Method::GET, CLIENT_FILES_PATH, "localhost:7391"))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(files.status(), StatusCode::OK);
            assert_eq!(
                files
                    .headers()
                    .get(header::CACHE_CONTROL)
                    .and_then(|v| v.to_str().ok()),
                Some("public, max-age=86400, immutable")
            );
            let files = body_text(files).await;
            assert_eq!(files.as_bytes(), CLIENT_FILES_JS);
            assert!(!files.contains("innerHTML"));
            assert!(!files.contains("eval("));
            let workspace = router
                .oneshot(request(Method::GET, WORKSPACE_UI_PATH, "localhost:7391"))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(workspace.status(), StatusCode::OK);
            assert_eq!(
                workspace
                    .headers()
                    .get(header::CACHE_CONTROL)
                    .and_then(|value| value.to_str().ok()),
                Some("public, max-age=86400, immutable")
            );
            let workspace = body_text(workspace).await;
            assert_eq!(workspace.as_bytes(), WORKSPACE_UI_JS);
            assert!(!workspace.contains("innerHTML"));
            assert!(!workspace.contains("eval("));
            assert!(script.contains("/_termdeck/api/v1/snapshot"));
            assert!(script.contains("/_termdeck/api/v1/windows/"));
            assert!(script.contains("textContent"));
            assert!(script.contains("replaceChildren"));
            assert!(script.contains("workspace-action-dialog"));
            assert!(script.contains("workspace-action-notice"));
            assert!(script.contains("workspace-statusbar"));
            assert!(!script.contains("window.confirm("));
            assert!(!script.contains("window.prompt("));
            assert!(script.contains("termdeck.terminal-control.v1"));
            assert!(script.contains("/_termdeck/ws/v1/windows/"));
            assert!(script.contains("new WebSocket"));
            assert!(script.contains("termdeck.terminal.v1"));
            assert!(script.contains("terminal-xterm"));
            assert!(script.contains("AbortController"));
            assert!(script.contains("revision_text"));
            assert!(!script.contains("/control/release"));
            assert!(!script.contains("/input/text"));
            assert!(!script.contains("/input/key"));
            assert!(!script.contains("Take control"));
            assert!(!script.contains("Release control"));
            assert!(!script.contains("Controller active"));
            assert!(!script.contains("typing switches control"));
            assert!(script.contains("Pane ${paneId} · connecting"));
            assert!(script.contains("workspace-headerbar"));
            assert!(script.contains("workspace-tab-close"));
            assert!(script.contains("Hide tab"));
            assert!(script.contains("Kill process…"));
            assert!(!script.contains("workspace-terminal-header"));
            assert!(!script.contains("Reconnecting in"));
            assert!(!script.contains("terminal-composer"));
            for forbidden in [
                "innerHTML",
                "outerHTML",
                "insertAdjacentHTML",
                "document.write",
                "eval(",
                "new Function",
                "localStorage",
                "sessionStorage",
            ] {
                assert!(
                    !script.contains(forbidden),
                    "forbidden frontend sink: {forbidden}"
                );
            }
        });
    }

    #[test]
    fn window_page_bootstraps_the_initial_terminal_before_api_refresh() {
        let rendered = match RenderedSnapshot::new(test_snapshot(unix_ms()), None) {
            Ok(value) => value,
            Err(error) => panic!("could not render initial-pane snapshot: {error}"),
        };
        let window = match rendered.windows.get("random") {
            Some(value) => value,
            None => panic!("rendered random window was missing"),
        };
        let html = match std::str::from_utf8(window.html.as_ref()) {
            Ok(value) => value,
            Err(error) => panic!("window HTML was not UTF-8: {error}"),
        };
        assert!(html.contains("data-termdeck-page=\"window\""));
        assert!(html.contains("data-termdeck-slug=\"random\""));
        assert!(html.contains("data-termdeck-pane=\"session-one\""));
        assert!(html.contains(TERMINAL_JS_PATH));
        assert!(html.contains(FRONTEND_JS_PATH));
    }

    #[test]
    fn revision_text_preserves_full_u64_for_javascript_clients() {
        let rendered =
            match RenderedSnapshot::new(test_snapshot_with_revision(unix_ms(), u64::MAX), None) {
                Ok(value) => value,
                Err(error) => panic!("could not render maximum revision: {error}"),
            };
        let host = match std::str::from_utf8(rendered.full_json.as_ref()) {
            Ok(value) => value,
            Err(error) => panic!("host JSON was not UTF-8: {error}"),
        };
        assert!(host.contains("\"revision\":18446744073709551615"));
        assert!(host.contains("\"revision_text\":\"18446744073709551615\""));
        let window = match rendered.windows.get("random") {
            Some(value) => value,
            None => panic!("missing rendered window"),
        };
        let window_json = match std::str::from_utf8(window.json.as_ref()) {
            Ok(value) => value,
            Err(error) => panic!("window JSON was not UTF-8: {error}"),
        };
        assert!(window_json.contains("\"revision\":18446744073709551615"));
        assert!(window_json.contains("\"revision_text\":\"18446744073709551615\""));
    }

    #[test]
    fn versioned_json_and_not_found_contracts_are_deterministic() {
        runtime().block_on(async {
            let router = build_router(state(30_000, unix_ms()));
            let snapshot = match router
                .clone()
                .oneshot(request(
                    Method::GET,
                    "/_termdeck/api/v1/snapshot",
                    "127.0.0.1",
                ))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(snapshot.status(), StatusCode::OK);
            let text = body_text(snapshot).await;
            assert!(text.contains("\"schema\":\"termdeck.host-snapshot.v1\""));
            assert!(text.contains("\"revision_text\":\"42\""));
            assert!(text.contains("\"path\":\"/random\""));
            for forbidden in [
                "\"command\"",
                "\"last_pid\"",
                "\"recovery_command\"",
                "\"recovery_cwd\"",
                "\"auth_scope_id\"",
                "\"codex_home\"",
                "\"selected_account\"",
                "\"pending_account\"",
                "\"recovery_recipe\"",
                "\"lease_id\"",
            ] {
                assert!(
                    !text.contains(forbidden),
                    "sensitive API field: {forbidden}"
                );
            }

            let missing = match router
                .clone()
                .oneshot(request(Method::GET, "/missing", "127.0.0.1"))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(missing.status(), StatusCode::NOT_FOUND);

            let internal = match router
                .oneshot(request(Method::GET, "/_termdeck/not-a-route", "127.0.0.1"))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(internal.status(), StatusCode::NOT_FOUND);
        });
    }

    #[test]
    fn host_origin_and_method_guards_fail_closed() {
        runtime().block_on(async {
            let router = build_router(state(30_000, unix_ms()));
            let foreign_host = match router
                .clone()
                .oneshot(request(Method::GET, "/", "termdeck.example"))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(foreign_host.status(), StatusCode::MISDIRECTED_REQUEST);

            let mut foreign_origin = request(Method::GET, "/", "127.0.0.1");
            foreign_origin.headers_mut().insert(
                header::ORIGIN,
                match "https://evil.example".parse() {
                    Ok(value) => value,
                    Err(error) => panic!("invalid test origin: {error}"),
                },
            );
            let foreign_origin = match router.clone().oneshot(foreign_origin).await {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(foreign_origin.status(), StatusCode::MISDIRECTED_REQUEST);

            let mutation = match router
                .oneshot(request(Method::POST, "/", "127.0.0.1"))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(mutation.status(), StatusCode::METHOD_NOT_ALLOWED);
        });
    }

    #[test]
    fn expired_stale_snapshot_is_not_served() {
        runtime().block_on(async {
            let now = unix_ms();
            let app_state = state(10, now.saturating_sub(100));
            let failed_at = now.saturating_sub(100);
            if let Err(error) = app_state
                .cache
                .record_error(String::from("source missing"), failed_at)
            {
                panic!("could not mark stale cache: {error}");
            }
            assert_eq!(app_state.cache.last_error_time(), Some(failed_at));
            assert!(matches!(
                app_state.cache.availability(now),
                CacheAvailability::Expired
            ));
            let response = match build_router(app_state)
                .oneshot(request(Method::GET, "/", "127.0.0.1"))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        });
    }

    #[test]
    fn repeated_failures_do_not_extend_the_stale_deadline() {
        let cache = match SnapshotCache::new(Duration::from_millis(10)) {
            Ok(value) => value,
            Err(error) => panic!("could not create cache: {error}"),
        };
        let rendered = match RenderedSnapshot::new(test_snapshot(90), None) {
            Ok(value) => value,
            Err(error) => panic!("could not render snapshot: {error}"),
        };
        if let Err(error) = cache.publish(rendered) {
            panic!("could not publish snapshot: {error}");
        }
        let first = match cache.record_error(String::from("first"), 100) {
            Ok(value) => value,
            Err(error) => panic!("could not record first failure: {error}"),
        };
        let repeated = match cache.record_error(String::from("second"), 109) {
            Ok(value) => value,
            Err(error) => panic!("could not record repeated failure: {error}"),
        };
        assert!(first);
        assert!(!repeated);
        assert_eq!(cache.last_error_time(), Some(100));
        assert!(matches!(
            cache.availability(111),
            CacheAvailability::Expired
        ));
    }

    #[test]
    fn many_viewers_do_not_create_control_state_or_unbounded_handlers() {
        runtime().block_on(async {
            let router = build_router(state(30_000, unix_ms()));
            let mut tasks = JoinSet::new();
            for _index in 0..64 {
                let clone = router.clone();
                tasks.spawn(async move {
                    clone
                        .oneshot(request(Method::GET, "/random", "127.0.0.1"))
                        .await
                });
            }
            let mut completed = 0usize;
            while let Some(result) = tasks.join_next().await {
                let response = match result {
                    Ok(Ok(value)) => value,
                    Ok(Err(error)) => match error {},
                    Err(error) => panic!("viewer task failed: {error}"),
                };
                assert_eq!(response.status(), StatusCode::OK);
                completed = completed.saturating_add(1);
            }
            assert_eq!(completed, 64);
        });
    }

    #[test]
    fn configuration_refuses_unauthenticated_lan_binding() {
        let result = ServerConfig::parse_with_home(
            [OsString::from("--listen"), OsString::from("0.0.0.0:7391")],
            PathBuf::from("/tmp/test-home"),
        );
        assert!(matches!(
            result,
            Err(ServerError::NonLoopbackRequiresAuthentication(_))
        ));
        let help = ServerConfig::parse_with_home(
            [OsString::from("--help")],
            PathBuf::from("/tmp/test-home"),
        );
        assert!(matches!(help, Ok(ConfigAction::Help)));
    }
}

#[cfg(test)]
mod auth_integration_tests {
    use super::auth_http::CSRF_HEADER_NAME;
    use super::{
        APP_CSS, AppState, ConfigAction, EXTERNAL_BASE_PATH, FORWARDED_HOST_HEADER,
        FORWARDED_PREFIX_HEADER, FORWARDED_PROTO_HEADER, HttpAuth, LOGIN_PATH,
        MAX_PASSWORD_OPERATIONS, PAIRING_PATH, RenderedSnapshot, SESSION_PATH, SETUP_PATH,
        ServerConfig, ServerError, SnapshotCache, TERMINAL_JS_PATH, build_router,
        ensure_auth_directory, loopback_authority, parse_pairing_form, unix_ms,
    };
    use axum::body::{Body, to_bytes};
    use axum::http::{HeaderValue, Method, Request, StatusCode, header};
    use std::ffi::OsString;
    use std::fs::{self, DirBuilder};
    use std::path::{Path, PathBuf};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::Duration;
    use termdeck_web_auth::{
        AUTH_DATABASE_FILENAME, AuthLimits, AuthStore, OWNER_PASSWORD_FILENAME,
    };
    use termdeck_web_core::{
        HostId, HostSnapshot, OwnerId, PaneId, PaneSnapshot, PaneStatus, SessionId, WindowId,
        WindowRoute, WindowSnapshot,
    };
    use tokio::runtime::{Builder, Runtime};
    use tokio::sync::Semaphore;
    use tower::ServiceExt;
    use zeroize::Zeroizing;

    #[cfg(unix)]
    use std::os::unix::fs::{DirBuilderExt, PermissionsExt};

    const TEST_HOST: &str = "127.0.0.1:7391";
    const TEST_AUTH_TTL_MS: u64 = 15 * 60_000;
    static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(1);

    struct TestDirectory(PathBuf);

    impl TestDirectory {
        fn new(label: &str) -> Self {
            let sequence = NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "termdeck-server-auth-{label}-{}-{sequence}",
                std::process::id()
            ));
            let mut builder = DirBuilder::new();
            #[cfg(unix)]
            builder.mode(0o700);
            if let Err(error) = builder.create(&path) {
                panic!("could not create HTTP auth test directory: {error}");
            }
            Self(path)
        }

        fn path(&self) -> &Path {
            self.0.as_path()
        }
    }

    impl Drop for TestDirectory {
        fn drop(&mut self) {
            let _result = fs::remove_dir_all(&self.0);
        }
    }

    struct AuthFixture {
        _directory: TestDirectory,
        state: AppState,
        invitations: Vec<Zeroizing<String>>,
    }

    #[derive(Clone)]
    struct CookieJar {
        header: HeaderValue,
        csrf: HeaderValue,
    }

    fn runtime() -> Runtime {
        match Builder::new_current_thread().enable_all().build() {
            Ok(value) => value,
            Err(error) => panic!("could not create HTTP auth test runtime: {error}"),
        }
    }

    fn id<T>(result: Result<T, termdeck_web_core::IdError>) -> T {
        match result {
            Ok(value) => value,
            Err(error) => panic!("invalid HTTP auth test identifier: {error}"),
        }
    }

    fn snapshot(generated_at_unix_ms: u64) -> HostSnapshot {
        let window_id = id(WindowId::new("window-random"));
        let route = WindowRoute::restored(window_id.clone(), "random")
            .unwrap_or_else(|error| panic!("invalid HTTP auth test route: {error}"));
        let pane = PaneSnapshot::new(
            id(PaneId::new("pane-one")),
            Some(id(SessionId::new("session-one"))),
            "Shell",
            "/tmp",
            PaneStatus::Running,
            Some(String::from("%1")),
            None,
        )
        .unwrap_or_else(|error| panic!("invalid HTTP auth test pane: {error}"));
        let window = WindowSnapshot::new(window_id, "Random", route, vec![pane], None)
            .unwrap_or_else(|error| panic!("invalid HTTP auth test window: {error}"));
        HostSnapshot::new(
            id(HostId::new("host-test")),
            id(OwnerId::new("owner-test")),
            1,
            generated_at_unix_ms,
            vec![window],
        )
        .unwrap_or_else(|error| panic!("invalid HTTP auth test snapshot: {error}"))
    }

    fn fixture(labels: &[&str]) -> AuthFixture {
        let directory = TestDirectory::new("fixture");
        let now_ms = unix_ms();
        let store = AuthStore::open_or_create(
            directory.path(),
            id(HostId::new("host-test")),
            id(OwnerId::new("owner-test")),
            now_ms,
            AuthLimits::default(),
        )
        .unwrap_or_else(|error| panic!("could not open HTTP auth fixture store: {error}"));
        let mut invitations = Vec::new();
        for label in labels {
            let invitation = store
                .create_invitation(label, now_ms, TEST_AUTH_TTL_MS)
                .unwrap_or_else(|error| panic!("could not create HTTP auth invitation: {error}"));
            invitations.push(Zeroizing::new(
                invitation.secret().expose_secret().to_owned(),
            ));
        }
        let auth = HttpAuth::new(store, TEST_AUTH_TTL_MS, false)
            .unwrap_or_else(|error| panic!("could not create HTTP auth fixture: {error}"));
        let cache = SnapshotCache::new(Duration::from_millis(30_000))
            .unwrap_or_else(|error| panic!("could not create HTTP auth cache: {error}"));
        let rendered = RenderedSnapshot::new(snapshot(now_ms), None)
            .unwrap_or_else(|error| panic!("could not render HTTP auth snapshot: {error}"));
        cache
            .publish(rendered)
            .unwrap_or_else(|error| panic!("could not publish HTTP auth snapshot: {error}"));
        AuthFixture {
            _directory: directory,
            state: AppState {
                cache,
                request_slots: Arc::new(Semaphore::new(64)),
                password_slots: Arc::new(Semaphore::new(MAX_PASSWORD_OPERATIONS)),
                auth: Some(Arc::new(auth)),
                terminal: None,
                tab_actions: None,
            },
            invitations,
        }
    }

    fn request(method: Method, uri: &str, body: Body) -> Request<Body> {
        Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, TEST_HOST)
            .body(body)
            .unwrap_or_else(|error| panic!("could not build HTTP auth request: {error}"))
    }

    fn with_external_prefix(mut request: Request<Body>) -> Request<Body> {
        request.headers_mut().insert(
            FORWARDED_PREFIX_HEADER,
            HeaderValue::from_static(EXTERNAL_BASE_PATH),
        );
        request
            .headers_mut()
            .insert(FORWARDED_PROTO_HEADER, HeaderValue::from_static("https"));
        request.headers_mut().insert(
            FORWARDED_HOST_HEADER,
            HeaderValue::from_static("maxchat.semiotech.org"),
        );
        request
    }

    fn authenticated_get(uri: &str, cookies: &CookieJar) -> Request<Body> {
        let mut request = request(Method::GET, uri, Body::empty());
        request
            .headers_mut()
            .insert(header::COOKIE, cookies.header.clone());
        request
    }

    fn unsafe_request(
        method: Method,
        uri: &str,
        body: Body,
        cookies: Option<&CookieJar>,
        csrf: bool,
    ) -> Request<Body> {
        let mut request = request(method, uri, body);
        let headers = request.headers_mut();
        headers.insert(
            header::ORIGIN,
            HeaderValue::from_static("http://127.0.0.1:7391"),
        );
        headers.insert("sec-fetch-site", HeaderValue::from_static("same-origin"));
        headers.insert("sec-fetch-mode", HeaderValue::from_static("cors"));
        if let Some(cookies) = cookies {
            headers.insert(header::COOKIE, cookies.header.clone());
            if csrf {
                headers.insert(CSRF_HEADER_NAME, cookies.csrf.clone());
            }
        }
        request
    }

    // Keep the double-submit cookie/header pair valid while changing only
    // whether the login exists or owns that CSRF capability.
    fn csrf_cookies(session: Option<&CookieJar>, csrf: &CookieJar) -> CookieJar {
        let csrf_value = csrf
            .csrf
            .to_str()
            .unwrap_or_else(|error| panic!("invalid fixture CSRF token: {error}"));
        let mut cookie = format!("termdeck_csrf={csrf_value}");
        if let Some(session) = session {
            let session_pair = session
                .header
                .to_str()
                .unwrap_or_else(|error| panic!("invalid fixture cookie: {error}"))
                .split("; ")
                .find(|pair| pair.starts_with("termdeck_session="))
                .unwrap_or_else(|| panic!("fixture session cookie missing"));
            cookie = format!("{session_pair}; {cookie}");
        }
        CookieJar {
            header: HeaderValue::from_str(&cookie)
                .unwrap_or_else(|error| panic!("invalid fixture cookie combination: {error}")),
            csrf: csrf.csrf.clone(),
        }
    }

    fn json_post(
        uri: &str,
        body: &serde_json::Value,
        cookies: &CookieJar,
        machine_key: Option<&str>,
    ) -> Request<Body> {
        let mut request = unsafe_request(
            Method::POST,
            uri,
            Body::from(body.to_string()),
            Some(cookies),
            true,
        );
        request.headers_mut().insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("application/json"),
        );
        if let Some(key) = machine_key {
            request.headers_mut().insert(
                super::machine_http::MACHINE_HEADER,
                HeaderValue::from_str(key)
                    .unwrap_or_else(|error| panic!("invalid fixture machine key: {error}")),
            );
        }
        request
    }

    async fn call(router: &axum::Router, request: Request<Body>) -> axum::response::Response {
        router
            .clone()
            .oneshot(request)
            .await
            .unwrap_or_else(|error| match error {})
    }

    async fn expect_json(
        response: axum::response::Response,
        status: StatusCode,
    ) -> serde_json::Value {
        assert_eq!(response.status(), status);
        serde_json::from_str(&body_text(response).await)
            .unwrap_or_else(|error| panic!("invalid HTTP fixture JSON: {error}"))
    }

    async fn assert_error(response: axum::response::Response, status: StatusCode, message: &str) {
        if status == StatusCode::UNAUTHORIZED {
            assert_eq!(
                response.headers().get(header::WWW_AUTHENTICATE),
                Some(&HeaderValue::from_static("TermDeckSession")),
            );
        }
        let value = expect_json(response, status).await;
        assert_eq!(value["error"], message);
    }

    fn expire_sessions(fixture: &AuthFixture) {
        fixture
            .state
            .auth
            .as_ref()
            .unwrap_or_else(|| panic!("fixture authentication missing"))
            .now_ms(unix_ms() + TEST_AUTH_TTL_MS + 1)
            .unwrap_or_else(|error| panic!("could not expire fixture sessions: {error}"));
    }

    async fn fresh_login(fixture: &AuthFixture, router: &axum::Router) -> CookieJar {
        let auth = fixture
            .state
            .auth
            .as_ref()
            .unwrap_or_else(|| panic!("fixture authentication missing"));
        let invitation = auth
            .machine_stores()
            .0
            .create_invitation("Fresh fixture login", unix_ms(), TEST_AUTH_TTL_MS)
            .unwrap_or_else(|error| panic!("could not create fresh fixture invitation: {error}"));
        pair(router, invitation.secret().expose_secret()).await
    }

    fn pairing_request(secret: &str) -> Request<Body> {
        let body = format!("invitation={secret}");
        let mut request = unsafe_request(Method::POST, PAIRING_PATH, Body::from(body), None, false);
        request.headers_mut().insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("application/x-www-form-urlencoded"),
        );
        request
    }

    fn password_request(
        path: &str,
        password: &str,
        confirmation: Option<&str>,
        label: &str,
    ) -> Request<Body> {
        let mut body = format!("password={password}&label={label}");
        if let Some(confirmation) = confirmation {
            body.push_str("&confirm=");
            body.push_str(confirmation);
        }
        password_form_request(path, body)
    }

    fn password_form_request(path: &str, body: String) -> Request<Body> {
        let mut request = unsafe_request(Method::POST, path, Body::from(body), None, false);
        request.headers_mut().insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("application/x-www-form-urlencoded"),
        );
        request
    }

    fn cookies_from_response(response: &axum::response::Response) -> CookieJar {
        let mut session_pair: Option<String> = None;
        let mut csrf_pair: Option<String> = None;
        for value in response.headers().get_all(header::SET_COOKIE).iter() {
            let text = value
                .to_str()
                .unwrap_or_else(|error| panic!("invalid HTTP auth Set-Cookie: {error}"));
            let pair = text
                .split(';')
                .next()
                .unwrap_or_else(|| panic!("missing HTTP auth cookie pair"));
            if pair.starts_with("termdeck_session=") {
                assert!(text.contains("HttpOnly"));
                assert!(text.contains("SameSite=Strict"));
                assert!(!text.contains("Domain="));
                session_pair = Some(pair.to_owned());
            } else if pair.starts_with("termdeck_csrf=") {
                assert!(!text.contains("HttpOnly"));
                assert!(text.contains("SameSite=Strict"));
                assert!(!text.contains("Domain="));
                csrf_pair = Some(pair.to_owned());
            }
        }
        let session_pair = session_pair.unwrap_or_else(|| panic!("missing session cookie"));
        let csrf_pair = csrf_pair.unwrap_or_else(|| panic!("missing CSRF cookie"));
        let csrf_value = csrf_pair
            .split_once('=')
            .map(|(_name, value)| value)
            .unwrap_or_else(|| panic!("invalid CSRF cookie pair"));
        CookieJar {
            header: HeaderValue::from_str(format!("{session_pair}; {csrf_pair}").as_str())
                .unwrap_or_else(|error| panic!("invalid combined HTTP auth cookie: {error}")),
            csrf: HeaderValue::from_str(csrf_value)
                .unwrap_or_else(|error| panic!("invalid HTTP auth CSRF value: {error}")),
        }
    }

    async fn body_text(response: axum::response::Response) -> String {
        let bytes = to_bytes(response.into_body(), 2 * 1024 * 1024)
            .await
            .unwrap_or_else(|error| panic!("could not read HTTP auth response: {error}"));
        String::from_utf8(bytes.to_vec())
            .unwrap_or_else(|error| panic!("HTTP auth response was not UTF-8: {error}"))
    }

    async fn pair(router: &axum::Router, secret: &str) -> CookieJar {
        let response = router
            .clone()
            .oneshot(pairing_request(secret))
            .await
            .unwrap_or_else(|error| match error {});
        assert_eq!(response.status(), StatusCode::SEE_OTHER);
        assert_eq!(
            response
                .headers()
                .get(header::LOCATION)
                .and_then(|value| value.to_str().ok()),
            Some("/")
        );
        cookies_from_response(&response)
    }

    #[test]
    fn disabled_auth_mode_preserves_read_only_routes_and_hides_auth_endpoints() {
        runtime().block_on(async {
            let cache = SnapshotCache::new(Duration::from_millis(30_000))
                .unwrap_or_else(|error| panic!("could not create disabled auth cache: {error}"));
            cache
                .publish(
                    RenderedSnapshot::new(snapshot(unix_ms()), None).unwrap_or_else(|error| {
                        panic!("could not render disabled auth state: {error}")
                    }),
                )
                .unwrap_or_else(|error| panic!("could not publish disabled auth state: {error}"));
            let router = build_router(AppState {
                cache,
                request_slots: Arc::new(Semaphore::new(64)),
                password_slots: Arc::new(Semaphore::new(MAX_PASSWORD_OPERATIONS)),
                auth: None,
                terminal: None,
                tab_actions: None,
            });
            let home = router
                .clone()
                .oneshot(request(Method::GET, "/", Body::empty()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(home.status(), StatusCode::OK);
            let pairing = router
                .clone()
                .oneshot(request(Method::GET, PAIRING_PATH, Body::empty()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(pairing.status(), StatusCode::NOT_FOUND);
            let session = router
                .oneshot(request(
                    Method::GET,
                    "/_termdeck/api/v1/session",
                    Body::empty(),
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(session.status(), StatusCode::NOT_FOUND);
        });
    }

    #[test]
    fn external_prefix_preserves_password_authentication_and_redirects() {
        runtime().block_on(async {
            let fixture = fixture(&[]);
            let router = build_router(fixture.state.clone());
            let password = "termdeck-test-owner-password-2026";

            let home = router
                .clone()
                .oneshot(with_external_prefix(request(
                    Method::GET,
                    "/",
                    Body::empty(),
                )))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(home.status(), StatusCode::SEE_OTHER);
            assert_eq!(
                home.headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok()),
                Some("/t1000/_termdeck/login")
            );

            let login = router
                .clone()
                .oneshot(with_external_prefix(request(
                    Method::GET,
                    LOGIN_PATH,
                    Body::empty(),
                )))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(login.status(), StatusCode::SEE_OTHER);
            assert_eq!(
                login
                    .headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok()),
                Some("/t1000/_termdeck/setup")
            );

            let setup_page = router
                .clone()
                .oneshot(with_external_prefix(request(
                    Method::GET,
                    SETUP_PATH,
                    Body::empty(),
                )))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(setup_page.status(), StatusCode::OK);
            let setup_text = body_text(setup_page).await;
            assert!(setup_text.contains(r#"href="/t1000/_termdeck/assets/v7/app.css""#));
            assert!(setup_text.contains(r#"action="/t1000/_termdeck/setup""#));
            assert!(!setup_text.contains("__TERMDECK_BASE_PATH__"));

            let setup = router
                .clone()
                .oneshot(with_external_prefix(password_request(
                    SETUP_PATH,
                    password,
                    Some(password),
                    "External browser",
                )))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(setup.status(), StatusCode::SEE_OTHER);
            assert_eq!(
                setup
                    .headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok()),
                Some("/t1000/")
            );
            for value in setup.headers().get_all(header::SET_COOKIE).iter() {
                let value = value
                    .to_str()
                    .unwrap_or_else(|error| panic!("invalid prefixed Set-Cookie: {error}"));
                assert!(value.contains("; Path=/t1000;"));
                assert!(!value.contains("; Path=/;"));
            }
            let cookies = cookies_from_response(&setup);

            let authenticated = router
                .oneshot(with_external_prefix(authenticated_get(
                    "/_termdeck/api/v1/snapshot",
                    &cookies,
                )))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(authenticated.status(), StatusCode::OK);
        });
    }

    #[test]
    fn password_only_setup_and_login_preserve_device_sessions() {
        runtime().block_on(async {
            let fixture = fixture(&[]);
            let router = build_router(fixture.state.clone());
            let password = "correct-password-123";
            let mut sessions = Vec::new();

            for (path, input_count, autocomplete, form) in [
                (
                    SETUP_PATH,
                    2,
                    "new-password",
                    format!("password={password}&confirm={password}"),
                ),
                (
                    LOGIN_PATH,
                    1,
                    "current-password",
                    format!("password={password}"),
                ),
            ] {
                let page = router
                    .clone()
                    .oneshot(request(Method::GET, path, Body::empty()))
                    .await
                    .unwrap_or_else(|error| match error {});
                assert_eq!(page.status(), StatusCode::OK);
                let html = body_text(page).await;
                assert_eq!(html.matches("<input ").count(), input_count);
                assert_eq!(html.matches(r#"type="password""#).count(), input_count);
                assert!(html.contains(format!(r#"autocomplete="{autocomplete}""#).as_str()));
                assert!(html.contains(r#"class="auth-page""#));
                assert!(html.contains("required autofocus"));
                assert!(!html.contains(r#"aria-invalid="true""#));
                assert!(!html.contains("Device name"));
                assert!(!html.contains(r#"name="label""#));

                let response = router
                    .clone()
                    .oneshot(password_form_request(path, form))
                    .await
                    .unwrap_or_else(|error| match error {});
                assert_eq!(response.status(), StatusCode::SEE_OTHER);
                sessions.push(cookies_from_response(&response));
            }

            // An automatic display label must not merge or revoke browser sessions.
            assert_ne!(sessions[0].header, sessions[1].header);
            for cookies in &sessions {
                let session = router
                    .clone()
                    .oneshot(authenticated_get(SESSION_PATH, cookies))
                    .await
                    .unwrap_or_else(|error| match error {});
                assert_eq!(session.status(), StatusCode::OK);
                assert!(
                    body_text(session)
                        .await
                        .contains(r#""device_label":"Browser""#)
                );
            }

            for (form, status) in [
                (String::new(), StatusCode::BAD_REQUEST),
                (
                    String::from("password=wrong-password"),
                    StatusCode::UNAUTHORIZED,
                ),
            ] {
                let response = router
                    .clone()
                    .oneshot(password_form_request(LOGIN_PATH, form))
                    .await
                    .unwrap_or_else(|error| match error {});
                assert_eq!(response.status(), status);
                assert!(!response.headers().contains_key(header::SET_COOKIE));
                let html = body_text(response).await;
                assert_eq!(html.matches("<input ").count(), 1);
                assert!(html.contains(r#"aria-invalid="true" aria-describedby="auth-error""#));
                assert!(html.contains(r#"class="auth-error" role="alert""#));
                assert!(!html.to_ascii_lowercase().contains("device name"));
                assert!(!html.contains(r#"name="label""#));
            }
        });
    }

    #[test]
    fn password_setup_login_logout_and_capacity_are_fail_closed() {
        runtime().block_on(async {
            let fixture = fixture(&[]);
            let router = build_router(fixture.state.clone());
            let password = "correct-password-123";

            let unauthenticated_home = router
                .clone()
                .oneshot(request(Method::GET, "/", Body::empty()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(unauthenticated_home.status(), StatusCode::SEE_OTHER);
            assert_eq!(
                unauthenticated_home
                    .headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok()),
                Some(LOGIN_PATH)
            );

            let login_before_setup = router
                .clone()
                .oneshot(request(Method::GET, LOGIN_PATH, Body::empty()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(login_before_setup.status(), StatusCode::SEE_OTHER);
            assert_eq!(
                login_before_setup
                    .headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok()),
                Some(SETUP_PATH)
            );

            let setup_page = router
                .clone()
                .oneshot(request(Method::GET, SETUP_PATH, Body::empty()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(setup_page.status(), StatusCode::OK);
            let setup_text = body_text(setup_page).await;
            assert!(setup_text.contains("Choose your t1000 password"));
            assert!(!setup_text.contains("invitation"));

            let mismatch = router
                .clone()
                .oneshot(password_request(
                    SETUP_PATH,
                    password,
                    Some("different-password"),
                    "Laptop",
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(mismatch.status(), StatusCode::BAD_REQUEST);

            let setup = router
                .clone()
                .oneshot(password_request(
                    SETUP_PATH,
                    password,
                    Some(password),
                    "Laptop",
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(setup.status(), StatusCode::SEE_OTHER);
            assert_eq!(
                setup
                    .headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok()),
                Some("/")
            );
            let first_session = cookies_from_response(&setup);

            let password_path = fixture._directory.path().join(OWNER_PASSWORD_FILENAME);
            let auth_path = fixture._directory.path().join(AUTH_DATABASE_FILENAME);
            let password_bytes = fs::read(&password_path)
                .unwrap_or_else(|error| panic!("could not read owner password record: {error}"));
            let auth_bytes = fs::read(&auth_path)
                .unwrap_or_else(|error| panic!("could not read authentication database: {error}"));
            assert!(
                !password_bytes
                    .windows(password.len())
                    .any(|window| window == password.as_bytes())
            );
            assert!(
                !auth_bytes
                    .windows(password.len())
                    .any(|window| window == password.as_bytes())
            );

            let authenticated_snapshot = router
                .clone()
                .oneshot(authenticated_get(
                    "/_termdeck/api/v1/snapshot",
                    &first_session,
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(authenticated_snapshot.status(), StatusCode::OK);

            let setup_after_configuration = router
                .clone()
                .oneshot(request(Method::GET, SETUP_PATH, Body::empty()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(setup_after_configuration.status(), StatusCode::SEE_OTHER);
            assert_eq!(
                setup_after_configuration
                    .headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok()),
                Some(LOGIN_PATH)
            );

            let pairing_after_configuration = router
                .clone()
                .oneshot(request(Method::GET, PAIRING_PATH, Body::empty()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(pairing_after_configuration.status(), StatusCode::SEE_OTHER);
            assert_eq!(
                pairing_after_configuration
                    .headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok()),
                Some(LOGIN_PATH)
            );

            let logout = router
                .clone()
                .oneshot(unsafe_request(
                    Method::POST,
                    "/_termdeck/api/v1/logout",
                    Body::empty(),
                    Some(&first_session),
                    true,
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(logout.status(), StatusCode::OK);

            let wrong_password = router
                .clone()
                .oneshot(password_request(
                    LOGIN_PATH,
                    "wrong-password",
                    None,
                    "Laptop2",
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(wrong_password.status(), StatusCode::UNAUTHORIZED);
            assert!(
                body_text(wrong_password)
                    .await
                    .contains("The password was not accepted")
            );

            let held = Arc::clone(&fixture.state.password_slots)
                .acquire_owned()
                .await
                .unwrap_or_else(|error| panic!("could not hold password semaphore: {error}"));
            let capacity = router
                .clone()
                .oneshot(password_request(LOGIN_PATH, password, None, "Laptop2"))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(capacity.status(), StatusCode::SERVICE_UNAVAILABLE);
            drop(held);

            let login = router
                .clone()
                .oneshot(password_request(LOGIN_PATH, password, None, "Laptop2"))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(login.status(), StatusCode::SEE_OTHER);
            let second_session = cookies_from_response(&login);
            let session = router
                .clone()
                .oneshot(authenticated_get(SESSION_PATH, &second_session))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(session.status(), StatusCode::OK);
            assert!(
                body_text(session)
                    .await
                    .contains("\"device_label\":\"Laptop2\"")
            );
        });
    }

    #[test]
    fn pairing_protects_state_and_logout_requires_exact_csrf_metadata() {
        runtime().block_on(async {
            let fixture = fixture(&["Laptop"]);
            let router = build_router(fixture.state.clone());
            let unauthenticated_home = router
                .clone()
                .oneshot(request(Method::GET, "/", Body::empty()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(unauthenticated_home.status(), StatusCode::SEE_OTHER);
            assert_eq!(
                unauthenticated_home
                    .headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok()),
                Some(LOGIN_PATH)
            );
            let unauthenticated_api = router
                .clone()
                .oneshot(request(
                    Method::GET,
                    "/_termdeck/api/v1/snapshot",
                    Body::empty(),
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(unauthenticated_api.status(), StatusCode::UNAUTHORIZED);
            assert!(
                unauthenticated_api
                    .headers()
                    .contains_key(header::WWW_AUTHENTICATE)
            );
            let asset = router
                .clone()
                .oneshot(request(
                    Method::GET,
                    "/_termdeck/assets/v7/app.css",
                    Body::empty(),
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(asset.status(), StatusCode::OK);
            assert_eq!(body_text(asset).await.as_bytes(), APP_CSS);
            let terminal_asset = router
                .clone()
                .oneshot(request(Method::GET, TERMINAL_JS_PATH, Body::empty()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(terminal_asset.status(), StatusCode::OK);
            assert_eq!(
                terminal_asset
                    .headers()
                    .get(header::CACHE_CONTROL)
                    .and_then(|value| value.to_str().ok()),
                Some("public, max-age=86400, immutable")
            );
            let pairing_page = router
                .clone()
                .oneshot(request(Method::GET, PAIRING_PATH, Body::empty()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(pairing_page.status(), StatusCode::SEE_OTHER);
            assert_eq!(
                pairing_page
                    .headers()
                    .get(header::LOCATION)
                    .and_then(|value| value.to_str().ok()),
                Some(SETUP_PATH)
            );

            let mut missing_metadata = request(
                Method::POST,
                PAIRING_PATH,
                Body::from(format!("invitation={}", fixture.invitations[0].as_str())),
            );
            missing_metadata.headers_mut().insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/x-www-form-urlencoded"),
            );
            let missing_metadata = router
                .clone()
                .oneshot(missing_metadata)
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(missing_metadata.status(), StatusCode::FORBIDDEN);

            let cookies = pair(&router, fixture.invitations[0].as_str()).await;
            let replay = router
                .clone()
                .oneshot(pairing_request(fixture.invitations[0].as_str()))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(replay.status(), StatusCode::UNAUTHORIZED);

            let home = router
                .clone()
                .oneshot(authenticated_get("/", &cookies))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(home.status(), StatusCode::OK);
            let session = router
                .clone()
                .oneshot(authenticated_get("/_termdeck/api/v1/session", &cookies))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(session.status(), StatusCode::OK);
            let session_text = body_text(session).await;
            assert!(session_text.contains("\"device_label\":\"Laptop\""));
            assert!(!session_text.contains("tds1_"));
            assert!(!session_text.contains("tdc1_"));

            let missing_csrf = router
                .clone()
                .oneshot(unsafe_request(
                    Method::POST,
                    "/_termdeck/api/v1/logout",
                    Body::empty(),
                    Some(&cookies),
                    false,
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(missing_csrf.status(), StatusCode::FORBIDDEN);

            let mut wrong_origin = unsafe_request(
                Method::POST,
                "/_termdeck/api/v1/logout",
                Body::empty(),
                Some(&cookies),
                true,
            );
            wrong_origin.headers_mut().insert(
                header::ORIGIN,
                HeaderValue::from_static("http://localhost:7391"),
            );
            let wrong_origin = router
                .clone()
                .oneshot(wrong_origin)
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(wrong_origin.status(), StatusCode::MISDIRECTED_REQUEST);

            let logout = router
                .clone()
                .oneshot(unsafe_request(
                    Method::POST,
                    "/_termdeck/api/v1/logout",
                    Body::empty(),
                    Some(&cookies),
                    true,
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(logout.status(), StatusCode::OK);
            assert!(
                logout
                    .headers()
                    .get_all(header::SET_COOKIE)
                    .iter()
                    .all(|value| value.to_str().is_ok_and(|text| text.contains("Max-Age=0")))
            );
            let revoked = router
                .oneshot(authenticated_get("/_termdeck/api/v1/session", &cookies))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(revoked.status(), StatusCode::UNAUTHORIZED);
        });
    }

    #[test]
    fn machine_routes_require_login_csrf_and_separate_enrollment() {
        runtime().block_on(async {
            use crate::machine_http;
            use termdeck_web_auth::{SecretKind, SecretToken};
            const PATH: &str = "/_termdeck/api/v1/machines";
            const CURRENT: &str = "/_termdeck/api/v1/machines/current";
            let fixture = fixture(&["Browser A", "Browser B"]);
            let router = build_router(fixture.state.clone());
            let a = pair(&router, fixture.invitations[0].as_str()).await;
            let b = pair(&router, fixture.invitations[1].as_str()).await;
            let key = SecretToken::generate(SecretKind::Machine).unwrap_or_else(|e| panic!("randomness: {e}"));
            let new_key = SecretToken::generate(SecretKind::Machine).unwrap_or_else(|e| panic!("randomness: {e}"));
            let body = serde_json::json!({"operation":"enroll","request_id":"a".repeat(32),"credential":key.expose_secret(),"label":"Laptop A"});
            let mutation = |value: &serde_json::Value, cookie: Option<&CookieJar>, csrf: bool| {
                let mut r = unsafe_request(Method::POST, PATH, Body::from(value.to_string()), cookie, csrf);
                r.headers_mut().insert(header::CONTENT_TYPE, HeaderValue::from_static("application/json")); r
            };
            let identify = |cookie: &CookieJar, key: Option<&str>| {
                let mut r = authenticated_get(CURRENT, cookie);
                if let Some(key) = key { r.headers_mut().insert(machine_http::MACHINE_HEADER, HeaderValue::from_str(key).unwrap_or_else(|e| panic!("header: {e}"))); } r
            };
            for (cookie, csrf, status) in [(None,false,StatusCode::UNAUTHORIZED),(Some(&a),false,StatusCode::FORBIDDEN)] {
                let r = router.clone().oneshot(mutation(&body,cookie,csrf)).await.unwrap_or_else(|e| match e {});
                assert_eq!(r.status(),status);
            }
            let response = router.clone().oneshot(mutation(&body,Some(&a),true)).await.unwrap_or_else(|e| match e {});
            assert_eq!(response.status(),StatusCode::OK);
            let text = body_text(response).await;
            assert!(!text.contains(key.expose_secret()));
            let value: serde_json::Value = serde_json::from_str(&text).unwrap_or_else(|e| panic!("JSON: {e}"));
            let machine = value["machine"]["machine_id"].clone();
            let repeated = router.clone().oneshot(mutation(&body,Some(&a),true)).await.unwrap_or_else(|e| match e {});
            assert_eq!(repeated.status(),StatusCode::OK);
            assert_eq!(body_text(repeated).await,text);
            let mut conflict=body.clone(); conflict["label"]="changed".into();
            assert_eq!(router.clone().oneshot(mutation(&conflict,Some(&a),true)).await.unwrap_or_else(|e| match e {}).status(),StatusCode::CONFLICT);
            for cookie in [&a,&b] {
                assert_eq!(router.clone().oneshot(identify(cookie,None)).await.unwrap_or_else(|e| match e {}).status(),StatusCode::FORBIDDEN);
            }
            assert_eq!(router.clone().oneshot(identify(&a,Some(key.expose_secret()))).await.unwrap_or_else(|e| match e {}).status(),StatusCode::OK);
            let malformed = [serde_json::json!({}),serde_json::json!({"operation":"enroll","request_id":"../x","label":"X","credential":"bad"}),
                serde_json::json!({"operation":"enroll","request_id":"b".repeat(32),"label":"x".repeat(5000),"credential":key.expose_secret()}),
                serde_json::json!({"operation":"enroll","request_id":"b".repeat(32),"label":"X","credential":key.expose_secret(),"server_path":"/tmp"})];
            for bad in malformed {
                assert_eq!(router.clone().oneshot(mutation(&bad,Some(&a),true)).await.unwrap_or_else(|e| match e {}).status(),StatusCode::BAD_REQUEST);
            }
            let rename = serde_json::json!({"operation":"rename","request_id":"b".repeat(32),"machine_id":machine,"credential":key.expose_secret(),"label":"Renamed laptop"});
            assert_eq!(router.clone().oneshot(mutation(&rename,Some(&a),true)).await.unwrap_or_else(|e| match e {}).status(),StatusCode::OK);
            let rotate=serde_json::json!({"operation":"rotate","request_id":"c".repeat(32),"machine_id":machine,"credential":key.expose_secret(),"new_credential":new_key.expose_secret()});
            for _ in 0..2 {assert_eq!(router.clone().oneshot(mutation(&rotate,Some(&a),true)).await.unwrap_or_else(|e| match e {}).status(),StatusCode::OK);}
            assert_eq!(router.clone().oneshot(identify(&a,Some(key.expose_secret()))).await.unwrap_or_else(|e| match e {}).status(),StatusCode::FORBIDDEN);
            assert_eq!(router.clone().oneshot(identify(&a,Some(new_key.expose_secret()))).await.unwrap_or_else(|e| match e {}).status(),StatusCode::OK);
            let revoke=serde_json::json!({"operation":"revoke","request_id":"d".repeat(32),"machine_id":machine});
            assert_eq!(router.clone().oneshot(mutation(&revoke,Some(&b),true)).await.unwrap_or_else(|e| match e {}).status(),StatusCode::OK);
            assert_eq!(router.clone().oneshot(identify(&a,Some(new_key.expose_secret()))).await.unwrap_or_else(|e| match e {}).status(),StatusCode::FORBIDDEN);
            assert_eq!(router.clone().oneshot(authenticated_get(SESSION_PATH,&a)).await.unwrap_or_else(|e| match e {}).status(),StatusCode::OK);
            let machines=router.clone().oneshot(authenticated_get(PATH,&a)).await.unwrap_or_else(|e| match e {});
            let listed: serde_json::Value=serde_json::from_str(&body_text(machines).await).unwrap_or_else(|e| panic!("JSON: {e}"));
            assert_eq!(listed["machines"].as_array().map(Vec::len),Some(1));
            let auth=fixture.state.auth.as_ref().unwrap_or_else(|| panic!("auth missing"));
            auth.now_ms(unix_ms()+TEST_AUTH_TTL_MS+1).unwrap_or_else(|e| panic!("time: {e}"));
            assert_eq!(router.clone().oneshot(mutation(&body,Some(&a),true)).await.unwrap_or_else(|e| match e {}).status(),StatusCode::UNAUTHORIZED);
        });
    }

    #[test]
    fn machine_http_denials_are_independent_and_expiry_preserves_enrollment() {
        runtime().block_on(async {
            use super::machine_http;
            use axum::Extension;
            use axum::extract::State;
            use termdeck_web_auth::{SecretKind, SecretToken};

            const MACHINES: &str = "/_termdeck/api/v1/machines";
            const CURRENT: &str = "/_termdeck/api/v1/machines/current";
            let fixture = fixture(&["Owner", "Other login"]);
            let router = build_router(fixture.state.clone());
            let owner = pair(&router, fixture.invitations[0].as_str()).await;
            let other = pair(&router, fixture.invitations[1].as_str()).await;
            let anonymous = csrf_cookies(None, &owner);
            let wrong_binding = csrf_cookies(Some(&owner), &other);
            let key = SecretToken::generate(SecretKind::Machine)
                .unwrap_or_else(|error| panic!("fixture randomness failed: {error}"));
            let enroll = serde_json::json!({
                "operation":"enroll", "request_id":"a".repeat(32),
                "credential":key.expose_secret(), "label":"Independent controls",
            });
            for (cookies, csrf, status, message) in [
                (
                    &anonymous,
                    true,
                    StatusCode::UNAUTHORIZED,
                    "authentication-required",
                ),
                (&owner, false, StatusCode::FORBIDDEN, "request-forbidden"),
                (
                    &wrong_binding,
                    true,
                    StatusCode::FORBIDDEN,
                    "request-forbidden",
                ),
            ] {
                let mut request = json_post(MACHINES, &enroll, cookies, None);
                if !csrf {
                    request.headers_mut().remove(CSRF_HEADER_NAME);
                }
                assert_error(call(&router, request).await, status, message).await;
            }
            let empty = expect_json(
                call(&router, authenticated_get(MACHINES, &owner)).await,
                StatusCode::OK,
            )
            .await;
            assert_eq!(empty["machines"], serde_json::json!([]));

            let mut malformed = json_post(MACHINES, &enroll, &owner, None);
            *malformed.body_mut() = Body::from("{");
            assert_error(
                call(&router, malformed).await,
                StatusCode::BAD_REQUEST,
                "invalid machine request",
            )
            .await;
            let enrolled = expect_json(
                call(&router, json_post(MACHINES, &enroll, &owner, None)).await,
                StatusCode::OK,
            )
            .await;
            assert!(!enrolled.to_string().contains(key.expose_secret()));
            let rename = serde_json::json!({
                "operation":"rename", "request_id":"b".repeat(32),
                "machine_id":enrolled["machine"]["machine_id"],
                "credential":key.expose_secret(), "label":"Still enrolled",
            });
            let current = |cookies: &CookieJar| {
                let mut request = authenticated_get(CURRENT, cookies);
                request.headers_mut().insert(
                    machine_http::MACHINE_HEADER,
                    HeaderValue::from_str(key.expose_secret())
                        .unwrap_or_else(|error| panic!("invalid fixture key header: {error}")),
                );
                request
            };
            for request in [authenticated_get(MACHINES, &anonymous), current(&anonymous)] {
                assert_error(
                    call(&router, request).await,
                    StatusCode::UNAUTHORIZED,
                    "authentication-required",
                )
                .await;
            }
            assert_error(
                call(&router, authenticated_get(CURRENT, &owner)).await,
                StatusCode::FORBIDDEN,
                "machine authorization failed",
            )
            .await;
            expect_json(call(&router, current(&owner)).await, StatusCode::OK).await;
            expect_json(
                call(&router, json_post(MACHINES, &rename, &owner, None)).await,
                StatusCode::OK,
            )
            .await;
            let context = fixture
                .state
                .auth
                .as_ref()
                .unwrap_or_else(|| panic!("fixture authentication missing"))
                .authenticate(current(&owner).headers(), unix_ms())
                .unwrap_or_else(|error| panic!("could not capture live fixture context: {error}"));
            expire_sessions(&fixture);
            for request in [
                authenticated_get(MACHINES, &owner),
                current(&owner),
                json_post(MACHINES, &rename, &owner, None),
            ] {
                assert_error(
                    call(&router, request).await,
                    StatusCode::UNAUTHORIZED,
                    "authentication-required",
                )
                .await;
            }
            // Exercise expiry after middleware has already captured a valid context.
            let list = machine_http::list(
                State(fixture.state.clone()),
                Some(Extension(context.clone())),
            )
            .await;
            let identify = machine_http::current(
                State(fixture.state.clone()),
                Some(Extension(context.clone())),
                current(&owner).headers().clone(),
            )
            .await;
            let mutation = machine_http::mutate(
                State(fixture.state.clone()),
                Some(Extension(context)),
                json_post(MACHINES, &rename, &owner, None),
            )
            .await;
            for response in [list, identify, mutation] {
                assert_error(
                    response,
                    StatusCode::UNAUTHORIZED,
                    "authentication-required",
                )
                .await;
            }
            let fresh = fresh_login(&fixture, &router).await;
            let retained = expect_json(call(&router, current(&fresh)).await, StatusCode::OK).await;
            assert_eq!(
                retained["machine"]["machine_id"],
                enrolled["machine"]["machine_id"]
            );
            expect_json(
                call(&router, json_post(MACHINES, &rename, &fresh, None)).await,
                StatusCode::OK,
            )
            .await;
        });
    }

    #[cfg(unix)]
    #[test]
    fn staging_http_denials_are_independent_and_never_invoke_the_bridge() {
        runtime().block_on(async {
            use axum::Extension;
            use axum::extract::State;
            use termdeck_web_auth::{SecretKind, SecretToken};

            const STAGING: &str = "/_termdeck/api/v1/client-files";
            let mut fixture = fixture(&["Owner", "Other login"]);
            // This fixture tests the HTTP-to-bridge boundary. Python storage is
            // exercised separately by check_client_files_integration.py.
            let bridge = fixture._directory.path().join("staging-fixture");
            let envelope = fixture._directory.path().join("bridge-envelope.json");
            let calls = fixture._directory.path().join("bridge-envelope.json.calls");
            fs::write(
                &bridge,
                "#!/bin/sh\nset -eu\n[ \"$1\" = client-files ]\ncat > \"$3\"\nprintf 'called\\n' >> \"$3.calls\"\nprintf '%s\\n' '{\"ok\":true,\"objects\":[]}'\n",
            )
            .unwrap_or_else(|error| panic!("could not write staging fixture: {error}"));
            fs::set_permissions(&bridge, fs::Permissions::from_mode(0o700))
                .unwrap_or_else(|error| panic!("could not protect staging fixture: {error}"));
            fixture.state.tab_actions = Some(Arc::new(
                super::TabActionService::new(bridge, envelope.clone(), Duration::from_secs(5))
                    .unwrap_or_else(|error| panic!("could not create staging bridge: {error}")),
            ));
            let router = build_router(fixture.state.clone());
            let owner = pair(&router, fixture.invitations[0].as_str()).await;
            let other = pair(&router, fixture.invitations[1].as_str()).await;
            let anonymous = csrf_cookies(None, &owner);
            let wrong_binding = csrf_cookies(Some(&owner), &other);
            let key = SecretToken::generate(SecretKind::Machine)
                .unwrap_or_else(|error| panic!("fixture randomness failed: {error}"));
            let enroll = serde_json::json!({
                "operation":"enroll", "request_id":"a".repeat(32),
                "credential":key.expose_secret(), "label":"Staging controls",
            });
            expect_json(
                call(&router, json_post("/_termdeck/api/v1/machines", &enroll, &owner, None)).await,
                StatusCode::OK,
            )
            .await;
            let list = serde_json::json!({"operation":"list"});
            for (cookies, csrf, status, message) in [
                (&anonymous, true, StatusCode::UNAUTHORIZED, "authentication-required"),
                (&owner, false, StatusCode::FORBIDDEN, "request-forbidden"),
                (&wrong_binding, true, StatusCode::FORBIDDEN, "request-forbidden"),
            ] {
                let mut request = json_post(STAGING, &list, cookies, Some(key.expose_secret()));
                if !csrf {
                    request.headers_mut().remove(CSRF_HEADER_NAME);
                }
                assert_error(call(&router, request).await, status, message).await;
            }
            let mut malformed = json_post(STAGING, &list, &owner, Some(key.expose_secret()));
            *malformed.body_mut() = Body::from("{");
            assert_error(call(&router, malformed).await, StatusCode::BAD_REQUEST, "invalid JSON").await;
            let mut wrong_type = json_post(STAGING, &list, &owner, Some(key.expose_secret()));
            wrong_type.headers_mut().insert(header::CONTENT_TYPE, HeaderValue::from_static("text/plain"));
            assert_error(call(&router, wrong_type).await, StatusCode::BAD_REQUEST, "JSON required").await;
            assert_error(
                call(&router, json_post(STAGING, &serde_json::json!({"operation":"resolve"}), &owner, Some(key.expose_secret()))).await,
                StatusCode::BAD_REQUEST,
                "invalid operation",
            )
            .await;
            assert_error(
                call(&router, json_post(STAGING, &list, &owner, None)).await,
                StatusCode::FORBIDDEN,
                "machine authorization failed",
            )
            .await;
            assert!(!calls.exists(), "a denied request reached the staging bridge");
            expect_json(
                call(&router, json_post(STAGING, &list, &owner, Some(key.expose_secret()))).await,
                StatusCode::OK,
            )
            .await;
            let recorded = fs::read_to_string(&envelope)
                .unwrap_or_else(|error| panic!("could not read staging envelope: {error}"));
            assert!(!recorded.contains(key.expose_secret()));
            let context = fixture
                .state
                .auth
                .as_ref()
                .unwrap_or_else(|| panic!("fixture authentication missing"))
                .authenticate(json_post(STAGING, &list, &owner, Some(key.expose_secret())).headers(), unix_ms())
                .unwrap_or_else(|error| panic!("could not capture staging session: {error}"));
            expire_sessions(&fixture);
            assert_error(
                call(&router, json_post(STAGING, &list, &owner, Some(key.expose_secret()))).await,
                StatusCode::UNAUTHORIZED,
                "authentication-required",
            )
            .await;
            let revalidated = super::staging_http::operate(
                State(fixture.state.clone()),
                Some(Extension(context)),
                json_post(STAGING, &list, &owner, Some(key.expose_secret())),
            )
            .await;
            assert_error(revalidated, StatusCode::UNAUTHORIZED, "authentication-required").await;
            assert_eq!(fs::read_to_string(&calls).unwrap_or_default(), "called\n");
            let fresh = fresh_login(&fixture, &router).await;
            expect_json(
                call(&router, json_post(STAGING, &list, &fresh, Some(key.expose_secret()))).await,
                StatusCode::OK,
            )
            .await;
            assert_eq!(fs::read_to_string(&calls).unwrap_or_default(), "called\ncalled\n");

            // Explicit server directories use the same enrolled-machine
            // bridge as saved choices, including its independent auth gates.
            const MANAGEMENT: &str = "/_termdeck/api/v1/management";
            let directory_launch = serde_json::json!({
                "operation":"create-window", "request_id":"b".repeat(32),
                "workspace_path":"/server/project with spaces", "policy_revision":"unconfigured",
                "name":"Directory", "runtime":"shell", "tabs":["One"], "attachments":[],
            });
            let mut no_csrf = json_post(MANAGEMENT, &directory_launch, &fresh, Some(key.expose_secret()));
            no_csrf.headers_mut().remove(CSRF_HEADER_NAME);
            assert_error(call(&router, no_csrf).await, StatusCode::FORBIDDEN, "request-forbidden").await;
            assert_error(
                call(&router, json_post(MANAGEMENT, &directory_launch, &fresh, None)).await,
                StatusCode::FORBIDDEN, "machine authorization failed",
            ).await;
            assert_eq!(fs::read_to_string(&calls).unwrap_or_default(), "called\ncalled\n");
            expect_json(
                call(&router, json_post(MANAGEMENT, &directory_launch, &fresh, Some(key.expose_secret()))).await,
                StatusCode::OK,
            ).await;
            let recorded: serde_json::Value = serde_json::from_str(&fs::read_to_string(&envelope)
                .unwrap_or_else(|error| panic!("could not read directory envelope: {error}")))
                .unwrap_or_else(|error| panic!("invalid directory envelope: {error}"));
            assert_eq!(recorded["request"], directory_launch);
            assert!(recorded["actor"]["machine_id"].as_str().is_some());
            assert_eq!(fs::read_to_string(&calls).unwrap_or_default(), "called\ncalled\ncalled\n");
        });
    }

    #[test]
    fn device_http_denials_preserve_the_selected_device() {
        runtime().block_on(async {
            const DEVICES: &str = "/_termdeck/api/v1/devices";
            let fixture = fixture(&["Owner", "Other login", "Selected device"]);
            let router = build_router(fixture.state.clone());
            let owner = pair(&router, fixture.invitations[0].as_str()).await;
            let other = pair(&router, fixture.invitations[1].as_str()).await;
            let selected = pair(&router, fixture.invitations[2].as_str()).await;
            let anonymous = csrf_cookies(None, &owner);
            let wrong_binding = csrf_cookies(Some(&owner), &other);
            let auth = fixture
                .state
                .auth
                .as_ref()
                .unwrap_or_else(|| panic!("fixture authentication missing"));
            let target = auth
                .authenticate(
                    authenticated_get(SESSION_PATH, &selected).headers(),
                    unix_ms(),
                )
                .unwrap_or_else(|error| panic!("could not identify selected device: {error}"));
            let revoke = format!("{DEVICES}/{}/revoke", target.device_id().as_str());
            let body = serde_json::json!({});
            assert_error(
                call(&router, authenticated_get(DEVICES, &anonymous)).await,
                StatusCode::UNAUTHORIZED,
                "authentication-required",
            )
            .await;
            expect_json(
                call(&router, authenticated_get(DEVICES, &owner)).await,
                StatusCode::OK,
            )
            .await;
            for (cookies, csrf, status, message) in [
                (
                    &anonymous,
                    true,
                    StatusCode::UNAUTHORIZED,
                    "authentication-required",
                ),
                (&owner, false, StatusCode::FORBIDDEN, "request-forbidden"),
                (
                    &wrong_binding,
                    true,
                    StatusCode::FORBIDDEN,
                    "request-forbidden",
                ),
            ] {
                let mut request = json_post(&revoke, &body, cookies, None);
                if !csrf {
                    request.headers_mut().remove(CSRF_HEADER_NAME);
                }
                assert_error(call(&router, request).await, status, message).await;
                expect_json(
                    call(&router, authenticated_get(SESSION_PATH, &selected)).await,
                    StatusCode::OK,
                )
                .await;
            }
            // The selected target is still present and unrevoked when the
            // requesting login expires; malformed IDs cannot mask this denial.
            expire_sessions(&fixture);
            for request in [
                authenticated_get(DEVICES, &owner),
                json_post(&revoke, &body, &owner, None),
            ] {
                assert_error(
                    call(&router, request).await,
                    StatusCode::UNAUTHORIZED,
                    "authentication-required",
                )
                .await;
            }
            let records = auth
                .devices()
                .unwrap_or_else(|error| panic!("could not inspect selected device: {error}"));
            let retained = records
                .iter()
                .find(|record| record.device_id() == target.device_id())
                .unwrap_or_else(|| panic!("denied revocation removed the selected device"));
            assert!(retained.revoked_at_ms().is_none());
        });
    }

    #[test]
    fn terminal_http_unavailability_is_explicit_and_other_io_stays_redacted() {
        runtime().block_on(async {
            for (kind, status, message) in [
                (
                    std::io::ErrorKind::NotFound,
                    StatusCode::SERVICE_UNAVAILABLE,
                    "tmux-unavailable",
                ),
                (
                    std::io::ErrorKind::ConnectionRefused,
                    StatusCode::SERVICE_UNAVAILABLE,
                    "tmux-unavailable",
                ),
                (
                    std::io::ErrorKind::PermissionDenied,
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "internal-error",
                ),
                (
                    std::io::ErrorKind::Other,
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "internal-error",
                ),
            ] {
                let error = super::TerminalError::Io(std::io::Error::new(
                    kind,
                    "private runtime fixture path",
                ));
                let value = expect_json(super::terminal_error_response(error), status).await;
                assert_eq!(value, serde_json::json!({"error":message}));
            }
        });
    }

    #[cfg(unix)]
    #[test]
    fn missing_managed_socket_returns_explicit_http_unavailable_without_starting_tmux() {
        use std::os::unix::fs::MetadataExt;

        const CHILD_ROOT: &str = "TERMDECK_HTTP_MISSING_SOCKET_TEST_ROOT";
        const TEST_NAME: &str = "auth_integration_tests::missing_managed_socket_returns_explicit_http_unavailable_without_starting_tmux";
        let Some(child_root) = std::env::var_os(CHILD_ROOT) else {
            // Isolate the process-wide runtime admission OnceLock and environment
            // from concurrent tests; no process-global environment is changed here.
            let directory = TestDirectory::new("missing-socket");
            let tmux_root = directory.path().join("tmux");
            DirBuilder::new()
                .mode(0o700)
                .create(&tmux_root)
                .unwrap_or_else(|error| {
                    panic!("could not create managed runtime fixture: {error}")
                });
            let executable = directory.path().join("tmux-fixture");
            fs::write(
                &executable,
                "#!/bin/sh\nprintf 'unexpected tmux invocation\\n' > \"$0.called\"\nexit 0\n",
            )
            .unwrap_or_else(|error| panic!("could not write tmux fixture: {error}"));
            fs::set_permissions(&executable, fs::Permissions::from_mode(0o700))
                .unwrap_or_else(|error| panic!("could not protect tmux fixture: {error}"));
            let metadata = fs::metadata(&tmux_root).unwrap_or_else(|error| {
                panic!("could not inspect managed runtime fixture: {error}")
            });
            let current_exe = std::env::current_exe()
                .unwrap_or_else(|error| panic!("could not locate test executable: {error}"));
            let output = std::process::Command::new(current_exe)
                .args(["--exact", TEST_NAME, "--nocapture"])
                .env(CHILD_ROOT, directory.path())
                .env("TERMDECK_STATE_ROOT", directory.path())
                .env("TERMDECK_TMUX_RUNTIME", &tmux_root)
                .env("TMUX_TMPDIR", &tmux_root)
                .env(
                    "TERMDECK_TMUX_IDENTITY",
                    format!("{}:{}:{}", metadata.dev(), metadata.ino(), metadata.uid()),
                )
                .env_remove("TMUX")
                .env_remove("TMUX_PANE")
                .output()
                .unwrap_or_else(|error| {
                    panic!("could not run isolated managed-runtime test: {error}")
                });
            assert!(
                output.status.success(),
                "isolated managed-runtime test failed:\n{}\n{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            assert!(!directory.path().join("tmux-fixture.called").exists());
            assert!(!tmux_root.join(format!("tmux-{}", metadata.uid())).exists());
            assert_eq!(
                fs::read(directory.path().join("http-unavailable-verified")).unwrap_or_default(),
                b"verified",
                "the isolated child must execute all HTTP assertions",
            );
            return;
        };
        let root = PathBuf::from(child_root);
        super::tmux_runtime::initialize().unwrap_or_else(|error| {
            panic!("missing socket should still admit safe runtime metadata: {error}")
        });
        runtime().block_on(async {
            let fixture = fixture(&["Owner"]);
            let router = build_router(fixture.state.clone());
            let cookies = pair(&router, fixture.invitations[0].as_str()).await;
            let saved = expect_json(
                call(&router, authenticated_get("/_termdeck/api/v1/snapshot", &cookies)).await,
                StatusCode::OK,
            )
            .await;
            assert!(saved.to_string().contains("pane-one"));
            const PANE: &str = "/_termdeck/api/v1/windows/random/panes/pane-one";
            for operation in ["capture", "input/text", "input/key"] {
                let mut state = fixture.state.clone();
                state.terminal = Some(Arc::new(super::TerminalService::new(
                    id(OwnerId::new("owner-test")),
                    root.join("tmux-fixture"),
                    Duration::from_secs(2),
                ).unwrap_or_else(|error| panic!("could not create terminal fixture: {error}"))));
                let router = build_router(state);
                let request = if operation == "capture" {
                    authenticated_get(&format!("{PANE}/capture"), &cookies)
                } else {
                    let control = expect_json(
                        call(&router, json_post(&format!("{PANE}/control"), &serde_json::json!({}), &cookies, None)).await,
                        StatusCode::OK,
                    )
                    .await;
                    let body = if operation == "input/text" {
                        serde_json::json!({"sequence":control["next_sequence"], "text":"fixture input"})
                    } else {
                        serde_json::json!({"sequence":control["next_sequence"], "key":"enter"})
                    };
                    json_post(&format!("{PANE}/{operation}"), &body, &cookies, None)
                };
                assert_error(call(&router, request).await, StatusCode::SERVICE_UNAVAILABLE, "tmux-unavailable").await;
                let preserved = expect_json(
                    call(&router, authenticated_get("/_termdeck/api/v1/snapshot", &cookies)).await,
                    StatusCode::OK,
                )
                .await;
                assert_eq!(preserved, saved);
                assert!(!root.join("tmux-fixture.called").exists());
            }
        });
        fs::write(root.join("http-unavailable-verified"), b"verified")
            .unwrap_or_else(|error| panic!("could not record isolated HTTP assertions: {error}"));
    }

    #[test]
    fn device_listing_and_revocation_invalidate_only_the_selected_device() {
        runtime().block_on(async {
            let fixture = fixture(&["Laptop", "Phone"]);
            let router = build_router(fixture.state.clone());
            let laptop = pair(&router, fixture.invitations[0].as_str()).await;
            let phone = pair(&router, fixture.invitations[1].as_str()).await;
            let devices = router
                .clone()
                .oneshot(authenticated_get("/_termdeck/api/v1/devices", &laptop))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(devices.status(), StatusCode::OK);
            let payload: serde_json::Value =
                serde_json::from_str(body_text(devices).await.as_str())
                    .unwrap_or_else(|error| panic!("invalid devices JSON: {error}"));
            let entries = payload["devices"]
                .as_array()
                .unwrap_or_else(|| panic!("devices JSON is not an array"));
            assert_eq!(entries.len(), 2);
            let current_id = entries
                .iter()
                .find(|entry| entry["current"] == true)
                .and_then(|entry| entry["device_id"].as_str())
                .unwrap_or_else(|| panic!("missing current device"));
            let phone_id = entries
                .iter()
                .find(|entry| entry["label"] == "Phone")
                .and_then(|entry| entry["device_id"].as_str())
                .unwrap_or_else(|| panic!("missing phone device"));
            assert_ne!(current_id, phone_id);

            let revoke_phone = router
                .clone()
                .oneshot(unsafe_request(
                    Method::POST,
                    format!("/_termdeck/api/v1/devices/{phone_id}/revoke").as_str(),
                    Body::empty(),
                    Some(&laptop),
                    true,
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(revoke_phone.status(), StatusCode::OK);
            let phone_session = router
                .clone()
                .oneshot(authenticated_get("/_termdeck/api/v1/session", &phone))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(phone_session.status(), StatusCode::UNAUTHORIZED);
            let laptop_session = router
                .clone()
                .oneshot(authenticated_get("/_termdeck/api/v1/session", &laptop))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(laptop_session.status(), StatusCode::OK);

            let revoke_self = router
                .clone()
                .oneshot(unsafe_request(
                    Method::POST,
                    format!("/_termdeck/api/v1/devices/{current_id}/revoke").as_str(),
                    Body::empty(),
                    Some(&laptop),
                    true,
                ))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(revoke_self.status(), StatusCode::OK);
            assert_eq!(
                revoke_self
                    .headers()
                    .get_all(header::SET_COOKIE)
                    .iter()
                    .count(),
                2
            );
            let laptop_revoked = router
                .oneshot(authenticated_get("/_termdeck/api/v1/session", &laptop))
                .await
                .unwrap_or_else(|error| match error {});
            assert_eq!(laptop_revoked.status(), StatusCode::UNAUTHORIZED);
        });
    }

    #[test]
    fn authentication_configuration_and_parsers_fail_closed() {
        let home = PathBuf::from("/tmp/termdeck-auth-config-home");
        let auth_directory = "/tmp/termdeck-auth-config-home/auth";
        let missing_opt_in = ServerConfig::parse_with_home(
            [OsString::from("--auth-dir"), OsString::from(auth_directory)],
            home.clone(),
        );
        assert!(matches!(
            missing_opt_in,
            Err(ServerError::InsecureLoopbackAuthRequiresOptIn)
        ));
        let missing_directory = ServerConfig::parse_with_home(
            [OsString::from("--allow-insecure-loopback-auth")],
            home.clone(),
        );
        assert!(matches!(
            missing_directory,
            Err(ServerError::AuthDirectoryRequired)
        ));
        let enabled = ServerConfig::parse_with_home(
            [
                OsString::from("--auth-dir"),
                OsString::from(auth_directory),
                OsString::from("--allow-insecure-loopback-auth"),
            ],
            home.clone(),
        );
        assert!(matches!(
            enabled,
            Ok(ConfigAction::Run(config)) if config.authentication_enabled()
        ));
        let invitation = ServerConfig::parse_with_home(
            [
                OsString::from("--auth-dir"),
                OsString::from(auth_directory),
                OsString::from("--create-invitation"),
                OsString::from("Laptop"),
            ],
            home.clone(),
        );
        assert!(matches!(invitation, Ok(ConfigAction::CreateInvitation(_))));
        let lan = ServerConfig::parse_with_home(
            [
                OsString::from("--listen"),
                OsString::from("0.0.0.0:7391"),
                OsString::from("--auth-dir"),
                OsString::from(auth_directory),
                OsString::from("--allow-insecure-loopback-auth"),
            ],
            home,
        );
        assert!(matches!(
            lan,
            Err(ServerError::NonLoopbackRequiresAuthentication(_))
        ));
        assert!(parse_pairing_form(b"invitation=tdi1_abc&extra=value").is_err());
        assert!(parse_pairing_form(b"invitation=one&invitation=two").is_err());
        assert!(!loopback_authority("127.0.0.1:bad"));
        assert!(!loopback_authority("127.0.0.1:7391:80"));
        assert!(!loopback_authority("localhost@evil.example"));
        assert!(loopback_authority("[::1]:7391"));

        let parent = TestDirectory::new("directory-parent");
        let created = parent.path().join("auth");
        ensure_auth_directory(&created)
            .unwrap_or_else(|error| panic!("could not create secure auth directory: {error}"));
        #[cfg(unix)]
        {
            let mode = fs::metadata(&created)
                .unwrap_or_else(|error| panic!("could not inspect auth directory: {error}"))
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o700);
        }
    }
}
