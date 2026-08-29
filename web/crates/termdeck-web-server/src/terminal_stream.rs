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
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tokio::sync::{mpsc, oneshot};
use tokio::time::{MissedTickBehavior, interval, timeout};

const HELLO_TIMEOUT: Duration = Duration::from_secs(10);
const WEBSOCKET_SEND_TIMEOUT: Duration = Duration::from_secs(5);
const MAX_CLIENT_TEXT_BYTES: usize = 4 * 1024;
const MAX_CONTROL_LINE_BYTES: usize = 256 * 1024;
const MAX_CONTROL_BODY_BYTES: usize = 8 * 1024 * 1024;
const MAX_REPLAY_BYTES: usize = 8 * 1024 * 1024;
const MAX_INITIAL_REPLAY_HISTORY_LINES: usize = 512;
// `capture-pane -J` preserves soft-wrap semantics, but it can join many physical
// rows into one control-protocol line. Bound joined source cells independently
// of history depth so even the maximum accepted geometry cannot create an
// oversized control line or a multi-second replay burst.
const MAX_INITIAL_REPLAY_JOINED_CELLS: usize = 32 * 1024;
const _: () = assert!(MAX_INITIAL_REPLAY_HISTORY_LINES <= 512);
const CONTROL_REQUEST_CAPACITY: usize = 64;
const OUTPUT_QUEUE_CAPACITY: usize = 32;
const INPUT_CHUNK_BYTES: usize = 256;
const MIN_COLUMNS: u16 = 2;
const MAX_COLUMNS: u16 = 1_000;
const MIN_ROWS: u16 = 2;
const MAX_ROWS: u16 = 500;
const INPUT_FRAME_TYPE: u8 = 1;
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);
const CONTROL_MODE_ARGUMENT: &str = "-C";
const CONTROL_FLAGS_VIEWER: &str = "active-pane,ignore-size,no-detach-on-destroy";
const CONTROL_FLAGS_WRITER: &str = "active-pane,!ignore-size,no-detach-on-destroy";
const PANE_STATE_FORMAT: &str = "#{pane_id}\t#{pane_pid}\t#{alternate_on}\t#{pane_dead}\t#{cursor_x}\t#{cursor_y}\t#{cursor_flag}\t#{keypad_cursor_flag}\t#{wrap_flag}\t#{insert_flag}\t#{origin_flag}\t#{mouse_standard_flag}\t#{mouse_button_flag}\t#{mouse_all_flag}\t#{mouse_sgr_flag}\t#{bracket_paste_flag}\t#{history_size}";

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

#[derive(Serialize)]
struct ResetEvent {
    r#type: &'static str,
}

#[derive(Serialize)]
struct ReadyEvent<'a> {
    r#type: &'static str,
    control: &'a TerminalControlView,
    terminal_asset_version: &'static str,
}

#[derive(Serialize)]
struct ControlEvent<'a> {
    r#type: &'static str,
    control: &'a TerminalControlView,
}

#[derive(Serialize)]
struct ResyncedEvent<'a> {
    r#type: &'static str,
    control: &'a TerminalControlView,
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
) {
    if let Err(error) = run_terminal_socket_inner(&mut socket, terminal, pane, actor).await {
        let message = error.to_string();
        let event = ErrorEvent {
            r#type: "error",
            code: error_code(&error),
            message: message.as_str(),
            recoverable: !matches!(error, StreamError::ClientProtocol(_)),
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
) -> Result<(), StreamError> {
    validate_termdeck_id(pane.pane_id.as_str())?;
    validate_tmux_target(pane.tmux_target.as_str())?;
    let (columns, rows) = receive_hello(socket).await?;
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
    let replay = control.initial_replay(columns, rows).await?;
    send_json(socket, &ResetEvent { r#type: "reset" }).await?;
    send_message(socket, Message::Binary(replay.into())).await?;
    control
        .set_writer_mode(control_view.mine, columns, rows)
        .await?;
    control.continue_output().await?;
    send_json(
        socket,
        &ReadyEvent {
            r#type: "ready",
            control: &control_view,
            terminal_asset_version: "termdeck-xterm-v1",
        },
    )
    .await?;

    let (mut sender, mut receiver) = socket.split();
    let mut heartbeat = interval(HEARTBEAT_INTERVAL);
    heartbeat.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut columns = columns;
    let mut rows = rows;

    loop {
        tokio::select! {
            output = control.output() => {
                let Some(output) = output else {
                    return Err(StreamError::ControlClosed);
                };
                send_sink_message(&mut sender, Message::Binary(output.into())).await?;
            }
            message = receiver.next() => {
                let Some(message) = message else {
                    break;
                };
                match message.map_err(|_| StreamError::ControlClosed)? {
                    Message::Binary(frame) => {
                        let (sequence, bytes) = parse_input_frame(frame.as_ref())?;
                        if !control_view.mine {
                            control_view = terminal
                                .acquire(pane.pane_id.as_str(), actor.as_str(), unix_ms(), true)
                                .await?;
                            control.set_writer_mode(true, columns, rows).await?;
                            send_sink_json(&mut sender, &ControlEvent { r#type: "control", control: &control_view }).await?;
                        }
                        let decision = terminal
                            .authorize_input(
                                pane.pane_id.as_str(),
                                actor.as_str(),
                                sequence,
                                b"websocket-raw",
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
                                match control.send_input(bytes).await {
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
                                if !control_view.mine {
                                    // Browser focus is the user's implicit control intent. A
                                    // remounted pane has a fresh WebSocket actor, so a
                                    // non-forcing acquire would leave the visible terminal in
                                    // viewer mode until the first keypress. Force the
                                    // generation-safe takeover here; every input frame remains
                                    // bound to the winning actor and stale writers are rejected
                                    // after the controller epoch changes.
                                    control_view = match terminal.acquire(pane.pane_id.as_str(), actor.as_str(), unix_ms(), true).await {
                                        Ok(value) => value,
                                        Err(_error) => terminal.status(pane.pane_id.as_str(), actor.as_str(), unix_ms()).await?,
                                    };
                                    control.set_writer_mode(control_view.mine, columns, rows).await?;
                                    send_sink_json(&mut sender, &ControlEvent { r#type: "control", control: &control_view }).await?;
                                }
                            }
                            ClientEvent::Resize { cols, rows: new_rows } => {
                                validate_geometry(cols, new_rows)?;
                                columns = cols;
                                rows = new_rows;
                                control.resize(columns, rows).await?;
                            }
                            ClientEvent::Resync { cols, rows: new_rows } => {
                                validate_geometry(cols, new_rows)?;
                                columns = cols;
                                rows = new_rows;
                                let replay = control.initial_replay(columns, rows).await?;
                                send_sink_json(&mut sender, &ResetEvent { r#type: "reset" }).await?;
                                send_sink_message(&mut sender, Message::Binary(replay.into())).await?;
                                control.set_writer_mode(control_view.mine, columns, rows).await?;
                                control.continue_output().await?;
                                send_sink_json(
                                    &mut sender,
                                    &ResyncedEvent {
                                        r#type: "resynced",
                                        control: &control_view,
                                    },
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
                    control.set_writer_mode(control_view.mine, columns, rows).await?;
                    send_sink_json(&mut sender, &ControlEvent { r#type: "control", control: &control_view }).await?;
                }
            }
        }
    }
    control.shutdown().await;
    Ok(())
}

fn error_code(error: &StreamError) -> &'static str {
    match error {
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

async fn receive_hello(socket: &mut WebSocket) -> Result<(u16, u16), StreamError> {
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
    validate_geometry(cols, rows)?;
    Ok((cols, rows))
}

fn validate_geometry(columns: u16, rows: u16) -> Result<(), StreamError> {
    if !(MIN_COLUMNS..=MAX_COLUMNS).contains(&columns) || !(MIN_ROWS..=MAX_ROWS).contains(&rows) {
        return Err(StreamError::ClientProtocol(
            "terminal geometry is outside bounds",
        ));
    }
    Ok(())
}

fn parse_input_frame(frame: &[u8]) -> Result<(u64, &[u8]), StreamError> {
    if frame.len() < 10 || frame[0] != INPUT_FRAME_TYPE {
        return Err(StreamError::ClientProtocol("invalid terminal input frame"));
    }
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
    Ok((sequence, bytes))
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
    output_rx: mpsc::Receiver<Vec<u8>>,
    shutdown_tx: Option<oneshot::Sender<()>>,
    actor: tokio::task::JoinHandle<()>,
    command_timeout: Duration,
    pane_id: String,
    target: String,
}

struct CommandRequest {
    command: String,
    reply: oneshot::Sender<Result<ControlReply, StreamError>>,
}

struct ControlReply {
    lines: Vec<Vec<u8>>,
}

struct ActiveBlock {
    identity: [Vec<u8>; 3],
    reply: Option<oneshot::Sender<Result<ControlReply, StreamError>>>,
    lines: Vec<Vec<u8>>,
    bytes: usize,
    greeting: bool,
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
        let mut command = Command::new(tmux_executable.as_ref());
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
        let (output_tx, output_rx) = mpsc::channel(OUTPUT_QUEUE_CAPACITY);
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let (ready_tx, ready_rx) = oneshot::channel();
        let actor_pane = pane_id.clone();
        let actor = tokio::spawn(async move {
            let _result = control_actor(ControlActorContext {
                child,
                stdin,
                stdout,
                request_rx,
                output_tx,
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
            shutdown_tx: Some(shutdown_tx),
            actor,
            command_timeout,
            pane_id,
            target,
        })
    }

    async fn output(&mut self) -> Option<Vec<u8>> {
        self.output_rx.recv().await
    }

    async fn command(&self, command: String) -> Result<ControlReply, StreamError> {
        if command.is_empty()
            || command.len() > MAX_CONTROL_LINE_BYTES
            || command.contains(['\n', '\r', '\0'])
        {
            return Err(StreamError::ControlProtocol("invalid control command"));
        }
        let (reply_tx, reply_rx) = oneshot::channel();
        self.request_tx
            .send(CommandRequest {
                command,
                reply: reply_tx,
            })
            .await
            .map_err(|_| StreamError::ControlClosed)?;
        timeout(self.command_timeout, reply_rx)
            .await
            .map_err(|_| StreamError::Timeout("tmux control command"))?
            .map_err(|_| StreamError::ControlClosed)?
    }

    async fn set_writer_mode(
        &self,
        writer: bool,
        columns: u16,
        rows: u16,
    ) -> Result<(), StreamError> {
        validate_geometry(columns, rows)?;
        let flags = if writer {
            CONTROL_FLAGS_WRITER
        } else {
            CONTROL_FLAGS_VIEWER
        };
        self.command(format!("refresh-client -f {flags} -C {columns}x{rows}"))
            .await?;
        Ok(())
    }

    async fn resize(&self, columns: u16, rows: u16) -> Result<(), StreamError> {
        validate_geometry(columns, rows)?;
        self.command(format!("refresh-client -C {columns}x{rows}"))
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

    async fn initial_replay(&mut self, columns: u16, rows: u16) -> Result<Vec<u8>, StreamError> {
        validate_geometry(columns, rows)?;
        let mut pause_command = refresh_pane_action(self.pane_id.as_str(), "pause")?;
        use std::fmt::Write as _;
        write!(&mut pause_command, " -C {columns}x{rows}")
            .map_err(|_| StreamError::ControlProtocol("pane pause command formatting failed"))?;
        self.command(pause_command).await?;
        while self.output_rx.try_recv().is_ok() {}
        let state_reply = self
            .command(format!(
                "list-panes -t {} -F '{}'",
                self.target, PANE_STATE_FORMAT
            ))
            .await?;
        let state_line = state_reply
            .lines
            .first()
            .ok_or(StreamError::ControlProtocol("pane state was empty"))?;
        let state = PaneState::parse(state_line)?;
        if state.pane_id != self.pane_id {
            return Err(StreamError::ControlProtocol(
                "pane identity changed during attach",
            ));
        }
        let history_lines = initial_replay_history_lines(state.history_size, columns);
        let history_command = (history_lines > 0).then(|| {
            format!(
                "capture-pane -epq -t {} -J -S -{} -E -1",
                self.pane_id, history_lines
            )
        });
        let history_capture = async {
            match history_command {
                Some(command) => self.command(command).await,
                None => Ok(ControlReply { lines: Vec::new() }),
            }
        };
        let visible_capture = self.command(format!("capture-pane -epq -t {} -N", self.pane_id));
        let (history, visible) = tokio::try_join!(history_capture, visible_capture)?;
        build_replay(&state, rows, history.lines, visible.lines)
    }

    async fn continue_output(&self) -> Result<(), StreamError> {
        self.command(refresh_pane_action(self.pane_id.as_str(), "continue")?)
            .await?;
        Ok(())
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
    let mut command = Command::new(tmux_executable);
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
    output_tx: mpsc::Sender<Vec<u8>>,
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
        output_tx,
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
                pending.push_back(request.reply);
                if let Err(error) = stdin.write_all(request.command.as_bytes()).await {
                    let reply = pending.pop_back();
                    if let Some(reply) = reply { let _result = reply.send(Err(StreamError::Io(error))); }
                    break Err(StreamError::ControlClosed);
                }
                if stdin.write_all(b"\n").await.is_err() || stdin.flush().await.is_err() {
                    break Err(StreamError::ControlClosed);
                }
            }
            read = reader.read_until(b'\n', &mut line) => {
                let count = read?;
                if count == 0 { break Err(StreamError::ControlClosed); }
                if line.len() > MAX_CONTROL_LINE_BYTES { break Err(StreamError::ControlProtocol("control line exceeded bound")); }
                trim_line_ending(&mut line);
                if let Err(error) = handle_control_line(
                    line.as_slice(),
                    &pane_id,
                    &mut greeting_pending,
                    &mut ready_tx,
                    &mut pending,
                    &mut active,
                    &output_tx,
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
    if let Some(block) = active.take() {
        if let Some(reply) = block.reply {
            let _result = reply.send(Err(StreamError::ControlClosed));
        }
    }
    while let Some(reply) = pending.pop_front() {
        let _result = reply.send(Err(StreamError::ControlClosed));
    }
    let _result = child.kill().await;
    result
}

fn handle_control_line(
    line: &[u8],
    pane_id: &str,
    greeting_pending: &mut bool,
    ready_tx: &mut Option<oneshot::Sender<Result<(), StreamError>>>,
    pending: &mut VecDeque<oneshot::Sender<Result<ControlReply, StreamError>>>,
    active: &mut Option<ActiveBlock>,
    output_tx: &mpsc::Sender<Vec<u8>>,
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
                *active = Some(ActiveBlock {
                    identity,
                    reply: if greeting { None } else { pending.pop_front() },
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
                } else if let Some(reply) = block.reply {
                    let value = if kind == Marker::End {
                        Ok(ControlReply { lines: block.lines })
                    } else {
                        Err(StreamError::Tmux(body_error(block.lines.as_slice())))
                    };
                    let _result = reply.send(value);
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
            output_tx
                .try_send(bytes)
                .map_err(|_| StreamError::Backpressure)?;
        }
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
        if fields.len() != 17 {
            return Err(StreamError::ControlProtocol(
                "pane state field count changed",
            ));
        }
        validate_tmux_pane_id(fields[0])?;
        let _pane_pid = parse_u32(fields[1], "pane pid")?;
        Ok(Self {
            pane_id: fields[0].to_owned(),
            alternate: flag(fields[2])?,
            cursor_x: parse_u16(fields[4], "cursor x")?,
            cursor_y: parse_u16(fields[5], "cursor y")?,
            cursor_visible: flag(fields[6])?,
            keypad_cursor: flag(fields[7])?,
            wrap: flag(fields[8])?,
            insert: flag(fields[9])?,
            origin: flag(fields[10])?,
            mouse_standard: flag(fields[11])?,
            mouse_button: flag(fields[12])?,
            mouse_all: flag(fields[13])?,
            mouse_sgr: flag(fields[14])?,
            bracket_paste: flag(fields[15])?,
            history_size: parse_u32(fields[16], "history size")?,
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
    }
    extend_bounded(&mut output, b"\x1b[2J\x1b[H")?;
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
    for (index, line) in lines.iter().enumerate() {
        extend_bounded(output, b"\x1b[0m\r\x1b[2K")?;
        extend_bounded(output, line)?;
        extend_bounded(output, b"\x1b[0m")?;
        if final_newline || index + 1 < lines.len() {
            extend_bounded(output, b"\r\n")?;
        }
    }
    Ok(())
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
        CONTROL_MODE_ARGUMENT, MAX_INITIAL_REPLAY_JOINED_CELLS, PANE_STATE_FORMAT, PaneState,
        StreamError, build_replay, decode_tmux_output, extended_output_payload,
        initial_replay_history_lines, output_payload, parse_input_frame, parse_marker,
        refresh_pane_action, validate_geometry,
    };

    #[test]
    fn tmux_control_mode_and_state_format_are_pipe_safe() {
        assert_eq!(CONTROL_MODE_ARGUMENT, "-C");
        assert_eq!(
            PANE_STATE_FORMAT
                .as_bytes()
                .iter()
                .filter(|byte| **byte == b'\t')
                .count(),
            16
        );
        assert!(
            !PANE_STATE_FORMAT
                .as_bytes()
                .windows(2)
                .any(|window| window == b"\\t")
        );

        let state = PaneState::parse(b"%7\t123\t1\t0\t4\t5\t1\t0\t1\t0\t0\t1\t0\t0\t1\t1\t42")
            .unwrap_or_else(|error| panic!("pane state: {error}"));
        assert_eq!(state.pane_id, "%7");
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
        assert_eq!(MAX_INITIAL_REPLAY_JOINED_CELLS, 32 * 1024);
    }

    #[test]
    fn initial_history_replay_is_bounded_by_depth_and_geometry() {
        assert_eq!(initial_replay_history_lines(u32::MAX, 2), 512);
        assert_eq!(initial_replay_history_lines(u32::MAX, 80), 409);
        assert_eq!(initial_replay_history_lines(u32::MAX, 100), 327);
        assert_eq!(initial_replay_history_lines(u32::MAX, 1_000), 32);
        assert_eq!(initial_replay_history_lines(7, 1_000), 7);
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
        let (sequence, bytes) =
            parse_input_frame(frame.as_slice()).unwrap_or_else(|error| panic!("frame: {error}"));
        assert_eq!(sequence, 7);
        assert_eq!(bytes, b"abc");
        assert!(matches!(
            parse_input_frame(&[1; 9]),
            Err(StreamError::ClientProtocol(_))
        ));
    }

    #[test]
    fn replay_restores_ansi_modes_and_cursor() {
        let state = PaneState {
            pane_id: "%1".to_owned(),
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
}
