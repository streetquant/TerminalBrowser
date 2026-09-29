use crate::auth_http::HttpAuth;
use crate::terminal_http::{
    InputDecision, InputOutcome, MAX_CAPTURE_HISTORY_LINES, MAX_TERMINAL_INPUT_BYTES,
    TerminalControlView, TerminalError, TerminalService,
};
use axum::extract::ws::{Message, WebSocket};
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::error::Error;
use std::fmt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;
use termdeck_web_auth::SessionContext;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout};
use tokio::sync::{mpsc, oneshot};
use tokio::time::{MissedTickBehavior, interval, timeout};

const HELLO_TIMEOUT: Duration = Duration::from_secs(10);
const WEBSOCKET_SEND_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_CLIENT_TEXT_BYTES: usize = 4 * 1024;
const MAX_CONTROL_COMMAND_BYTES: usize = 256 * 1024;
const MAX_CONTROL_NOTIFICATION_LINE_BYTES: usize = 256 * 1024;
// A joined tmux history line can contain every retained wrapped row. Keep the
// wire, reply and replay limits finite while admitting the configured history
// depth at the largest accepted grid. Commands retain their smaller limit.
const MAX_CONTROL_LINE_BYTES: usize = 16 * 1024 * 1024;
const MAX_CONTROL_BODY_BYTES: usize = 16 * 1024 * 1024;
const MAX_REPLAY_BYTES: usize = 8 * 1024 * 1024;
const MAX_INITIAL_REPLAY_HISTORY_LINES: usize = MAX_CAPTURE_HISTORY_LINES as usize;
const MAX_INITIAL_REPLAY_JOINED_CELLS: usize =
    MAX_INITIAL_REPLAY_HISTORY_LINES * MAX_COLUMNS as usize;
const CONTROL_REQUEST_CAPACITY: usize = 64;
// Viewer output is bounded by queued bytes, not by tmux line count: a burst of
// small `%output` lines must not disconnect a healthy browser while the stream
// task awaits one input round trip or one WebSocket send.
const MAX_PENDING_OUTPUT_BYTES: usize = 8 * 1024 * 1024;
// Output already queued behind a send is coalesced into one browser frame.
const MAX_OUTPUT_FRAME_BYTES: usize = 256 * 1024;
// Non-output notifications are charged against the same byte bound.
const NOTIFICATION_COST_BYTES: usize = 64;
// One control line may carry a short, fixed tmux command list.
const MAX_COMMAND_BLOCKS: usize = 8;
// tmux emits short SGR parameter groups; longer ones are not replayed.
const MAX_SGR_GROUP_BYTES: usize = 64;
const SGR_ATTRIBUTE_SLOTS: usize = 9;
const INPUT_CHUNK_BYTES: usize = 4 * 1024;
const MIN_COLUMNS: u16 = 2;
const MAX_COLUMNS: u16 = 1_000;
const MIN_ROWS: u16 = 2;
const MAX_ROWS: u16 = 500;
const INPUT_FRAME_TYPE: u8 = 1;
const MOUSE_WHEEL_FRAME_TYPE: u8 = 2;
const APPLICATION_SCROLL_FRAME_TYPE: u8 = 3;
const MAX_MOUSE_WHEEL_BYTES: usize = 64;
const MOUSE_REPORTING_FORMAT: &str =
    "#{||:#{mouse_standard_flag},#{||:#{mouse_button_flag},#{mouse_all_flag}}}";
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
// An idle viewer must lose access too; controller lease heartbeats alone do
// not observe logout, device revocation or authentication expiry.
const SESSION_CHECK_INTERVAL: Duration = Duration::from_secs(1);
const CONTROL_MODE_ARGUMENT: &str = "-C";
const CONTROL_FLAGS_VIEWER: &str = "active-pane,ignore-size,no-detach-on-destroy";
const CONTROL_FLAGS_WRITER: &str = "active-pane,!ignore-size,no-detach-on-destroy";
const PANE_STATE_FORMAT: &str = "#{pane_id}\t#{pane_pid}\t#{pane_width}\t#{pane_height}\t#{alternate_on}\t#{pane_dead}\t#{cursor_x}\t#{cursor_y}\t#{cursor_flag}\t#{keypad_cursor_flag}\t#{wrap_flag}\t#{insert_flag}\t#{origin_flag}\t#{mouse_standard_flag}\t#{mouse_button_flag}\t#{mouse_all_flag}\t#{mouse_sgr_flag}\t#{bracket_paste_flag}\t#{history_size}";

#[derive(Clone)]
pub(crate) struct StreamPane {
    pub pane_id: String,
    pub tmux_target: String,
}

#[derive(Debug)]
pub(crate) enum StreamError {
    Backpressure,
    ClientProtocol(&'static str),
    ControlClosed,
    ControlProtocol(&'static str),
    Io(std::io::Error),
    Json(serde_json::Error),
    SessionUnavailable,
    Terminal(TerminalError),
    Timeout(&'static str),
    Tmux(String),
}

impl fmt::Display for StreamError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Backpressure => formatter.write_str("terminal viewer fell behind"),
            Self::ClientProtocol(detail) => write!(formatter, "invalid terminal client: {detail}"),
            Self::ControlClosed => formatter.write_str("tmux control connection closed"),
            Self::ControlProtocol(detail) => {
                write!(formatter, "tmux control protocol failed: {detail}")
            }
            Self::Io(error) => write!(formatter, "terminal stream I/O failed: {error}"),
            Self::Json(error) => write!(formatter, "terminal stream JSON failed: {error}"),
            Self::SessionUnavailable => {
                formatter.write_str("terminal authentication is no longer valid")
            }
            Self::Terminal(error) => error.fmt(formatter),
            Self::Timeout(operation) => {
                write!(formatter, "terminal stream timed out during {operation}")
            }
            Self::Tmux(detail) => write!(formatter, "tmux stream command failed: {detail}"),
        }
    }
}

impl Error for StreamError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::Json(error) => Some(error),
            Self::Terminal(error) => Some(error),
            _ => None,
        }
    }
}

impl From<std::io::Error> for StreamError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value)
    }
}

impl From<serde_json::Error> for StreamError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json(value)
    }
}

impl From<TerminalError> for StreamError {
    fn from(value: TerminalError) -> Self {
        Self::Terminal(value)
    }
}

#[derive(Deserialize)]
#[serde(tag = "type", rename_all = "kebab-case", deny_unknown_fields)]
enum ClientEvent {
    Hello { cols: u16, rows: u16 },
    Focus,
    Ping,
    Resize { cols: u16, rows: u16 },
    Resync { cols: u16, rows: u16 },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum InputKind {
    Raw,
    MouseWheel,
    MouseMotion,
    ApplicationScroll,
}

impl InputKind {
    fn domain(self) -> &'static [u8] {
        match self {
            Self::Raw => b"websocket-raw",
            Self::MouseWheel => b"websocket-wheel",
            Self::MouseMotion => b"websocket-motion",
            Self::ApplicationScroll => b"websocket-application-scroll",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
struct TerminalGeometry {
    cols: u16,
    rows: u16,
}

impl TerminalGeometry {
    fn new(cols: u16, rows: u16) -> Result<Self, StreamError> {
        validate_geometry(cols, rows)?;
        Ok(Self { cols, rows })
    }
}

struct ReplaySnapshot {
    geometry: TerminalGeometry,
    bytes: Vec<u8>,
}

enum ControlNotification {
    Output(Vec<u8>),
    LayoutChanged,
    /// Queued when the first reply block of an atomic replay begins. Output
    /// queued before it is already contained in that replay's capture.
    ReplayBarrier(u64),
}

#[derive(Serialize)]
struct ResetEvent {
    r#type: &'static str,
    geometry: TerminalGeometry,
}

#[derive(Serialize)]
struct ReadyEvent<'a> {
    r#type: &'static str,
    control: &'a TerminalControlView,
    geometry: TerminalGeometry,
    terminal_asset_version: &'static str,
}

#[derive(Serialize)]
struct ControlEvent<'a> {
    r#type: &'static str,
    control: &'a TerminalControlView,
    focused: bool,
}

#[derive(Serialize)]
struct ResyncedEvent<'a> {
    r#type: &'static str,
    control: &'a TerminalControlView,
    geometry: TerminalGeometry,
}

#[derive(Serialize)]
struct AckEvent<'a> {
    r#type: &'static str,
    sequence: u64,
    control: &'a TerminalControlView,
    duplicate: bool,
}

#[derive(Serialize)]
struct ErrorEvent<'a> {
    r#type: &'static str,
    code: &'a str,
    message: &'a str,
    recoverable: bool,
}

#[derive(Serialize)]
struct PongEvent {
    r#type: &'static str,
}

pub(crate) async fn run_terminal_socket(
    mut socket: WebSocket,
    terminal: Arc<TerminalService>,
    pane: StreamPane,
    actor: String,
    auth: Arc<HttpAuth>,
    context: SessionContext,
) {
    if let Err(error) =
        run_terminal_socket_inner(&mut socket, terminal, pane, actor, auth, context).await
    {
        let message = error.to_string();
        let event = ErrorEvent {
            r#type: "error",
            code: error_code(&error),
            message: message.as_str(),
            recoverable: !matches!(
                error,
                StreamError::ClientProtocol(_) | StreamError::SessionUnavailable
            ),
        };
        let _result = send_json(&mut socket, &event).await;
    }
    let _result = socket.send(Message::Close(None)).await;
}

async fn run_terminal_socket_inner(
    socket: &mut WebSocket,
    terminal: Arc<TerminalService>,
    pane: StreamPane,
    actor: String,
    auth: Arc<HttpAuth>,
    context: SessionContext,
) -> Result<(), StreamError> {
    validate_termdeck_id(pane.pane_id.as_str())?;
    validate_tmux_target(pane.tmux_target.as_str())?;
    let mut viewport_geometry = receive_hello(socket).await?;
    require_stream_session(&auth, &context).await?;
    let mut control_view = match terminal
        .acquire(pane.pane_id.as_str(), actor.as_str(), unix_ms(), false)
        .await
    {
        Ok(value) => value,
        Err(_error) => {
            terminal
                .status(pane.pane_id.as_str(), actor.as_str(), unix_ms())
                .await?
        }
    };
    let mut control = TmuxControl::spawn(
        terminal.tmux_executable(),
        pane.tmux_target.clone(),
        terminal.command_timeout(),
    )
    .await?;
    // Only the active browser writer contributes a size to tmux.  Viewers
    // retain the canonical pane grid without resizing another user's TUI.
    control
        .set_writer_mode(control_view.mine, viewport_geometry)
        .await?;
    // Output resumes inside the replay transaction; anything tmux emits while
    // the replay is sent stays queued (byte-bounded) and follows it in order.
    let replay = control.initial_replay().await?;
    let mut geometry = replay.geometry;
    send_json(
        socket,
        &ResetEvent {
            r#type: "reset",
            geometry,
        },
    )
    .await?;
    send_message(socket, Message::Binary(replay.bytes.into())).await?;
    send_json(
        socket,
        &ReadyEvent {
            r#type: "ready",
            control: &control_view,
            geometry,
            terminal_asset_version: "termdeck-xterm-v2",
        },
    )
    .await?;

    let (mut sender, mut receiver) = socket.split();
    let mut heartbeat = interval(HEARTBEAT_INTERVAL);
    heartbeat.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut session_check = interval(SESSION_CHECK_INTERVAL);
    session_check.set_missed_tick_behavior(MissedTickBehavior::Delay);

    loop {
        tokio::select! {
            notification = control.notification() => {
                let Some(notification) = notification else {
                    return Err(StreamError::ControlClosed);
                };
                match notification {
                    ControlNotification::Output(output) => {
                        let output = control.coalesce_output(output);
                        send_sink_message(&mut sender, Message::Binary(output.into())).await?;
                    }
                    ControlNotification::ReplayBarrier(_) => {}
                    ControlNotification::LayoutChanged => {
                        let observed = control.current_geometry().await?;
                        if observed != geometry {
                            geometry = send_sink_resync(
                                &mut sender,
                                &mut control,
                                &control_view,
                                viewport_geometry,
                            )
                            .await?;
                        }
                    }
                }
            }
            message = receiver.next() => {
                let Some(message) = message else {
                    break;
                };
                // Recheck before input, focus/takeover, resize or any reply.
                // The periodic branch separately closes an idle/output-only
                // socket. SQLite work stays off the terminal executor thread.
                require_stream_session(&auth, &context).await?;
                match message.map_err(|_| StreamError::ControlClosed)? {
                    Message::Binary(frame) => {
                        let (sequence, bytes, kind) = parse_input_frame(frame.as_ref())?;
                        // Passive mouse input must never take a pane from another
                        // controller, including when the browser's lease view
                        // is stale. authorize_input checks the current lease.
                        if kind == InputKind::Raw && !control_view.mine {
                            control_view = terminal
                                .acquire(pane.pane_id.as_str(), actor.as_str(), unix_ms(), true)
                                .await?;
                            control.set_writer_mode(true, viewport_geometry).await?;
                            send_sink_json(&mut sender, &ControlEvent { r#type: "control", control: &control_view, focused: false }).await?;
                        }
                        let decision = terminal
                            .authorize_input(
                                pane.pane_id.as_str(),
                                actor.as_str(),
                                sequence,
                                kind.domain(),
                                bytes,
                                unix_ms(),
                            )
                            .await?;
                        match decision {
                            InputDecision::Duplicate(view) => {
                                control_view = view;
                                send_sink_json(&mut sender, &AckEvent { r#type: "ack", sequence, control: &control_view, duplicate: true }).await?;
                            }
                            InputDecision::Execute(permit) => {
                                let delivered = match kind {
                                    InputKind::Raw | InputKind::MouseMotion => control.send_input(bytes).await,
                                    InputKind::MouseWheel => control.send_mouse_wheel(bytes).await,
                                    InputKind::ApplicationScroll => control.send_application_scroll(bytes).await,
                                };
                                match delivered {
                                    Ok(()) => {
                                        control_view = terminal.complete_input(permit, InputOutcome::Committed, unix_ms()).await?;
                                        send_sink_json(&mut sender, &AckEvent { r#type: "ack", sequence, control: &control_view, duplicate: false }).await?;
                                    }
                                    Err(error) => {
                                        let _result = terminal.complete_input(permit, InputOutcome::Indeterminate, unix_ms()).await;
                                        return Err(error);
                                    }
                                }
                            }
                        }
                    }
                    Message::Text(text) => {
                        if text.len() > MAX_CLIENT_TEXT_BYTES {
                            return Err(StreamError::ClientProtocol("text frame is too large"));
                        }
                        match serde_json::from_str::<ClientEvent>(text.as_str())? {
                            ClientEvent::Hello { .. } => return Err(StreamError::ClientProtocol("hello may only be sent once")),
                            ClientEvent::Focus => {
                                // The cached view can predate HTTP management or
                                // another stream. Focus must check current ownership.
                                control_view = terminal.focus(pane.pane_id.as_str(), actor.as_str(), unix_ms()).await?;
                                control.set_writer_mode(control_view.mine, viewport_geometry).await?;
                                send_sink_json(&mut sender, &ControlEvent { r#type: "control", control: &control_view, focused: true }).await?;
                            }
                            ClientEvent::Resize { cols, rows } => {
                                viewport_geometry = TerminalGeometry::new(cols, rows)?;
                                if control_view.mine {
                                    control.set_writer_mode(true, viewport_geometry).await?;
                                    let observed = control.current_geometry().await?;
                                    if observed != geometry {
                                        geometry = send_sink_resync(
                                            &mut sender,
                                            &mut control,
                                            &control_view,
                                            viewport_geometry,
                                        ).await?;
                                    } else {
                                        send_sink_json(&mut sender, &ResyncedEvent {
                                            r#type: "resynced",
                                            control: &control_view,
                                            geometry,
                                        }).await?;
                                    }
                                }
                            }
                            ClientEvent::Resync { cols, rows } => {
                                viewport_geometry = TerminalGeometry::new(cols, rows)?;
                                geometry = send_sink_resync(
                                    &mut sender,
                                    &mut control,
                                    &control_view,
                                    viewport_geometry,
                                )
                                .await?;
                            }
                            ClientEvent::Ping => {
                                send_sink_json(&mut sender, &PongEvent { r#type: "pong" }).await?;
                            }
                        }
                    }
                    Message::Ping(value) => {
                        send_sink_message(&mut sender, Message::Pong(value)).await?;
                    }
                    Message::Pong(_) => {}
                    Message::Close(_) => break,
                }
            }
            _ = heartbeat.tick() => {
                if control_view.mine {
                    control_view = match terminal.heartbeat(pane.pane_id.as_str(), actor.as_str(), unix_ms()).await {
                        Ok(value) => value,
                        Err(_error) => terminal.status(pane.pane_id.as_str(), actor.as_str(), unix_ms()).await?,
                    };
                    control.set_writer_mode(control_view.mine, viewport_geometry).await?;
                    send_sink_json(&mut sender, &ControlEvent { r#type: "control", control: &control_view, focused: false }).await?;
                }
            }
            _ = session_check.tick() => {
                require_stream_session(&auth, &context).await?;
            }
        }
    }
    control.shutdown().await;
    Ok(())
}

async fn require_stream_session(
    auth: &Arc<HttpAuth>,
    context: &SessionContext,
) -> Result<(), StreamError> {
    let auth = Arc::clone(auth);
    let context = context.clone();
    tokio::task::spawn_blocking(move || auth.require_session(&context, unix_ms()))
        .await
        .map_err(|_| StreamError::SessionUnavailable)?
        .map_err(|_| StreamError::SessionUnavailable)
}

async fn send_sink_resync<S>(
    sender: &mut S,
    control: &mut TmuxControl,
    control_view: &TerminalControlView,
    viewport_geometry: TerminalGeometry,
) -> Result<TerminalGeometry, StreamError>
where
    S: futures_util::Sink<Message> + Unpin,
    S::Error: fmt::Debug,
{
    // Assert the writer grid before capturing so the replay is taken at the
    // size the browser will display; resizing after the capture would force a
    // second layout-change resync.
    control
        .set_writer_mode(control_view.mine, viewport_geometry)
        .await?;
    let replay = control.initial_replay().await?;
    let geometry = replay.geometry;
    send_sink_json(
        sender,
        &ResetEvent {
            r#type: "reset",
            geometry,
        },
    )
    .await?;
    send_sink_message(sender, Message::Binary(replay.bytes.into())).await?;
    send_sink_json(
        sender,
        &ResyncedEvent {
            r#type: "resynced",
            control: control_view,
            geometry,
        },
    )
    .await?;
    Ok(geometry)
}

fn error_code(error: &StreamError) -> &'static str {
    match error {
        StreamError::SessionUnavailable => "authentication-required",
        StreamError::Backpressure => "viewer-backpressure",
        StreamError::ClientProtocol(_) | StreamError::Json(_) => "invalid-client-message",
        StreamError::ControlClosed | StreamError::ControlProtocol(_) => "terminal-disconnected",
        StreamError::Io(_) | StreamError::Timeout(_) | StreamError::Tmux(_) => {
            "terminal-transport-failed"
        }
        StreamError::Terminal(TerminalError::NotController) => "controller-changed",
        StreamError::Terminal(TerminalError::Indeterminate) => "input-indeterminate",
        StreamError::Terminal(TerminalError::InvalidSequence { .. }) => "input-sequence-mismatch",
        StreamError::Terminal(_) => "terminal-action-failed",
    }
}

async fn receive_hello(socket: &mut WebSocket) -> Result<TerminalGeometry, StreamError> {
    let message = timeout(HELLO_TIMEOUT, socket.recv())
        .await
        .map_err(|_| StreamError::Timeout("client hello"))?
        .ok_or(StreamError::ControlClosed)?
        .map_err(|_| StreamError::ControlClosed)?;
    let Message::Text(text) = message else {
        return Err(StreamError::ClientProtocol(
            "first frame must be hello JSON",
        ));
    };
    if text.len() > MAX_CLIENT_TEXT_BYTES {
        return Err(StreamError::ClientProtocol("hello is too large"));
    }
    let ClientEvent::Hello { cols, rows } = serde_json::from_str(text.as_str())? else {
        return Err(StreamError::ClientProtocol(
            "first frame must be hello JSON",
        ));
    };
    TerminalGeometry::new(cols, rows)
}

fn validate_geometry(columns: u16, rows: u16) -> Result<(), StreamError> {
    if !(MIN_COLUMNS..=MAX_COLUMNS).contains(&columns) || !(MIN_ROWS..=MAX_ROWS).contains(&rows) {
        return Err(StreamError::ClientProtocol(
            "terminal geometry is outside bounds",
        ));
    }
    Ok(())
}

fn parse_input_frame(frame: &[u8]) -> Result<(u64, &[u8], InputKind), StreamError> {
    if frame.len() < 10 {
        return Err(StreamError::ClientProtocol("invalid terminal input frame"));
    }
    let mut kind = match frame[0] {
        INPUT_FRAME_TYPE => InputKind::Raw,
        MOUSE_WHEEL_FRAME_TYPE => InputKind::MouseWheel,
        APPLICATION_SCROLL_FRAME_TYPE => InputKind::ApplicationScroll,
        _ => return Err(StreamError::ClientProtocol("invalid terminal input frame")),
    };
    let sequence = u64::from_be_bytes(
        frame[1..9]
            .try_into()
            .map_err(|_| StreamError::ClientProtocol("invalid terminal sequence"))?,
    );
    let bytes = &frame[9..];
    if bytes.is_empty() || bytes.len() > MAX_TERMINAL_INPUT_BYTES {
        return Err(StreamError::ClientProtocol(
            "terminal input size is outside bounds",
        ));
    }
    if kind == InputKind::MouseWheel && !is_mouse_wheel_packet(bytes) {
        return Err(StreamError::ClientProtocol("invalid terminal mouse wheel"));
    }
    if kind == InputKind::ApplicationScroll && application_scroll_key(bytes).is_none() {
        return Err(StreamError::ClientProtocol("invalid application scroll"));
    }
    // Mouse motion is generated while hovering, before a wheel gesture can
    // reach its own handler. Even an older or stale browser must not turn a
    // pure motion batch into an implicit control request. Clicks and mixed
    // keyboard input keep their existing explicit-intent behavior.
    if kind == InputKind::Raw && is_mouse_motion_payload(bytes) {
        kind = InputKind::MouseMotion;
    }
    Ok((sequence, bytes, kind))
}

/// Admit one vertical wheel report, never keys or a concatenated paste. xterm
/// emits SGR (also used by pixel mouse mode) or the six-byte legacy encoding.
fn is_mouse_wheel_packet(bytes: &[u8]) -> bool {
    mouse_packet(bytes)
        .is_some_and(|(button, length)| length == bytes.len() && matches!(button & !28, 64 | 65))
}

fn is_mouse_motion_payload(mut bytes: &[u8]) -> bool {
    if bytes.is_empty() {
        return false;
    }
    while !bytes.is_empty() {
        let Some((button, length)) = mouse_packet(bytes) else {
            return false;
        };
        if !matches!(button & !28, 32..=35) {
            return false;
        }
        bytes = &bytes[length..];
    }
    true
}

fn mouse_packet(bytes: &[u8]) -> Option<(u32, usize)> {
    if bytes.starts_with(b"\x1b[M") {
        if bytes.len() >= 6 && bytes[3] >= 32 && bytes[4] >= 33 && bytes[5] >= 33 {
            return Some((u32::from(bytes[3] - 32), 6));
        }
        return None;
    }
    let end = bytes
        .iter()
        .take(MAX_MOUSE_WHEEL_BYTES)
        .position(|byte| *byte == b'M')?;
    let bytes = &bytes[..=end];
    if bytes.len() > MAX_MOUSE_WHEEL_BYTES {
        return None;
    }
    let encoded = bytes
        .strip_prefix(b"\x1b[<")
        .and_then(|value| value.strip_suffix(b"M"))?;
    let fields = encoded.split(|byte| *byte == b';').collect::<Vec<_>>();
    if fields.len() != 3
        || fields.iter().any(|field| {
            field.is_empty() || field.len() > 6 || !field.iter().all(u8::is_ascii_digit)
        })
    {
        return None;
    }
    let values = fields
        .iter()
        .map(|field| {
            std::str::from_utf8(field)
                .ok()
                .and_then(|field| field.parse::<u32>().ok())
        })
        .collect::<Option<Vec<_>>>();
    // SGR pixel coordinates are zero based, unlike cell coordinates.
    values.map(|value| (value[0], bytes.len()))
}

fn application_scroll_key(bytes: &[u8]) -> Option<&'static str> {
    match bytes {
        b"\x1b[5~" => Some("PageUp"),
        b"\x1b[6~" => Some("PageDown"),
        _ => None,
    }
}

fn application_scroll_command(pane_id: &str, bytes: &[u8]) -> Result<String, StreamError> {
    validate_tmux_pane_id(pane_id)?;
    let key = application_scroll_key(bytes)
        .ok_or(StreamError::ClientProtocol("invalid application scroll"))?;
    // Evaluate mode inside tmux's command queue. A stale browser gesture must
    // not become shell/prompt input after the application exits or enables
    // its mouse protocol. Navigation never implicitly acquires control.
    Ok(format!(
        "if-shell -F -t {pane_id} '#{{&&:#{{alternate_on}},#{{!:{MOUSE_REPORTING_FORMAT}}}}}' 'send-keys -t {pane_id} {key}'"
    ))
}

fn mouse_wheel_command(pane_id: &str, bytes: &[u8]) -> Result<String, StreamError> {
    validate_tmux_pane_id(pane_id)?;
    if !is_mouse_wheel_packet(bytes) {
        return Err(StreamError::ClientProtocol("invalid terminal mouse wheel"));
    }
    let mut input = format!("send-keys -t {pane_id} -H");
    for byte in bytes {
        use std::fmt::Write as _;
        write!(&mut input, " {byte:02x}")
            .map_err(|_| StreamError::ControlProtocol("input encoding failed"))?;
    }
    // Check the application's mouse mode inside tmux's command queue. A mode
    // change after browser rendering therefore cannot leak a wheel report to
    // a shell or a prompt that has stopped accepting mouse events.
    let sgr = u8::from(bytes.starts_with(b"\x1b[<"));
    let format = format!("#{{&&:{MOUSE_REPORTING_FORMAT},#{{==:#{{mouse_sgr_flag}},{sgr}}}}}");
    Ok(format!("if-shell -F -t {pane_id} '{format}' '{input}'"))
}

async fn send_json(socket: &mut WebSocket, value: &impl Serialize) -> Result<(), StreamError> {
    let text = serde_json::to_string(value)?;
    send_message(socket, Message::Text(text.into())).await
}

async fn send_message(socket: &mut WebSocket, message: Message) -> Result<(), StreamError> {
    timeout(WEBSOCKET_SEND_TIMEOUT, socket.send(message))
        .await
        .map_err(|_| StreamError::Backpressure)?
        .map_err(|_| StreamError::ControlClosed)
}

async fn send_sink_json<S>(sink: &mut S, value: &impl Serialize) -> Result<(), StreamError>
where
    S: futures_util::Sink<Message> + Unpin,
    S::Error: fmt::Debug,
{
    let text = serde_json::to_string(value)?;
    send_sink_message(sink, Message::Text(text.into())).await
}

async fn send_sink_message<S>(sink: &mut S, message: Message) -> Result<(), StreamError>
where
    S: futures_util::Sink<Message> + Unpin,
    S::Error: fmt::Debug,
{
    timeout(WEBSOCKET_SEND_TIMEOUT, sink.send(message))
        .await
        .map_err(|_| StreamError::Backpressure)?
        .map_err(|_| StreamError::ControlClosed)
}

struct TmuxControl {
    request_tx: mpsc::Sender<CommandRequest>,
    output_rx: mpsc::UnboundedReceiver<ControlNotification>,
    pending_output: Arc<AtomicUsize>,
    deferred: Option<ControlNotification>,
    next_barrier: u64,
    shutdown_tx: Option<oneshot::Sender<()>>,
    actor: tokio::task::JoinHandle<()>,
    command_timeout: Duration,
    pane_id: String,
    target: String,
}

type ReplySender = oneshot::Sender<Result<Vec<ControlReply>, StreamError>>;

struct CommandRequest {
    command: String,
    /// Number of `%begin` reply blocks: one per command in a `;` list.
    blocks: usize,
    barrier: Option<u64>,
    reply: ReplySender,
}

struct ControlReply {
    lines: Vec<Vec<u8>>,
}

struct PendingReply {
    reply: ReplySender,
    remaining: usize,
    barrier: Option<u64>,
    replies: Vec<ControlReply>,
}

struct ActiveBlock {
    identity: [Vec<u8>; 3],
    owned: bool,
    lines: Vec<Vec<u8>>,
    bytes: usize,
    greeting: bool,
}

/// Actor-side viewer output queue with an explicit byte bound.
struct OutputQueue {
    tx: mpsc::UnboundedSender<ControlNotification>,
    pending_bytes: Arc<AtomicUsize>,
}

impl OutputQueue {
    fn push(&self, notification: ControlNotification) -> Result<(), StreamError> {
        let cost = notification_cost(&notification);
        let queued = self
            .pending_bytes
            .fetch_add(cost, Ordering::AcqRel)
            .saturating_add(cost);
        if queued > MAX_PENDING_OUTPUT_BYTES {
            return Err(StreamError::Backpressure);
        }
        self.tx
            .send(notification)
            .map_err(|_| StreamError::ControlClosed)
    }
}

fn notification_cost(notification: &ControlNotification) -> usize {
    match notification {
        ControlNotification::Output(bytes) => bytes.len().max(NOTIFICATION_COST_BYTES),
        ControlNotification::LayoutChanged | ControlNotification::ReplayBarrier(_) => {
            NOTIFICATION_COST_BYTES
        }
    }
}

impl TmuxControl {
    async fn spawn(
        tmux_executable: Arc<PathBuf>,
        target: String,
        command_timeout: Duration,
    ) -> Result<Self, StreamError> {
        validate_tmux_target(target.as_str())?;
        let pane_id =
            resolve_pane_id(tmux_executable.as_ref(), target.as_str(), command_timeout).await?;
        let mut command = crate::tmux_runtime::command(tmux_executable.as_ref()).await?;
        command
            .arg(CONTROL_MODE_ARGUMENT)
            .arg("attach-session")
            .arg("-f")
            .arg(CONTROL_FLAGS_VIEWER)
            .arg("-t")
            .arg(target.as_str())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        let mut child = command.spawn()?;
        let stdin = child
            .stdin
            .take()
            .ok_or(StreamError::ControlProtocol("tmux stdin unavailable"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or(StreamError::ControlProtocol("tmux stdout unavailable"))?;
        let (request_tx, request_rx) = mpsc::channel(CONTROL_REQUEST_CAPACITY);
        let (output_tx, output_rx) = mpsc::unbounded_channel();
        let pending_output = Arc::new(AtomicUsize::new(0));
        let output = OutputQueue {
            tx: output_tx,
            pending_bytes: Arc::clone(&pending_output),
        };
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let (ready_tx, ready_rx) = oneshot::channel();
        let actor_pane = pane_id.clone();
        let actor = tokio::spawn(async move {
            let _result = control_actor(ControlActorContext {
                child,
                stdin,
                stdout,
                request_rx,
                output,
                shutdown_rx,
                ready_tx,
                pane_id: actor_pane,
            })
            .await;
        });
        timeout(command_timeout, ready_rx)
            .await
            .map_err(|_| StreamError::Timeout("tmux control greeting"))?
            .map_err(|_| StreamError::ControlClosed)??;
        Ok(Self {
            request_tx,
            output_rx,
            pending_output,
            deferred: None,
            next_barrier: 0,
            shutdown_tx: Some(shutdown_tx),
            actor,
            command_timeout,
            pane_id,
            target,
        })
    }

    fn dequeued(&self, notification: ControlNotification) -> ControlNotification {
        self.pending_output
            .fetch_sub(notification_cost(&notification), Ordering::AcqRel);
        notification
    }

    async fn notification(&mut self) -> Option<ControlNotification> {
        if let Some(notification) = self.deferred.take() {
            return Some(notification);
        }
        let notification = self.output_rx.recv().await?;
        Some(self.dequeued(notification))
    }

    fn try_notification(&mut self) -> Option<ControlNotification> {
        if let Some(notification) = self.deferred.take() {
            return Some(notification);
        }
        let notification = self.output_rx.try_recv().ok()?;
        Some(self.dequeued(notification))
    }

    /// Append output that is already queued to one bounded browser frame.
    /// The first non-output notification is deferred, preserving order.
    fn coalesce_output(&mut self, mut output: Vec<u8>) -> Vec<u8> {
        while output.len() < MAX_OUTPUT_FRAME_BYTES {
            match self.try_notification() {
                Some(ControlNotification::Output(next))
                    if output.len().saturating_add(next.len()) <= MAX_OUTPUT_FRAME_BYTES =>
                {
                    output.extend_from_slice(&next);
                }
                Some(other) => {
                    self.deferred = Some(other);
                    break;
                }
                None => break,
            }
        }
        output
    }

    /// Drop notifications queued before `barrier`; later output is kept.
    fn discard_until_barrier(&mut self, barrier: u64) -> Result<(), StreamError> {
        while let Some(notification) = self.try_notification() {
            if matches!(notification, ControlNotification::ReplayBarrier(value) if value == barrier)
            {
                return Ok(());
            }
        }
        Err(StreamError::ControlProtocol(
            "replay barrier was not observed",
        ))
    }

    async fn command(&self, command: String) -> Result<ControlReply, StreamError> {
        self.command_blocks(command, 1, None)
            .await?
            .pop()
            .ok_or(StreamError::ControlProtocol("tmux reply was empty"))
    }

    async fn command_blocks(
        &self,
        command: String,
        blocks: usize,
        barrier: Option<u64>,
    ) -> Result<Vec<ControlReply>, StreamError> {
        if command.is_empty()
            || command.len() > MAX_CONTROL_COMMAND_BYTES
            || command.contains(['\n', '\r', '\0'])
            || !(1..=MAX_COMMAND_BLOCKS).contains(&blocks)
        {
            return Err(StreamError::ControlProtocol("invalid control command"));
        }
        let (reply_tx, reply_rx) = oneshot::channel();
        self.request_tx
            .send(CommandRequest {
                command,
                blocks,
                barrier,
                reply: reply_tx,
            })
            .await
            .map_err(|_| StreamError::ControlClosed)?;
        let replies = timeout(self.command_timeout, reply_rx)
            .await
            .map_err(|_| StreamError::Timeout("tmux control command"))?
            .map_err(|_| StreamError::ControlClosed)??;
        if replies.len() != blocks {
            return Err(StreamError::ControlProtocol(
                "tmux reply block count changed",
            ));
        }
        Ok(replies)
    }

    async fn set_writer_mode(
        &self,
        writer: bool,
        geometry: TerminalGeometry,
    ) -> Result<(), StreamError> {
        let flags = if writer {
            CONTROL_FLAGS_WRITER
        } else {
            CONTROL_FLAGS_VIEWER
        };
        self.command(format!(
            "refresh-client -f {flags} -C {}x{}",
            geometry.cols, geometry.rows
        ))
        .await?;
        Ok(())
    }

    async fn send_input(&self, bytes: &[u8]) -> Result<(), StreamError> {
        if bytes.is_empty() || bytes.len() > MAX_TERMINAL_INPUT_BYTES {
            return Err(StreamError::ClientProtocol(
                "terminal input size is outside bounds",
            ));
        }
        for chunk in bytes.chunks(INPUT_CHUNK_BYTES) {
            let mut command = format!("send-keys -t {} -H", self.pane_id);
            for byte in chunk {
                use std::fmt::Write as _;
                write!(&mut command, " {byte:02x}")
                    .map_err(|_| StreamError::ControlProtocol("input encoding failed"))?;
            }
            self.command(command).await?;
        }
        Ok(())
    }

    async fn send_mouse_wheel(&self, bytes: &[u8]) -> Result<(), StreamError> {
        self.command(mouse_wheel_command(self.pane_id.as_str(), bytes)?)
            .await?;
        Ok(())
    }

    async fn send_application_scroll(&self, bytes: &[u8]) -> Result<(), StreamError> {
        self.command(application_scroll_command(self.pane_id.as_str(), bytes)?)
            .await?;
        Ok(())
    }

    fn state_command(&self) -> String {
        format!("list-panes -t {} -F '{}'", self.target, PANE_STATE_FORMAT)
    }

    async fn current_state(&self) -> Result<PaneState, StreamError> {
        let state_reply = self.command(self.state_command()).await?;
        self.select_state(state_reply.lines.as_slice())
    }

    fn select_state(&self, lines: &[Vec<u8>]) -> Result<PaneState, StreamError> {
        let mut selected = None;
        // list-panes returns every pane in the containing window, even for an
        // explicit adopted pane target. Match the identity pinned at attach.
        for line in lines {
            let state = PaneState::parse(line)?;
            if state.pane_id == self.pane_id && selected.replace(state).is_some() {
                return Err(StreamError::ControlProtocol("duplicate pane identity"));
            }
        }
        selected.ok_or(StreamError::ControlProtocol(
            "pane identity changed during attach",
        ))
    }

    async fn current_geometry(&self) -> Result<TerminalGeometry, StreamError> {
        Ok(self.current_state().await?.geometry)
    }

    /// Capture a replay without an output gap.
    ///
    /// tmux runs one control line's command list without reading pane output
    /// in between, so pause, pane state, captures, and continue in a single
    /// line leave no window in which pane bytes are applied after the capture
    /// but before output resumes (those bytes were previously discarded by
    /// the pause). Output queued before the pause is already part of the
    /// capture and is discarded up to the replay barrier.
    async fn initial_replay(&mut self) -> Result<ReplaySnapshot, StreamError> {
        let sizing = self.current_state().await?;
        let requested_history =
            initial_replay_history_lines(sizing.history_size, sizing.geometry.cols);
        self.next_barrier = self.next_barrier.wrapping_add(1);
        let barrier = self.next_barrier;
        let mut commands = vec![
            refresh_pane_action(self.pane_id.as_str(), "pause")?,
            self.state_command(),
        ];
        if requested_history > 0 {
            commands.push(format!(
                "capture-pane -epq -t {} -J -S -{} -E -1",
                self.pane_id, requested_history
            ));
        }
        commands.push(format!("capture-pane -epq -t {} -N", self.pane_id));
        commands.push(refresh_pane_action(self.pane_id.as_str(), "continue")?);
        let count = commands.len();
        let mut replies = self
            .command_blocks(commands.join(" ; "), count, Some(barrier))
            .await?;
        self.discard_until_barrier(barrier)?;
        let visible = std::mem::take(&mut replies[count - 2].lines);
        let mut history = if requested_history > 0 {
            std::mem::take(&mut replies[2].lines)
        } else {
            Vec::new()
        };
        let state = self.select_state(replies[1].lines.as_slice())?;
        let geometry = state.geometry;
        // The pane may have been resized between sizing and the transaction.
        let history_bound = initial_replay_history_lines(state.history_size, geometry.cols);
        if history.len() > history_bound {
            history.drain(..history.len() - history_bound);
        }
        let bytes = build_replay(&state, geometry.rows, history, visible)?;
        Ok(ReplaySnapshot { geometry, bytes })
    }

    async fn shutdown(&mut self) {
        if let Some(shutdown) = self.shutdown_tx.take() {
            let _result = shutdown.send(());
        }
        let _result = timeout(Duration::from_secs(2), &mut self.actor).await;
        self.actor.abort();
    }
}

async fn resolve_pane_id(
    tmux_executable: &Path,
    target: &str,
    command_timeout: Duration,
) -> Result<String, StreamError> {
    let mut command = crate::tmux_runtime::command(tmux_executable).await?;
    command
        .arg("display-message")
        .arg("-p")
        .arg("-t")
        .arg(target)
        .arg("#{pane_id}")
        .kill_on_drop(true);
    let output = timeout(command_timeout, command.output())
        .await
        .map_err(|_| StreamError::Timeout("pane identity"))??;
    if !output.status.success() {
        return Err(StreamError::Tmux(bounded_lossy(&output.stderr)));
    }
    let value = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    validate_tmux_pane_id(value.as_str())?;
    Ok(value)
}

struct ControlActorContext {
    child: Child,
    stdin: ChildStdin,
    stdout: ChildStdout,
    request_rx: mpsc::Receiver<CommandRequest>,
    output: OutputQueue,
    shutdown_rx: oneshot::Receiver<()>,
    ready_tx: oneshot::Sender<Result<(), StreamError>>,
    pane_id: String,
}

async fn control_actor(context: ControlActorContext) -> Result<(), StreamError> {
    let ControlActorContext {
        mut child,
        mut stdin,
        stdout,
        mut request_rx,
        output,
        mut shutdown_rx,
        ready_tx,
        pane_id,
    } = context;
    let mut reader = BufReader::new(stdout);
    let mut line = Vec::new();
    let mut pending = VecDeque::new();
    let mut active: Option<ActiveBlock> = None;
    let mut greeting_pending = true;
    let mut ready_tx = Some(ready_tx);
    let result = loop {
        tokio::select! {
            _ = &mut shutdown_rx => break Ok(()),
            request = request_rx.recv() => {
                let Some(request) = request else { break Ok(()); };
                pending.push_back(PendingReply {
                    reply: request.reply,
                    remaining: request.blocks,
                    barrier: request.barrier,
                    replies: Vec::with_capacity(request.blocks),
                });
                if let Err(error) = stdin.write_all(request.command.as_bytes()).await {
                    let entry = pending.pop_back();
                    if let Some(entry) = entry { let _result = entry.reply.send(Err(StreamError::Io(error))); }
                    break Err(StreamError::ControlClosed);
                }
                if stdin.write_all(b"\n").await.is_err() || stdin.flush().await.is_err() {
                    break Err(StreamError::ControlClosed);
                }
            }
            read = reader.read_until(b'\n', &mut line) => {
                let count = read?;
                if count == 0 { break Err(StreamError::ControlClosed); }
                let line_bound = if active.is_some() {
                    MAX_CONTROL_LINE_BYTES
                } else {
                    MAX_CONTROL_NOTIFICATION_LINE_BYTES
                };
                if line.len() > line_bound { break Err(StreamError::ControlProtocol("control line exceeded bound")); }
                trim_line_ending(&mut line);
                if let Err(error) = handle_control_line(
                    line.as_slice(),
                    &pane_id,
                    &mut greeting_pending,
                    &mut ready_tx,
                    &mut pending,
                    &mut active,
                    &output,
                ) {
                    break Err(error);
                }
                line.clear();
            }
        }
    };
    if let Some(ready) = ready_tx.take() {
        let _result = ready.send(Err(StreamError::ControlClosed));
    }
    while let Some(entry) = pending.pop_front() {
        let _result = entry.reply.send(Err(StreamError::ControlClosed));
    }
    let _result = child.kill().await;
    result
}

fn handle_control_line(
    line: &[u8],
    pane_id: &str,
    greeting_pending: &mut bool,
    ready_tx: &mut Option<oneshot::Sender<Result<(), StreamError>>>,
    pending: &mut VecDeque<PendingReply>,
    active: &mut Option<ActiveBlock>,
    output: &OutputQueue,
) -> Result<(), StreamError> {
    if let Some((kind, identity)) = parse_marker(line) {
        match kind {
            Marker::Begin => {
                if active.is_some() {
                    append_body(active, line)?;
                    return Ok(());
                }
                let greeting = *greeting_pending;
                if greeting {
                    *greeting_pending = false;
                }
                // Reply blocks arrive in request order; the oldest pending
                // request owns every block until its command list completes.
                let owner = if greeting { None } else { pending.front_mut() };
                let owned = owner.is_some();
                if let Some(barrier) = owner.and_then(|entry| entry.barrier.take()) {
                    output.push(ControlNotification::ReplayBarrier(barrier))?;
                }
                *active = Some(ActiveBlock {
                    identity,
                    owned,
                    lines: Vec::new(),
                    bytes: 0,
                    greeting,
                });
                return Ok(());
            }
            Marker::End | Marker::Error => {
                let Some(block) = active.as_ref() else {
                    return Ok(());
                };
                if block.identity != identity {
                    append_body(active, line)?;
                    return Ok(());
                }
                let block = active
                    .take()
                    .ok_or(StreamError::ControlProtocol("active block vanished"))?;
                if block.greeting {
                    if let Some(ready) = ready_tx.take() {
                        let _result = ready.send(Ok(()));
                    }
                } else if block.owned {
                    complete_block(pending, kind, block.lines)?;
                }
                return Ok(());
            }
        }
    }
    if active.is_some() {
        append_body(active, line)?;
        return Ok(());
    }
    if let Some(encoded) = output_payload(line, pane_id.as_bytes())
        .or_else(|| extended_output_payload(line, pane_id.as_bytes()))
    {
        let bytes = decode_tmux_output(encoded);
        if !bytes.is_empty() {
            output.push(ControlNotification::Output(bytes))?;
        }
        return Ok(());
    }
    if line.starts_with(b"%layout-change ") {
        output.push(ControlNotification::LayoutChanged)?;
        return Ok(());
    }
    if line.starts_with(b"%pause ") || line.starts_with(b"%continue ") {
        return Ok(());
    }
    if line.starts_with(b"%exit") {
        return Err(StreamError::ControlClosed);
    }
    Ok(())
}

fn complete_block(
    pending: &mut VecDeque<PendingReply>,
    kind: Marker,
    lines: Vec<Vec<u8>>,
) -> Result<(), StreamError> {
    let entry = pending
        .front_mut()
        .ok_or(StreamError::ControlProtocol("reply owner vanished"))?;
    if kind == Marker::Error {
        // tmux skips the rest of a command list after an error, so no more
        // blocks will arrive for this request.
        if let Some(entry) = pending.pop_front() {
            let _result = entry
                .reply
                .send(Err(StreamError::Tmux(body_error(lines.as_slice()))));
        }
        return Ok(());
    }
    entry.replies.push(ControlReply { lines });
    entry.remaining = entry.remaining.saturating_sub(1);
    if entry.remaining == 0 {
        if let Some(entry) = pending.pop_front() {
            let _result = entry.reply.send(Ok(entry.replies));
        }
    }
    Ok(())
}

fn append_body(active: &mut Option<ActiveBlock>, line: &[u8]) -> Result<(), StreamError> {
    let block = active
        .as_mut()
        .ok_or(StreamError::ControlProtocol("body without active block"))?;
    block.bytes = block
        .bytes
        .checked_add(line.len())
        .ok_or(StreamError::ControlProtocol("control body overflow"))?;
    if block.bytes > MAX_CONTROL_BODY_BYTES {
        return Err(StreamError::ControlProtocol("control body exceeded bound"));
    }
    block.lines.push(line.to_vec());
    Ok(())
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Marker {
    Begin,
    End,
    Error,
}

fn parse_marker(line: &[u8]) -> Option<(Marker, [Vec<u8>; 3])> {
    let mut fields = line.split(|byte| *byte == b' ');
    let kind = match fields.next()? {
        b"%begin" => Marker::Begin,
        b"%end" => Marker::End,
        b"%error" => Marker::Error,
        _ => return None,
    };
    let first = fields.next()?;
    let second = fields.next()?;
    let third = fields.next()?;
    if fields.next().is_some()
        || [first, second, third]
            .iter()
            .any(|field| field.is_empty() || !field.iter().all(u8::is_ascii_digit))
    {
        return None;
    }
    Some((kind, [first.to_vec(), second.to_vec(), third.to_vec()]))
}

fn output_payload<'a>(line: &'a [u8], pane_id: &[u8]) -> Option<&'a [u8]> {
    let rest = line.strip_prefix(b"%output ")?;
    let separator = rest.iter().position(|byte| *byte == b' ')?;
    if &rest[..separator] != pane_id {
        return None;
    }
    Some(&rest[separator + 1..])
}

fn extended_output_payload<'a>(line: &'a [u8], pane_id: &[u8]) -> Option<&'a [u8]> {
    let rest = line.strip_prefix(b"%extended-output ")?;
    let separator = rest.windows(3).position(|window| window == b" : ")?;
    let header = &rest[..separator];
    let observed_pane = header.split(|byte| *byte == b' ').next()?;
    if observed_pane != pane_id {
        return None;
    }
    Some(&rest[separator + 3..])
}

fn decode_tmux_output(encoded: &[u8]) -> Vec<u8> {
    let mut decoded = Vec::with_capacity(encoded.len());
    let mut index = 0;
    while index < encoded.len() {
        if encoded[index] != b'\\' || index + 1 >= encoded.len() {
            decoded.push(encoded[index]);
            index += 1;
            continue;
        }
        if index + 3 < encoded.len()
            && encoded[index + 1..index + 4]
                .iter()
                .all(|byte| (b'0'..=b'7').contains(byte))
        {
            let value = (encoded[index + 1] - b'0') * 64
                + (encoded[index + 2] - b'0') * 8
                + (encoded[index + 3] - b'0');
            decoded.push(value);
            index += 4;
        } else if encoded[index + 1] == b'\\' {
            decoded.push(b'\\');
            index += 2;
        } else {
            decoded.push(b'\\');
            index += 1;
        }
    }
    decoded
}

struct PaneState {
    pane_id: String,
    geometry: TerminalGeometry,
    alternate: bool,
    cursor_x: u16,
    cursor_y: u16,
    cursor_visible: bool,
    keypad_cursor: bool,
    wrap: bool,
    insert: bool,
    origin: bool,
    mouse_standard: bool,
    mouse_button: bool,
    mouse_all: bool,
    mouse_sgr: bool,
    bracket_paste: bool,
    history_size: u32,
}

impl PaneState {
    fn parse(line: &[u8]) -> Result<Self, StreamError> {
        let value = std::str::from_utf8(line)
            .map_err(|_| StreamError::ControlProtocol("pane state was not ASCII"))?;
        let fields = value.split('\t').collect::<Vec<_>>();
        if fields.len() != 19 {
            return Err(StreamError::ControlProtocol(
                "pane state field count changed",
            ));
        }
        validate_tmux_pane_id(fields[0])?;
        let _pane_pid = parse_u32(fields[1], "pane pid")?;
        let geometry = TerminalGeometry::new(
            parse_u16(fields[2], "pane width")?,
            parse_u16(fields[3], "pane height")?,
        )?;
        let _pane_dead = flag(fields[5])?;
        Ok(Self {
            pane_id: fields[0].to_owned(),
            geometry,
            alternate: flag(fields[4])?,
            cursor_x: parse_u16(fields[6], "cursor x")?,
            cursor_y: parse_u16(fields[7], "cursor y")?,
            cursor_visible: flag(fields[8])?,
            keypad_cursor: flag(fields[9])?,
            wrap: flag(fields[10])?,
            insert: flag(fields[11])?,
            origin: flag(fields[12])?,
            mouse_standard: flag(fields[13])?,
            mouse_button: flag(fields[14])?,
            mouse_all: flag(fields[15])?,
            mouse_sgr: flag(fields[16])?,
            bracket_paste: flag(fields[17])?,
            history_size: parse_u32(fields[18], "history size")?,
        })
    }
}

fn initial_replay_history_lines(history_size: u32, columns: u16) -> usize {
    let width = usize::from(columns.max(1));
    let geometry_bound = (MAX_INITIAL_REPLAY_JOINED_CELLS / width).max(1);
    usize::try_from(history_size)
        .unwrap_or(usize::MAX)
        .min(MAX_CAPTURE_HISTORY_LINES as usize)
        .min(MAX_INITIAL_REPLAY_HISTORY_LINES)
        .min(geometry_bound)
}

fn build_replay(
    state: &PaneState,
    rows: u16,
    mut history: Vec<Vec<u8>>,
    mut visible: Vec<Vec<u8>>,
) -> Result<Vec<u8>, StreamError> {
    let mut output = Vec::with_capacity(64 * 1024);
    extend_bounded(&mut output, b"\x1b[0m\x1b[?6l\x1b[4l\x1b[?7l\x1b[?25l")?;
    for line in &mut history {
        while line
            .last()
            .is_some_and(|byte| matches!(byte, b' ' | b'\t' | b'\r'))
        {
            line.pop();
        }
    }
    if history.iter().any(|line| !line.is_empty()) {
        paint_rows(&mut output, history.as_slice(), true)?;
    }
    if state.alternate {
        extend_bounded(&mut output, b"\x1b[?1049h")?;
        extend_bounded(&mut output, b"\x1b[2J\x1b[H")?;
    }
    // The browser resets its terminal immediately before this replay. On the
    // normal screen, painting the visible rows after history scrolls the last
    // viewport of history into xterm's scrollback. Clearing here would erase
    // those retained rows instead. The alternate screen starts separately and
    // still needs its own clear/home sequence.
    visible.truncate(usize::from(rows));
    paint_rows(&mut output, visible.as_slice(), false)?;
    mode(&mut output, state.keypad_cursor, b"\x1b[?1h", b"\x1b[?1l")?;
    mode(&mut output, state.wrap, b"\x1b[?7h", b"\x1b[?7l")?;
    mode(&mut output, state.insert, b"\x1b[4h", b"\x1b[4l")?;
    mode(&mut output, state.origin, b"\x1b[?6h", b"\x1b[?6l")?;
    // DEC mouse tracking modes 1000, 1002, and 1003 are mutually exclusive.
    // Reset them as a group before enabling the pane's strongest active mode;
    // resetting an inactive mode after enabling another makes xterm.js report
    // `none` and silently drops browser mouse events.
    extend_bounded(&mut output, b"\x1b[?1000l\x1b[?1002l\x1b[?1003l")?;
    if state.mouse_all {
        extend_bounded(&mut output, b"\x1b[?1003h")?;
    } else if state.mouse_button {
        extend_bounded(&mut output, b"\x1b[?1002h")?;
    } else if state.mouse_standard {
        extend_bounded(&mut output, b"\x1b[?1000h")?;
    }
    mode(&mut output, state.mouse_sgr, b"\x1b[?1006h", b"\x1b[?1006l")?;
    mode(
        &mut output,
        state.bracket_paste,
        b"\x1b[?2004h",
        b"\x1b[?2004l",
    )?;
    let cursor_y = state.cursor_y.min(rows.saturating_sub(1));
    let cursor = format!(
        "\x1b[{};{}H",
        u32::from(cursor_y) + 1,
        u32::from(state.cursor_x) + 1
    );
    extend_bounded(&mut output, cursor.as_bytes())?;
    extend_bounded(
        &mut output,
        if state.cursor_visible {
            b"\x1b[?25h"
        } else {
            b"\x1b[?25l"
        },
    )?;
    Ok(output)
}

fn paint_rows(
    output: &mut Vec<u8>,
    lines: &[Vec<u8>],
    final_newline: bool,
) -> Result<(), StreamError> {
    // Each capture command starts from the default style.
    let mut carried = SgrState::default();
    for (index, line) in lines.iter().enumerate() {
        // Erase with the default background, then restore the style tmux
        // considers active at the start of this captured row.
        extend_bounded(output, b"\x1b[0m\r\x1b[2K")?;
        carried.write_to(output)?;
        extend_bounded(output, line)?;
        extend_bounded(output, b"\x1b[0m")?;
        carried.advance(line);
        if final_newline || index + 1 < lines.len() {
            extend_bounded(output, b"\r\n")?;
        }
    }
    Ok(())
}

/// SGR state reconstructed from `capture-pane -e` rows.
///
/// tmux emits SGR changes relative to the previous cell of the same capture,
/// including across row boundaries, so a row whose first cells continue the
/// previous row's style carries no prefix of its own. Replay paints every row
/// from a reset, so the style in effect at the end of the previous row has to
/// be re-emitted; otherwise those cells lose their background (for example
/// the space cells of a Codex popup) until the application repaints them.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
struct SgrState {
    // bold, dim, italic, underline, blink, inverse, hidden, strike, overline
    attributes: [Option<Vec<u8>>; SGR_ATTRIBUTE_SLOTS],
    foreground: Option<Vec<u8>>,
    background: Option<Vec<u8>>,
    underline_colour: Option<Vec<u8>>,
}

impl SgrState {
    const BOLD: usize = 0;
    const DIM: usize = 1;
    const ITALIC: usize = 2;
    const UNDERLINE: usize = 3;
    const BLINK: usize = 4;
    const INVERSE: usize = 5;
    const HIDDEN: usize = 6;
    const STRIKE: usize = 7;
    const OVERLINE: usize = 8;

    fn advance(&mut self, line: &[u8]) {
        let mut index = 0;
        while index + 1 < line.len() {
            if line[index] != 0x1b || line[index + 1] != b'[' {
                index += 1;
                continue;
            }
            let start = index + 2;
            let mut end = start;
            while end < line.len() && matches!(line[end], b'0'..=b'9' | b';' | b':') {
                end += 1;
            }
            if line.get(end) == Some(&b'm') {
                self.apply(&line[start..end]);
                index = end + 1;
            } else {
                // Another CSI sequence (for example a private mode); skip it.
                index = start;
            }
        }
    }

    fn apply(&mut self, parameters: &[u8]) {
        let groups: Vec<&[u8]> = parameters.split(|byte| *byte == b';').collect();
        let mut index = 0;
        while index < groups.len() {
            let group = groups[index];
            index += 1;
            if group.len() > MAX_SGR_GROUP_BYTES {
                continue;
            }
            let code_text = group.split(|byte| *byte == b':').next().unwrap_or_default();
            let code = if code_text.is_empty() {
                Some(0)
            } else {
                std::str::from_utf8(code_text)
                    .ok()
                    .and_then(|text| text.parse::<u16>().ok())
            };
            let Some(code) = code else {
                continue;
            };
            match code {
                0 => *self = Self::default(),
                1 => self.attributes[Self::BOLD] = Some(group.to_vec()),
                2 => self.attributes[Self::DIM] = Some(group.to_vec()),
                3 => self.attributes[Self::ITALIC] = Some(group.to_vec()),
                4 if group == b"4:0" => self.attributes[Self::UNDERLINE] = None,
                4 | 21 => self.attributes[Self::UNDERLINE] = Some(group.to_vec()),
                5 | 6 => self.attributes[Self::BLINK] = Some(group.to_vec()),
                7 => self.attributes[Self::INVERSE] = Some(group.to_vec()),
                8 => self.attributes[Self::HIDDEN] = Some(group.to_vec()),
                9 => self.attributes[Self::STRIKE] = Some(group.to_vec()),
                53 => self.attributes[Self::OVERLINE] = Some(group.to_vec()),
                22 => {
                    self.attributes[Self::BOLD] = None;
                    self.attributes[Self::DIM] = None;
                }
                23 => self.attributes[Self::ITALIC] = None,
                24 => self.attributes[Self::UNDERLINE] = None,
                25 => self.attributes[Self::BLINK] = None,
                27 => self.attributes[Self::INVERSE] = None,
                28 => self.attributes[Self::HIDDEN] = None,
                29 => self.attributes[Self::STRIKE] = None,
                55 => self.attributes[Self::OVERLINE] = None,
                30..=37 | 90..=97 => self.foreground = Some(group.to_vec()),
                39 => self.foreground = None,
                40..=47 | 100..=107 => self.background = Some(group.to_vec()),
                49 => self.background = None,
                59 => self.underline_colour = None,
                38 | 48 | 58 => {
                    let colour = if group.contains(&b':') {
                        group.to_vec()
                    } else {
                        // Semicolon form consumes `5;n` or `2;r;g;b`.
                        let extra = match groups.get(index).copied() {
                            Some(b"5") => 2,
                            Some(b"2") => 4,
                            _ => 0,
                        };
                        let end = index.saturating_add(extra).min(groups.len());
                        let mut value = group.to_vec();
                        for part in &groups[index..end] {
                            value.push(b';');
                            value.extend_from_slice(part);
                        }
                        index = end;
                        value
                    };
                    if colour.len() <= MAX_SGR_GROUP_BYTES {
                        match code {
                            38 => self.foreground = Some(colour),
                            48 => self.background = Some(colour),
                            _ => self.underline_colour = Some(colour),
                        }
                    }
                }
                _ => {}
            }
        }
    }

    fn write_to(&self, output: &mut Vec<u8>) -> Result<(), StreamError> {
        let mut sequence = Vec::new();
        for part in self
            .attributes
            .iter()
            .flatten()
            .chain(self.foreground.iter())
            .chain(self.background.iter())
            .chain(self.underline_colour.iter())
        {
            let separator: &[u8] = if sequence.is_empty() { b"\x1b[" } else { b";" };
            sequence.extend_from_slice(separator);
            sequence.extend_from_slice(part);
        }
        if sequence.is_empty() {
            return Ok(());
        }
        sequence.push(b'm');
        extend_bounded(output, sequence.as_slice())
    }
}

fn mode(output: &mut Vec<u8>, enabled: bool, set: &[u8], reset: &[u8]) -> Result<(), StreamError> {
    extend_bounded(output, if enabled { set } else { reset })
}

fn extend_bounded(output: &mut Vec<u8>, bytes: &[u8]) -> Result<(), StreamError> {
    let projected = output
        .len()
        .checked_add(bytes.len())
        .ok_or(StreamError::ControlProtocol("replay length overflow"))?;
    if projected > MAX_REPLAY_BYTES {
        return Err(StreamError::ControlProtocol(
            "terminal replay exceeded bound",
        ));
    }
    output.extend_from_slice(bytes);
    Ok(())
}

fn refresh_pane_action(pane_id: &str, action: &str) -> Result<String, StreamError> {
    validate_tmux_pane_id(pane_id)?;
    if !matches!(action, "pause" | "continue") {
        return Err(StreamError::ControlProtocol(
            "tmux pane output action is invalid",
        ));
    }
    Ok(format!("refresh-client -A '{pane_id}:{action}'"))
}

fn validate_tmux_target(value: &str) -> Result<(), StreamError> {
    if value.is_empty()
        || value.len() > 256
        || !value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':' | b'%' | b'+')
        })
    {
        return Err(StreamError::ControlProtocol(
            "tmux target contains unsupported characters",
        ));
    }
    Ok(())
}

fn validate_tmux_pane_id(value: &str) -> Result<(), StreamError> {
    if value.len() < 2
        || value.len() > 32
        || !value.starts_with('%')
        || !value[1..].bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err(StreamError::ControlProtocol(
            "tmux pane identity is invalid",
        ));
    }
    Ok(())
}

fn validate_termdeck_id(value: &str) -> Result<(), StreamError> {
    if value.is_empty() || value.len() > 256 || value.chars().any(char::is_control) {
        return Err(StreamError::ClientProtocol("pane identity is invalid"));
    }
    Ok(())
}

fn flag(value: &str) -> Result<bool, StreamError> {
    match value {
        "0" => Ok(false),
        "1" => Ok(true),
        _ => Err(StreamError::ControlProtocol("tmux flag is invalid")),
    }
}

fn parse_u16(value: &str, field: &'static str) -> Result<u16, StreamError> {
    value
        .parse()
        .map_err(|_| StreamError::ControlProtocol(field))
}

fn parse_u32(value: &str, field: &'static str) -> Result<u32, StreamError> {
    value
        .parse()
        .map_err(|_| StreamError::ControlProtocol(field))
}

fn trim_line_ending(line: &mut Vec<u8>) {
    if line.last() == Some(&b'\n') {
        line.pop();
    }
    if line.last() == Some(&b'\r') {
        line.pop();
    }
}

fn body_error(lines: &[Vec<u8>]) -> String {
    let mut joined = Vec::new();
    for (index, line) in lines.iter().enumerate() {
        if index > 0 {
            joined.push(b' ');
        }
        let remaining = 2_048usize.saturating_sub(joined.len());
        if remaining == 0 {
            break;
        }
        joined.extend_from_slice(&line[..line.len().min(remaining)]);
    }
    let value = String::from_utf8_lossy(joined.as_slice()).trim().to_owned();
    if value.is_empty() {
        "tmux command failed".to_owned()
    } else {
        value
    }
}

fn bounded_lossy(value: &[u8]) -> String {
    String::from_utf8_lossy(&value[..value.len().min(2_048)])
        .trim()
        .to_owned()
}

fn unix_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}

#[cfg(test)]
mod tests {
    use super::{
        CONTROL_FLAGS_VIEWER, CONTROL_FLAGS_WRITER, CONTROL_MODE_ARGUMENT, ControlNotification,
        InputKind, MAX_INITIAL_REPLAY_JOINED_CELLS, MAX_OUTPUT_FRAME_BYTES,
        MAX_PENDING_OUTPUT_BYTES, OutputQueue, PANE_STATE_FORMAT, PaneState, PendingReply,
        StreamError, TerminalGeometry, application_scroll_command, build_replay,
        decode_tmux_output, extended_output_payload, handle_control_line,
        initial_replay_history_lines, is_mouse_wheel_packet, mouse_wheel_command, output_payload,
        parse_input_frame, parse_marker, refresh_pane_action, validate_geometry,
    };
    use std::collections::VecDeque;
    use std::sync::Arc;
    use std::sync::atomic::AtomicUsize;

    #[test]
    fn application_scroll_admits_only_fenced_page_navigation() {
        for (bytes, key) in [
            (b"\x1b[5~".as_slice(), "PageUp"),
            (b"\x1b[6~".as_slice(), "PageDown"),
        ] {
            let mut frame = vec![3];
            frame.extend_from_slice(&7_u64.to_be_bytes());
            frame.extend_from_slice(bytes);
            let (sequence, payload, kind) = parse_input_frame(&frame)
                .unwrap_or_else(|error| panic!("valid application scroll: {error}"));
            assert_eq!(sequence, 7);
            assert_eq!(payload, bytes);
            assert_eq!(kind, InputKind::ApplicationScroll);
            assert_eq!(kind.domain(), b"websocket-application-scroll");
            let command = application_scroll_command("%7", bytes)
                .unwrap_or_else(|error| panic!("valid application scroll command: {error}"));
            assert!(command.contains("#{alternate_on}"));
            assert!(command.contains("#{!:"));
            assert!(command.ends_with(&format!("'send-keys -t %7 {key}'")));
        }
        for bytes in [
            b"\x1b[A".as_slice(),
            b"\x1b[5~\x1b[6~",
            b"run-shell x",
            b"\x1b[5;2~",
            b"",
        ] {
            let mut frame = vec![3];
            frame.extend_from_slice(&7_u64.to_be_bytes());
            frame.extend_from_slice(bytes);
            assert!(parse_input_frame(&frame).is_err());
            assert!(application_scroll_command("%7", bytes).is_err());
        }
        assert!(application_scroll_command("%7; kill-session", b"\x1b[5~").is_err());
    }

    #[test]
    fn tmux_control_mode_and_state_format_are_pipe_safe() {
        assert_eq!(CONTROL_MODE_ARGUMENT, "-C");
        assert_eq!(
            PANE_STATE_FORMAT
                .as_bytes()
                .iter()
                .filter(|byte| **byte == b'\t')
                .count(),
            18
        );
        assert!(
            !PANE_STATE_FORMAT
                .as_bytes()
                .windows(2)
                .any(|window| window == b"\\t")
        );

        let state =
            PaneState::parse(b"%7\t123\t120\t40\t1\t0\t4\t5\t1\t0\t1\t0\t0\t1\t0\t0\t1\t1\t42")
                .unwrap_or_else(|error| panic!("pane state: {error}"));
        assert_eq!(state.pane_id, "%7");
        assert_eq!(
            state.geometry,
            TerminalGeometry {
                cols: 120,
                rows: 40
            }
        );
        assert!(state.alternate);
        assert_eq!(state.cursor_x, 4);
        assert_eq!(state.cursor_y, 5);
        assert_eq!(state.history_size, 42);
        assert_eq!(
            refresh_pane_action("%7", "pause")
                .unwrap_or_else(|error| panic!("pause command: {error}")),
            "refresh-client -A '%7:pause'"
        );
        assert_eq!(
            refresh_pane_action("%7", "continue")
                .unwrap_or_else(|error| panic!("continue command: {error}")),
            "refresh-client -A '%7:continue'"
        );
        assert!(refresh_pane_action("%7", "run-shell").is_err());
        assert_eq!(MAX_INITIAL_REPLAY_JOINED_CELLS, 4_000_000);
        assert!(CONTROL_FLAGS_VIEWER.contains("ignore-size"));
        assert!(CONTROL_FLAGS_WRITER.contains("!ignore-size"));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn current_state_selects_non_first_adopted_pane_and_rejects_missing_identity() {
        let first = b"%3\t100\t80\t24\t0\t0\t0\t0\t1\t0\t1\t0\t0\t0\t0\t0\t0\t0\t0";
        let adopted = b"%7\t123\t120\t40\t1\t0\t4\t5\t1\t0\t1\t0\t0\t1\t0\t0\t1\t1\t42";
        for lines in [
            vec![first.to_vec(), adopted.to_vec()],
            vec![first.to_vec()],
            vec![adopted.to_vec(), adopted.to_vec()],
        ] {
            let expected_success = lines.len() == 2 && lines[0].as_slice() == first;
            let (request_tx, mut request_rx) =
                tokio::sync::mpsc::channel::<super::CommandRequest>(1);
            let (_output_tx, output_rx) = tokio::sync::mpsc::unbounded_channel();
            let actor = tokio::spawn(async move {
                let request = request_rx
                    .recv()
                    .await
                    .unwrap_or_else(|| panic!("state command was not sent"));
                assert_eq!(
                    request.command,
                    format!("list-panes -t %7 -F '{}'", PANE_STATE_FORMAT)
                );
                assert!(
                    request
                        .reply
                        .send(Ok(vec![super::ControlReply { lines }]))
                        .is_ok()
                );
            });
            let mut control = super::TmuxControl {
                request_tx,
                output_rx,
                pending_output: Arc::new(AtomicUsize::new(0)),
                deferred: None,
                next_barrier: 0,
                shutdown_tx: None,
                actor,
                command_timeout: std::time::Duration::from_secs(1),
                pane_id: "%7".to_owned(),
                target: "%7".to_owned(),
            };
            let result = control.current_state().await;
            if expected_success {
                let state = result.unwrap_or_else(|error| panic!("adopted state: {error}"));
                assert_eq!(state.pane_id, "%7");
                assert_eq!(
                    state.geometry,
                    TerminalGeometry {
                        cols: 120,
                        rows: 40
                    }
                );
                assert!(state.alternate);
                assert_eq!(state.history_size, 42);
            } else {
                assert!(matches!(result, Err(StreamError::ControlProtocol(_))));
            }
            control.shutdown().await;
        }
    }

    #[test]
    fn initial_history_replay_retains_configured_depth_at_any_valid_geometry() {
        assert_eq!(initial_replay_history_lines(u32::MAX, 2), 4_000);
        assert_eq!(initial_replay_history_lines(u32::MAX, 80), 4_000);
        assert_eq!(initial_replay_history_lines(u32::MAX, 181), 4_000);
        assert_eq!(initial_replay_history_lines(u32::MAX, 1_000), 4_000);
        assert_eq!(initial_replay_history_lines(2_000, 181), 2_000);
        assert_eq!(initial_replay_history_lines(7, 1_000), 7);
    }

    #[test]
    fn replay_keeps_distant_normal_screen_history_in_one_terminal_stream() {
        let state = PaneState {
            pane_id: "%1".to_owned(),
            geometry: TerminalGeometry {
                cols: 181,
                rows: 49,
            },
            alternate: false,
            cursor_x: 0,
            cursor_y: 48,
            cursor_visible: true,
            keypad_cursor: false,
            wrap: true,
            insert: false,
            origin: false,
            mouse_standard: false,
            mouse_button: false,
            mouse_all: false,
            mouse_sgr: false,
            bracket_paste: false,
            history_size: 2_000,
        };
        let history = (0..2_000)
            .map(|index| format!("history-row-{index:04}").into_bytes())
            .collect();
        let replay = build_replay(&state, 49, history, vec![b"live-row".to_vec()])
            .unwrap_or_else(|error| panic!("replay: {error}"));
        for marker in [
            b"history-row-0000".as_slice(),
            b"history-row-1999",
            b"live-row",
        ] {
            assert!(replay.windows(marker.len()).any(|window| window == marker));
        }
        assert_eq!(
            replay
                .windows(2)
                .filter(|window| *window == b"\r\n")
                .count(),
            2_000
        );
        assert!(replay.len() < super::MAX_REPLAY_BYTES);
    }

    #[test]
    fn oversized_replay_still_fails_closed() {
        let state = plain_state();
        let source = vec![b'x'; super::MAX_REPLAY_BYTES];
        assert!(matches!(
            build_replay(&state, 24, vec![source], vec![b"visible".to_vec()]),
            Err(StreamError::ControlProtocol(
                "terminal replay exceeded bound"
            ))
        ));
    }

    #[test]
    fn tmux_output_decoder_preserves_exact_bytes() {
        assert_eq!(
            decode_tmux_output(br"A\015B\\C\303\251"),
            b"A\rB\\C\xc3\xa9"
        );
        assert_eq!(output_payload(br"%output %7 hi", b"%7"), Some(&b"hi"[..]));
        assert_eq!(output_payload(br"%output %8 hi", b"%7"), None);
        assert_eq!(
            extended_output_payload(br"%extended-output %7 12 : delayed\015", b"%7"),
            Some(&br"delayed\015"[..])
        );
        assert_eq!(
            extended_output_payload(br"%extended-output %8 12 : delayed", b"%7"),
            None
        );
    }

    #[test]
    fn marker_parser_rejects_lookalikes() {
        assert!(parse_marker(b"%begin 1 2 3").is_some());
        assert!(parse_marker(b"%begin one 2 3").is_none());
        assert!(parse_marker(b"%end 1 2 3 extra").is_none());
    }

    #[test]
    fn binary_input_frame_is_bounded_and_sequenced() {
        let mut frame = vec![1];
        frame.extend_from_slice(&7_u64.to_be_bytes());
        frame.extend_from_slice(b"abc");
        let (sequence, bytes, kind) =
            parse_input_frame(frame.as_slice()).unwrap_or_else(|error| panic!("frame: {error}"));
        assert_eq!(sequence, 7);
        assert_eq!(bytes, b"abc");
        assert_eq!(kind, InputKind::Raw);
        assert!(matches!(
            parse_input_frame(&[1; 9]),
            Err(StreamError::ClientProtocol(_))
        ));
    }

    #[test]
    fn wheel_frames_admit_only_one_bounded_vertical_mouse_report() {
        for packet in [
            &b"\x1b[<64;1;2M"[..],
            &b"\x1b[<65;1000;500M"[..],
            &b"\x1b[<92;10;20M"[..],
            &b"\x1b[<64;0;0M"[..],
            &b"\x1b[M\x60\x21\x22"[..],
            &b"\x1b[M\x61\xfe\xff"[..],
        ] {
            let mut frame = vec![2];
            frame.extend_from_slice(&8_u64.to_be_bytes());
            frame.extend_from_slice(packet);
            let (sequence, bytes, kind) =
                parse_input_frame(&frame).unwrap_or_else(|error| panic!("wheel frame: {error}"));
            assert_eq!(sequence, 8);
            assert_eq!(bytes, packet);
            assert_eq!(kind, InputKind::MouseWheel);
            assert_ne!(kind.domain(), InputKind::Raw.domain());
        }
        for packet in [
            &b"\x1b[A"[..],
            &b"\x14\x1b[A"[..],
            &b"\x1b[<0;1;2M"[..],
            &b"\x1b[<66;1;2M"[..],
            &b"\x1b[<64;1;2m"[..],
            &b"\x1b[<64;-1;2M"[..],
            &b"\x1b[<64;1;2;3M"[..],
            &b"\x1b[<64;1;2M\x1b[<65;1;2M"[..],
            &b"\x1b[<64;1;2M\ncommand"[..],
            &b"\x1b[M\x60\x20\x22"[..],
            &b"\x1b[M\x20\x21\x22"[..],
            &b"\x1b[<64;1234567;1M"[..],
        ] {
            let mut frame = vec![2];
            frame.extend_from_slice(&8_u64.to_be_bytes());
            frame.extend_from_slice(packet);
            assert!(!is_mouse_wheel_packet(packet));
            assert!(matches!(
                parse_input_frame(&frame),
                Err(StreamError::ClientProtocol(_))
            ));
        }
        assert!(!is_mouse_wheel_packet(&[b'0'; 65]));
    }

    #[test]
    fn wheel_commands_are_hex_encoded_and_guarded_by_current_mouse_mode() {
        let command = mouse_wheel_command("%7", b"\x1b[<64;1;2M")
            .unwrap_or_else(|error| panic!("wheel command: {error}"));
        assert_eq!(
            command,
            "if-shell -F -t %7 '#{&&:#{||:#{mouse_standard_flag},#{||:#{mouse_button_flag},#{mouse_all_flag}}},#{==:#{mouse_sgr_flag},1}}' 'send-keys -t %7 -H 1b 5b 3c 36 34 3b 31 3b 32 4d'"
        );
        assert!(mouse_wheel_command("%7; kill-session", b"\x1b[<64;1;2M").is_err());
        assert!(mouse_wheel_command("%7", b"\x1b[A").is_err());
    }

    #[test]
    fn pure_mouse_motion_is_passive_but_click_and_keyboard_intent_are_preserved() {
        for packet in [
            &b"\x1b[<35;5;3M"[..],
            &b"\x1b[<32;5;3M\x1b[<63;6;4M"[..],
            &b"\x1b[MC\x22\x23"[..],
            &b"\x1b[<35;5;3M\x1b[MC\x22\x23"[..],
        ] {
            let mut frame = vec![1];
            frame.extend_from_slice(&1_u64.to_be_bytes());
            frame.extend_from_slice(packet);
            let (_, bytes, kind) =
                parse_input_frame(&frame).unwrap_or_else(|error| panic!("motion frame: {error}"));
            assert_eq!(bytes, packet);
            assert_eq!(kind, InputKind::MouseMotion);
            assert_ne!(kind.domain(), InputKind::Raw.domain());
            frame[0] = 2;
            assert!(parse_input_frame(&frame).is_err());
        }
        for packet in [
            &b"\x1b[<0;5;3M"[..],
            &b"\x1b[<0;5;3m"[..],
            &b"\x1b[<35;5;3Mhello"[..],
            &b"\x1b[<35;5;3M\x1b[<0;5;3M"[..],
            &b"\x1b[<35;5;3"[..],
            &b"hello"[..],
        ] {
            let mut frame = vec![1];
            frame.extend_from_slice(&1_u64.to_be_bytes());
            frame.extend_from_slice(packet);
            let (_, _, kind) =
                parse_input_frame(&frame).unwrap_or_else(|error| panic!("ordinary frame: {error}"));
            assert_eq!(kind, InputKind::Raw);
        }
    }

    #[test]
    fn replay_restores_ansi_modes_and_cursor() {
        let state = PaneState {
            pane_id: "%1".to_owned(),
            geometry: TerminalGeometry { cols: 80, rows: 24 },
            alternate: true,
            cursor_x: 4,
            cursor_y: 2,
            cursor_visible: true,
            keypad_cursor: true,
            wrap: true,
            insert: false,
            origin: false,
            mouse_standard: true,
            mouse_button: false,
            mouse_all: false,
            mouse_sgr: true,
            bracket_paste: true,
            history_size: 1,
        };
        let replay = build_replay(
            &state,
            24,
            vec![b"history".to_vec()],
            vec![b"\x1b[31mred".to_vec()],
        )
        .unwrap_or_else(|error| panic!("replay: {error}"));
        for expected in [
            &b"\x1b[?1049h"[..],
            &b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1000h"[..],
            &b"\x1b[?1006h"[..],
            &b"\x1b[?2004h"[..],
            &b"\x1b[3;5H"[..],
            &b"\x1b[31m"[..],
        ] {
            assert!(
                replay
                    .windows(expected.len())
                    .any(|window| window == expected),
                "missing replay sequence {expected:?}"
            );
        }
    }

    #[test]
    fn geometry_has_explicit_limits() {
        assert!(validate_geometry(80, 24).is_ok());
        assert!(validate_geometry(1, 24).is_err());
        assert!(validate_geometry(80, 501).is_err());
    }

    fn plain_state() -> PaneState {
        PaneState {
            pane_id: "%1".to_owned(),
            geometry: TerminalGeometry { cols: 80, rows: 24 },
            alternate: false,
            cursor_x: 0,
            cursor_y: 0,
            cursor_visible: true,
            keypad_cursor: false,
            wrap: true,
            insert: false,
            origin: false,
            mouse_standard: false,
            mouse_button: false,
            mouse_all: false,
            mouse_sgr: false,
            bracket_paste: false,
            history_size: 0,
        }
    }

    fn contains(haystack: &[u8], needle: &[u8]) -> bool {
        haystack
            .windows(needle.len())
            .any(|window| window == needle)
    }

    #[test]
    fn replay_carries_sgr_state_across_captured_rows() {
        // tmux 3.7c `capture-pane -e -N`: a popup row that continues the
        // previous row's background is captured without an SGR prefix.
        let visible = vec![
            b"\x1b[48;2;30;30;30m    AAA".to_vec(),
            b"    BBB".to_vec(),
            b"\x1b[1;38:2::216:230:242mX\x1b[22;49mY".to_vec(),
            b"Z".to_vec(),
            b"\x1b[4;7;38;5;196mQ\x1b[?25l\x1b[m".to_vec(),
            b"R".to_vec(),
        ];
        let history = vec![b"\x1b[44mhistory".to_vec(), b"tail".to_vec()];
        let mut state = plain_state();
        state.history_size = 2;
        let replay = build_replay(&state, 24, history, visible)
            .unwrap_or_else(|error| panic!("replay: {error}"));
        assert!(contains(
            &replay,
            b"\x1b[0m\r\x1b[2K\x1b[48;2;30;30;30m    BBB"
        ));
        assert!(contains(
            &replay,
            b"\x1b[0m\r\x1b[2K\x1b[48;2;30;30;30m\x1b[1;38:2::216:230:242mX"
        ));
        // 22 clears bold and 49 the background; only the colour is carried.
        assert!(contains(
            &replay,
            b"\x1b[0m\r\x1b[2K\x1b[38:2::216:230:242mZ"
        ));
        // An empty SGR is a full reset, and private CSI sequences are ignored.
        assert!(contains(&replay, b"\x1b[0m\r\x1b[2KR"));
        // History and the visible screen are separate captures, but the
        // normal-screen replay must not clear the last history viewport.
        assert!(contains(&replay, b"\x1b[0m\r\x1b[2K\x1b[44mtail"));
        assert!(contains(
            &replay,
            b"\r\n\x1b[0m\r\x1b[2K\x1b[48;2;30;30;30m    AAA"
        ));
        assert!(!contains(&replay, b"\x1b[2J\x1b[H"));
    }

    fn output_queue() -> (
        OutputQueue,
        tokio::sync::mpsc::UnboundedReceiver<ControlNotification>,
        Arc<AtomicUsize>,
    ) {
        let (tx, rx) = tokio::sync::mpsc::unbounded_channel();
        let pending_bytes = Arc::new(AtomicUsize::new(0));
        (
            OutputQueue {
                tx,
                pending_bytes: Arc::clone(&pending_bytes),
            },
            rx,
            pending_bytes,
        )
    }

    fn feed(lines: &[&[u8]], pending: &mut VecDeque<PendingReply>, output: &OutputQueue) {
        let mut greeting = false;
        let mut ready = None;
        let mut active = None;
        for line in lines {
            handle_control_line(
                line,
                "%1",
                &mut greeting,
                &mut ready,
                pending,
                &mut active,
                output,
            )
            .unwrap_or_else(|error| panic!("control line {line:?}: {error}"));
        }
    }

    fn pending_reply(
        remaining: usize,
        barrier: Option<u64>,
    ) -> (
        PendingReply,
        tokio::sync::oneshot::Receiver<Result<Vec<super::ControlReply>, StreamError>>,
    ) {
        let (reply, receiver) = tokio::sync::oneshot::channel();
        (
            PendingReply {
                reply,
                remaining,
                barrier,
                replies: Vec::new(),
            },
            receiver,
        )
    }

    #[test]
    fn command_list_blocks_follow_the_replay_barrier() {
        let (output, mut notifications, _pending_bytes) = output_queue();
        let (entry, mut receiver) = pending_reply(3, Some(7));
        let mut pending = VecDeque::from([entry]);
        // Framing observed from tmux 3.7c for `pause ; list-panes ; continue`:
        // one block per command, %pause/%continue inside their own block.
        feed(
            &[
                b"%output %1 stale",
                b"%begin 1790621011 300 1",
                b"%pause %1",
                b"%end 1790621011 300 1",
                b"%begin 1790621011 301 1",
                b"state",
                b"%end 1790621011 301 1",
                b"%begin 1790621011 302 1",
                b"%continue %1",
                b"%end 1790621011 302 1",
                b"%output %1 fresh",
            ],
            &mut pending,
            &output,
        );
        let replies = receiver
            .try_recv()
            .unwrap_or_else(|error| panic!("reply: {error}"))
            .unwrap_or_else(|error| panic!("tmux: {error}"));
        let bodies: Vec<Vec<Vec<u8>>> = replies.into_iter().map(|reply| reply.lines).collect();
        assert_eq!(
            bodies,
            vec![
                vec![b"%pause %1".to_vec()],
                vec![b"state".to_vec()],
                vec![b"%continue %1".to_vec()]
            ]
        );
        assert!(pending.is_empty());
        let mut order = Vec::new();
        while let Ok(notification) = notifications.try_recv() {
            order.push(match notification {
                ControlNotification::Output(bytes) => String::from_utf8_lossy(&bytes).into_owned(),
                ControlNotification::ReplayBarrier(value) => format!("barrier-{value}"),
                ControlNotification::LayoutChanged => "layout".to_owned(),
            });
        }
        assert_eq!(order, ["stale", "barrier-7", "fresh"]);
    }

    #[test]
    fn command_list_error_completes_request_and_keeps_replies_aligned() {
        let (output, _notifications, _pending_bytes) = output_queue();
        let (first, mut first_receiver) = pending_reply(3, None);
        let (second, mut second_receiver) = pending_reply(1, None);
        let mut pending = VecDeque::from([first, second]);
        // tmux 3.7c skips the rest of a command list after `%error`.
        feed(
            &[
                b"%begin 1 20 1",
                b"A",
                b"%end 1 20 1",
                b"%begin 1 21 1",
                b"can't find pane: %999",
                b"%error 1 21 1",
                b"%begin 1 22 1",
                b"next",
                b"%end 1 22 1",
            ],
            &mut pending,
            &output,
        );
        assert!(matches!(
            first_receiver.try_recv(),
            Ok(Err(StreamError::Tmux(message))) if message == "can't find pane: %999"
        ));
        let second = second_receiver
            .try_recv()
            .unwrap_or_else(|error| panic!("reply: {error}"))
            .unwrap_or_else(|error| panic!("tmux: {error}"));
        assert_eq!(second.len(), 1);
        assert_eq!(second[0].lines, vec![b"next".to_vec()]);
        assert!(pending.is_empty());
    }

    #[tokio::test(flavor = "current_thread")]
    async fn viewer_output_is_coalesced_in_order_and_discarded_only_before_barrier() {
        let (output, output_rx, pending_output) = output_queue();
        let (request_tx, _request_rx) = tokio::sync::mpsc::channel(1);
        let mut control = super::TmuxControl {
            request_tx,
            output_rx,
            pending_output: Arc::clone(&pending_output),
            deferred: None,
            next_barrier: 0,
            shutdown_tx: None,
            actor: tokio::spawn(async {}),
            command_timeout: std::time::Duration::from_secs(1),
            pane_id: "%1".to_owned(),
            target: "%1".to_owned(),
        };
        for notification in [
            ControlNotification::Output(b"old".to_vec()),
            ControlNotification::ReplayBarrier(1),
            ControlNotification::Output(b"a".to_vec()),
            ControlNotification::Output(b"b".to_vec()),
            ControlNotification::LayoutChanged,
            ControlNotification::Output(vec![b'x'; MAX_OUTPUT_FRAME_BYTES - 1]),
            ControlNotification::Output(b"yy".to_vec()),
        ] {
            output
                .push(notification)
                .unwrap_or_else(|error| panic!("push: {error}"));
        }
        control
            .discard_until_barrier(1)
            .unwrap_or_else(|error| panic!("barrier: {error}"));
        let Some(ControlNotification::Output(first)) = control.notification().await else {
            panic!("expected output after the barrier");
        };
        assert_eq!(control.coalesce_output(first), b"ab");
        assert!(matches!(
            control.notification().await,
            Some(ControlNotification::LayoutChanged)
        ));
        let Some(ControlNotification::Output(large)) = control.notification().await else {
            panic!("expected large output");
        };
        assert_eq!(
            control.coalesce_output(large).len(),
            MAX_OUTPUT_FRAME_BYTES - 1
        );
        assert!(matches!(
            control.notification().await,
            Some(ControlNotification::Output(bytes)) if bytes == b"yy"
        ));
        assert_eq!(pending_output.load(std::sync::atomic::Ordering::Acquire), 0);
        assert!(control.discard_until_barrier(2).is_err());
        control.shutdown().await;
    }

    #[test]
    fn viewer_output_queue_is_bounded_by_bytes_not_line_count() {
        let (output, _notifications, pending_bytes) = output_queue();
        // Far beyond the previous 32-notification queue.
        for _ in 0..4_096 {
            output
                .push(ControlNotification::Output(b"x".to_vec()))
                .unwrap_or_else(|error| panic!("push: {error}"));
        }
        assert!(
            pending_bytes.load(std::sync::atomic::Ordering::Acquire) < MAX_PENDING_OUTPUT_BYTES
        );
        assert!(matches!(
            output.push(ControlNotification::Output(vec![
                0;
                MAX_PENDING_OUTPUT_BYTES
            ])),
            Err(StreamError::Backpressure)
        ));
    }
}
