#![forbid(unsafe_code)]

//! Bounded loopback HTTP vertical slice for the read-only TermDeck mirror.
//!
//! The server does not authenticate or mutate anything yet. Consequently it
//! refuses every non-loopback bind, exposes only GET/HEAD routes, opens SQLite
//! through `termdeck-web-state`'s strict read-only adapter, and invokes only one
//! hard-coded read-only tmux inventory command.

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{Path as AxumPath, Request, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use serde::Serialize;
use std::collections::BTreeMap;
use std::error::Error;
use std::ffi::OsString;
use std::fmt;
use std::net::{IpAddr, SocketAddr};
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use termdeck_web_core::{
    ControllerView, HostId, HostSnapshot, OwnerId, PaneSnapshot, PaneStatus, WindowSnapshot,
};
use termdeck_web_state::{
    MAX_DATABASE_ROWS, ReadOnlyStateMirror, StateError, TmuxInventory, TmuxPaneState,
};
use tokio::net::TcpListener;
use tokio::process::Command;
use tokio::sync::Semaphore;
use tokio::time::{MissedTickBehavior, timeout};

pub const DEFAULT_LISTEN: &str = "127.0.0.1:7391";
pub const DEFAULT_REFRESH_MS: u64 = 1_000;
pub const DEFAULT_MAX_STALE_MS: u64 = 30_000;
pub const DEFAULT_TMUX_TIMEOUT_MS: u64 = 750;
pub const DEFAULT_MAX_CONNECTIONS: usize = 128;
pub const MAX_CONNECTIONS: usize = 1_024;
pub const MAX_RENDERED_RESPONSE_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_TMUX_OUTPUT_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_TMUX_ROWS: usize = MAX_DATABASE_ROWS * 4;

const CONTENT_SECURITY_POLICY: &str = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'";
const USAGE: &str = "TermDeck read-only web mirror\n\n\
Usage: termdeck-web-server [options]\n\n\
Options:\n\
  --db PATH                TermDeck sessions.sqlite3 path\n\
  --listen IP:PORT         Loopback address (default 127.0.0.1:7391)\n\
  --refresh-ms N           Refresh interval, 250..60000\n\
  --max-stale-ms N         Maximum stale serving window, 1000..600000\n\
  --max-connections N      Concurrent request cap, 1..1024\n\
  --tmux PATH              tmux executable (default tmux)\n\
  --tmux-timeout-ms N      Read-only inventory timeout, 50..5000\n\
  --host-id ID             Opaque local host identity\n\
  --owner-id ID            Opaque single-owner identity\n\
  -h, --help               Show this help\n\n\
Non-loopback listening is intentionally refused until authenticated-device\n\
transport is implemented in the next security-gated slice.\n";

#[derive(Clone, Debug)]
pub struct ServerConfig {
    database_path: PathBuf,
    listen: SocketAddr,
    refresh_interval: Duration,
    max_stale: Duration,
    max_connections: usize,
    tmux_executable: PathBuf,
    tmux_timeout: Duration,
    host_id: HostId,
    owner_id: OwnerId,
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
            host_id: HostId::new("termdeck-local-host")?,
            owner_id: OwnerId::new("termdeck-local-owner")?,
        };
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
                "--host-id" => {
                    config.host_id = HostId::new(next_value(&mut arguments, "--host-id")?)?;
                }
                "--owner-id" => {
                    config.owner_id = OwnerId::new(next_value(&mut arguments, "--owner-id")?)?;
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
}

pub enum ConfigAction {
    Run(ServerConfig),
    Help,
}

#[derive(Debug)]
pub enum ServerError {
    Io(std::io::Error),
    State(StateError),
    Json(serde_json::Error),
    Id(termdeck_web_core::IdError),
    Join(tokio::task::JoinError),
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
    CacheLockPoisoned,
    ResponseTooLarge { actual: usize, maximum: usize },
    TmuxTimeout,
    TmuxFailed(String),
    TmuxOutputTooLarge(usize),
    MalformedTmuxRow(String),
    TooManyTmuxRows(usize),
}

impl fmt::Display for ServerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "web server I/O failed: {error}"),
            Self::State(error) => write!(formatter, "web state refresh failed: {error}"),
            Self::Json(error) => write!(formatter, "web JSON rendering failed: {error}"),
            Self::Id(error) => write!(formatter, "web identity failed: {error}"),
            Self::Join(error) => write!(formatter, "web refresh worker failed: {error}"),
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
                "non-loopback listen {address} is refused until device authentication is implemented"
            ),
            Self::StaleWindowShorterThanRefresh => {
                formatter.write_str("max stale window must not be shorter than refresh interval")
            }
            Self::NumericOverflow => formatter.write_str("numeric conversion overflow"),
            Self::MirrorLockPoisoned => formatter.write_str("state mirror lock is poisoned"),
            Self::CacheLockPoisoned => formatter.write_str("snapshot cache lock is poisoned"),
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
            Self::Io(error) => Some(error),
            Self::State(error) => Some(error),
            Self::Json(error) => Some(error),
            Self::Id(error) => Some(error),
            Self::Join(error) => Some(error),
            _ => None,
        }
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
                    Ok(true) => eprintln!("TermDeck web refresh failed: {error}"),
                    Ok(false) => {}
                    Err(cache_error) => eprintln!(
                        "TermDeck web refresh failed: {error}; cache update failed: {cache_error}"
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
        let full_json = serde_json::to_vec(&host_dto(&snapshot))?;
        ensure_response_bound(full_json.len())?;
        let homepage_html = render_homepage(&snapshot, tmux_warning.as_deref())?;
        let mut windows = BTreeMap::new();
        for window in snapshot.windows() {
            let json = serde_json::to_vec(&WindowEnvelopeDto {
                schema: "termdeck.window-snapshot.v1",
                revision: snapshot.revision(),
                generated_at_unix_ms: snapshot.generated_at_unix_ms(),
                window: window_dto(window),
            })?;
            ensure_response_bound(json.len())?;
            let html = render_window_page(&snapshot, window, tmux_warning.as_deref())?;
            windows.insert(
                window.route().slug().to_owned(),
                RenderedWindow {
                    html: Bytes::from(html),
                    json: Bytes::from(json),
                },
            );
        }
        Ok(Self {
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
}

pub fn run_from_env() -> Result<(), ServerError> {
    match ServerConfig::parse(std::env::args_os().skip(1))? {
        ConfigAction::Help => {
            println!("{USAGE}");
            Ok(())
        }
        ConfigAction::Run(config) => {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            runtime.block_on(serve(config))
        }
    }
}

pub async fn serve(config: ServerConfig) -> Result<(), ServerError> {
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
    };
    let application = build_router(state);
    let listener = TcpListener::bind(config.listen).await?;
    println!(
        "TermDeck read-only web mirror listening on http://{}",
        listener.local_addr()?
    );
    let refresher = tokio::spawn(engine.run_periodic(config.refresh_interval));
    let result = axum::serve(listener, application)
        .with_graceful_shutdown(shutdown_signal())
        .await;
    refresher.abort();
    result.map_err(ServerError::Io)
}

fn build_router(state: AppState) -> Router {
    Router::new()
        .route("/", get(homepage))
        .route("/_termdeck/healthz", get(healthz))
        .route("/_termdeck/readyz", get(readyz))
        .route("/_termdeck/api/v1/snapshot", get(api_snapshot))
        .route("/_termdeck/api/v1/windows/{slug}", get(api_window))
        .route("/{slug}", get(window_page))
        .fallback(not_found)
        .layer(middleware::from_fn_with_state(state.clone(), request_gate))
        .layer(middleware::from_fn(security_headers))
        .with_state(state)
}

async fn request_gate(State(state): State<AppState>, request: Request, next: Next) -> Response {
    if !host_is_allowed(request.headers()) || !origin_is_allowed(request.headers()) {
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

async fn security_headers(request: Request, next: Next) -> Response {
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
        HeaderName::from_static("referrer-policy"),
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(
        HeaderName::from_static("permissions-policy"),
        HeaderValue::from_static("camera=(), microphone=(), geolocation=(), payment=()"),
    );
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store, max-age=0"),
    );
    response
}

async fn homepage(State(state): State<AppState>) -> Response {
    publication_response(&state.cache, |publication| {
        (
            "text/html; charset=utf-8",
            publication.homepage_html.clone(),
        )
    })
}

async fn window_page(State(state): State<AppState>, AxumPath(slug): AxumPath<String>) -> Response {
    publication_response(&state.cache, |publication| {
        publication.windows.get(&slug).map_or_else(
            || {
                (
                    "text/html; charset=utf-8",
                    Bytes::from(render_not_found_page(&slug)),
                )
            },
            |window| ("text/html; charset=utf-8", window.html.clone()),
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

async fn not_found(method: Method) -> Response {
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
        Bytes::from(render_not_found_page("")),
    )
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

fn host_is_allowed(headers: &HeaderMap) -> bool {
    let Some(value) = headers.get(header::HOST) else {
        return false;
    };
    let Ok(host) = value.to_str() else {
        return false;
    };
    loopback_authority(host)
}

fn origin_is_allowed(headers: &HeaderMap) -> bool {
    let Some(value) = headers.get(header::ORIGIN) else {
        return true;
    };
    let Ok(origin) = value.to_str() else {
        return false;
    };
    let Some(authority) = origin
        .strip_prefix("http://")
        .or_else(|| origin.strip_prefix("https://"))
    else {
        return false;
    };
    !authority.contains('/') && loopback_authority(authority)
}

fn loopback_authority(authority: &str) -> bool {
    let host = if let Some(rest) = authority.strip_prefix('[') {
        match rest.split_once(']') {
            Some((host, suffix)) if suffix.is_empty() || suffix.starts_with(':') => host,
            _ => return false,
        }
    } else {
        authority.split(':').next().unwrap_or("")
    };
    host.eq_ignore_ascii_case("localhost")
        || IpAddr::from_str(host).is_ok_and(|address| address.is_loopback())
}

#[derive(Serialize)]
struct HostDto<'a> {
    schema: &'static str,
    host_id: &'a str,
    owner_id: &'a str,
    revision: u64,
    generated_at_unix_ms: u64,
    total_panes: usize,
    windows: Vec<WindowDto<'a>>,
}

#[derive(Serialize)]
struct WindowEnvelopeDto<'a> {
    schema: &'static str,
    revision: u64,
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
    output.push_str("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>TermDeck</title><style>");
    output.push_str(STYLES);
    output.push_str("</style></head><body><main><header><p class=\"eyebrow\">TermDeck host mirror</p><h1>Windows</h1><p class=\"subtle\">");
    output.push_str(&snapshot.windows().len().to_string());
    output.push_str(" windows · ");
    output.push_str(&snapshot.total_panes().to_string());
    output.push_str(" visible panes · revision ");
    output.push_str(&snapshot.revision().to_string());
    output.push_str("</p></header>");
    if tmux_warning.is_some() {
        output.push_str("<div class=\"notice\">tmux inventory is temporarily unavailable; registry status is shown without claiming live-process certainty.</div>");
    }
    output.push_str("<section class=\"grid\" aria-label=\"TermDeck windows\">");
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
        output.push_str(" running</span><code>");
        output.push_str(window.route().path().as_str());
        output.push_str("</code></a>");
    }
    output.push_str("</section><footer><a href=\"/_termdeck/api/v1/snapshot\">JSON snapshot</a></footer></main></body></html>");
    ensure_response_bound(output.len())?;
    Ok(output)
}

fn render_window_page(
    snapshot: &HostSnapshot,
    window: &WindowSnapshot,
    tmux_warning: Option<&str>,
) -> Result<String, ServerError> {
    let mut output = String::with_capacity(8_192 + window.panes().len().saturating_mul(512));
    output.push_str("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>");
    escape_html_into(&mut output, window.name());
    output.push_str(" · TermDeck</title><style>");
    output.push_str(STYLES);
    output.push_str("</style></head><body><main><nav><a href=\"/\">← All windows</a></nav><header><p class=\"eyebrow\">TermDeck window</p><h1>");
    escape_html_into(&mut output, window.name());
    output.push_str("</h1><p class=\"subtle\">");
    output.push_str(&window.panes().len().to_string());
    output.push_str(" visible panes · revision ");
    output.push_str(&snapshot.revision().to_string());
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
        output.push_str("<article class=\"pane\"><div><h2>");
        escape_html_into(&mut output, pane.title());
        output.push_str("</h2><p class=\"path\">");
        escape_html_into(&mut output, pane.cwd());
        output.push_str("</p></div><span class=\"status status-");
        output.push_str(pane_status_name(pane.status()));
        output.push_str("\">");
        output.push_str(pane_status_name(pane.status()));
        output.push_str("</span></article>");
    }
    output.push_str("</section><footer><a href=\"/_termdeck/api/v1/windows/");
    output.push_str(window.route().slug());
    output.push_str("\">Window JSON</a></footer></main></body></html>");
    ensure_response_bound(output.len())?;
    Ok(output)
}

fn render_not_found_page(slug: &str) -> String {
    let mut output = String::from(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Not found · TermDeck</title><style>",
    );
    output.push_str(STYLES);
    output.push_str("</style></head><body><main><h1>Window not found</h1><p class=\"subtle\">No mirrored TermDeck window owns this route");
    if !slug.is_empty() {
        output.push_str(": <code>/");
        escape_html_into(&mut output, slug);
        output.push_str("</code>");
    }
    output.push_str(".</p><p><a href=\"/\">Return to all windows</a></p></main></body></html>");
    output
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

const STYLES: &str = r#"
:root{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,sans-serif;background:#090c12;color:#e8edf7}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#171d2b 0,#090c12 45%);min-height:100vh}main{width:min(1180px,calc(100% - 32px));margin:0 auto;padding:48px 0 72px}a{color:#b8c8ff;text-decoration:none}a:hover{text-decoration:underline}header{margin:24px 0 32px}.eyebrow{color:#8fa2d8;text-transform:uppercase;letter-spacing:.16em;font-size:.74rem;font-weight:700}h1{font-size:clamp(2rem,5vw,4rem);line-height:1;margin:.15em 0}.subtle,.card-meta,.path{color:#9ba7bd}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}.card,.pane,.empty,.notice{border:1px solid #293247;background:rgba(17,22,33,.86);border-radius:14px}.card{display:flex;min-height:150px;padding:20px;flex-direction:column;gap:10px;transition:transform .12s ease,border-color .12s ease}.card:hover{transform:translateY(-2px);border-color:#778bc5;text-decoration:none}.card-title{font-size:1.25rem;font-weight:700;color:#f4f7ff}.card code{margin-top:auto;color:#8294c7}.panes{display:grid;gap:10px}.pane{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:17px 20px}.pane h2{font-size:1rem;margin:0 0 5px}.path{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:.82rem;margin:0;overflow-wrap:anywhere}.status{border-radius:999px;padding:5px 10px;font-size:.72rem;font-weight:700;background:#283044;color:#c9d2e8}.status-running{background:#173e30;color:#9be5bd}.status-paused{background:#493a18;color:#f5d68a}.status-missing,.status-dead,.status-start-error{background:#4b252c;color:#ffb5c1}.notice{padding:13px 16px;margin:0 0 20px;color:#f0d398}.empty{padding:30px;color:#9ba7bd}nav{margin-bottom:28px}footer{margin-top:32px;padding-top:20px;border-top:1px solid #232b3c}@media(max-width:600px){main{padding-top:24px}.pane{align-items:flex-start;flex-direction:column}.status{align-self:flex-start}}
"#;

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
    let mut command = Command::new(executable);
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
        eprintln!("TermDeck web shutdown signal failed: {error}");
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
        AppState, CONTENT_SECURITY_POLICY, CacheAvailability, ConfigAction, RenderedSnapshot,
        ServerConfig, ServerError, SnapshotCache, build_router, unix_ms,
    };
    use axum::body::{Body, to_bytes};
    use axum::http::{Method, Request, StatusCode, header};
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
            42,
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
            assert!(home_text.contains("href=\"/random\""));
            assert!(home_text.contains("Random &amp; Research"));

            let window = match router
                .oneshot(request(Method::GET, "/random", "localhost:7391"))
                .await
            {
                Ok(value) => value,
                Err(error) => match error {},
            };
            assert_eq!(window.status(), StatusCode::OK);
            let window_text = body_text(window).await;
            assert!(window_text.contains("Shell &lt;unsafe&gt;"));
            assert!(window_text.contains("/tmp/&amp;work"));
        });
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
            assert!(text.contains("\"path\":\"/random\""));

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
