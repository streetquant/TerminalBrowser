use getrandom::fill;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
use termdeck_web_core::{
    AcquireMode, AuthenticatedDevice, ControlError, ControllerToken, DaemonGeneration, DeviceId,
    LeaseId, OwnerId, PaneControllerRegistry, PaneId,
};
use tokio::io::AsyncWriteExt;
use tokio::sync::{Mutex, Semaphore};
use tokio::time::timeout;

pub const DEFAULT_CONTROLLER_TTL_MS: u64 = 30_000;
pub const MAX_TERMINAL_INPUT_BYTES: usize = 64 * 1024;
pub const MAX_TERMINAL_CAPTURE_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_TERMINAL_OPERATIONS: usize = 16;
pub const MAX_CAPTURE_HISTORY_LINES: u32 = 4_000;

#[derive(Clone)]
pub struct TerminalService {
    owner_id: OwnerId,
    state: Arc<Mutex<ControlState>>,
    tmux_executable: Arc<PathBuf>,
    command_timeout: Duration,
    operation_slots: Arc<Semaphore>,
}

struct ControlState {
    registry: PaneControllerRegistry,
    slots: BTreeMap<PaneId, ControllerSlot>,
}

#[derive(Clone)]
struct ControllerSlot {
    token: ControllerToken,
    next_sequence: u64,
    indeterminate: bool,
    in_flight: Option<Arc<AtomicBool>>,
    last_receipt: Option<InputReceipt>,
}

impl ControllerSlot {
    fn input_is_indeterminate(&self) -> bool {
        self.indeterminate
            || self
                .in_flight
                .as_ref()
                .is_some_and(|active| !active.load(Ordering::Acquire))
    }
}

#[derive(Clone, Copy)]
struct InputReceipt {
    sequence: u64,
    digest: [u8; 32],
}

pub(crate) struct InputPermit {
    pane_id: PaneId,
    device_id: DeviceId,
    token: ControllerToken,
    sequence: u64,
    digest: [u8; 32],
    in_flight: Arc<AtomicBool>,
}

impl Drop for InputPermit {
    fn drop(&mut self) {
        // An aborted request may have already delivered input. Keep its
        // reservation in the slot, with an indeterminate outcome, until a
        // successful completion removes it or the controller gets a new lease.
        self.in_flight.store(false, Ordering::Release);
    }
}

pub(crate) enum InputDecision {
    Execute(InputPermit),
    Duplicate(TerminalControlView),
}

#[derive(Clone, Copy)]
pub(crate) enum InputOutcome {
    Committed,
    Indeterminate,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct TerminalControlView {
    pub schema: &'static str,
    pub pane_id: String,
    pub state: &'static str,
    pub controller_device_id: Option<String>,
    pub mine: bool,
    pub epoch: Option<u64>,
    pub expires_at_unix_ms: Option<u64>,
    pub next_sequence: Option<u64>,
    pub indeterminate: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct TerminalCapture {
    pub schema: &'static str,
    pub pane_id: String,
    pub text: String,
    pub truncated: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TerminalKey {
    Enter,
    CtrlC,
    CtrlD,
    CtrlL,
    Tab,
    Escape,
    Up,
    Down,
    Left,
    Right,
    Home,
    End,
    PageUp,
    PageDown,
    Backspace,
    Delete,
}

impl TerminalKey {
    pub fn parse(value: &str) -> Result<Self, TerminalError> {
        match value {
            "enter" => Ok(Self::Enter),
            "ctrl-c" => Ok(Self::CtrlC),
            "ctrl-d" => Ok(Self::CtrlD),
            "ctrl-l" => Ok(Self::CtrlL),
            "tab" => Ok(Self::Tab),
            "escape" => Ok(Self::Escape),
            "up" => Ok(Self::Up),
            "down" => Ok(Self::Down),
            "left" => Ok(Self::Left),
            "right" => Ok(Self::Right),
            "home" => Ok(Self::Home),
            "end" => Ok(Self::End),
            "page-up" => Ok(Self::PageUp),
            "page-down" => Ok(Self::PageDown),
            "backspace" => Ok(Self::Backspace),
            "delete" => Ok(Self::Delete),
            _ => Err(TerminalError::InvalidKey),
        }
    }

    fn tmux_name(self) -> &'static str {
        match self {
            Self::Enter => "Enter",
            Self::CtrlC => "C-c",
            Self::CtrlD => "C-d",
            Self::CtrlL => "C-l",
            Self::Tab => "Tab",
            Self::Escape => "Escape",
            Self::Up => "Up",
            Self::Down => "Down",
            Self::Left => "Left",
            Self::Right => "Right",
            Self::Home => "Home",
            Self::End => "End",
            Self::PageUp => "PageUp",
            Self::PageDown => "PageDown",
            Self::Backspace => "BSpace",
            Self::Delete => "DC",
        }
    }
}

#[derive(Debug)]
pub enum TerminalError {
    Busy,
    Control(ControlError),
    Id(termdeck_web_core::IdError),
    InvalidKey,
    InvalidSequence { expected: u64, actual: u64 },
    InputTooLarge { actual: usize, maximum: usize },
    NotController,
    Indeterminate,
    OutputTooLarge { actual: usize, maximum: usize },
    TmuxFailed(String),
    TmuxTimeout,
    Utf8(std::string::FromUtf8Error),
    Io(std::io::Error),
    SequenceOverflow,
}

impl fmt::Display for TerminalError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Busy => formatter.write_str("terminal operation capacity reached"),
            Self::Control(error) => error.fmt(formatter),
            Self::Id(error) => error.fmt(formatter),
            Self::InvalidKey => formatter.write_str("terminal key is not allowed"),
            Self::InvalidSequence { expected, actual } => {
                write!(
                    formatter,
                    "terminal input sequence {actual} does not match {expected}"
                )
            }
            Self::InputTooLarge { actual, maximum } => {
                write!(
                    formatter,
                    "terminal input is {actual} bytes; maximum is {maximum}"
                )
            }
            Self::NotController => formatter.write_str("device does not control this pane"),
            Self::Indeterminate => {
                formatter.write_str("prior terminal input outcome is indeterminate")
            }
            Self::OutputTooLarge { actual, maximum } => {
                write!(
                    formatter,
                    "terminal output is {actual} bytes; maximum is {maximum}"
                )
            }
            Self::TmuxFailed(detail) => write!(formatter, "tmux command failed: {detail}"),
            Self::TmuxTimeout => formatter.write_str("tmux command timed out"),
            Self::Utf8(error) => write!(formatter, "terminal output is not UTF-8: {error}"),
            Self::Io(error) => write!(formatter, "terminal I/O failed: {error}"),
            Self::SequenceOverflow => formatter.write_str("terminal input sequence exhausted"),
        }
    }
}

impl Error for TerminalError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Control(error) => Some(error),
            Self::Id(error) => Some(error),
            Self::Utf8(error) => Some(error),
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<ControlError> for TerminalError {
    fn from(value: ControlError) -> Self {
        Self::Control(value)
    }
}

impl From<termdeck_web_core::IdError> for TerminalError {
    fn from(value: termdeck_web_core::IdError) -> Self {
        Self::Id(value)
    }
}

impl From<std::io::Error> for TerminalError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value)
    }
}

impl From<std::string::FromUtf8Error> for TerminalError {
    fn from(value: std::string::FromUtf8Error) -> Self {
        Self::Utf8(value)
    }
}

impl TerminalService {
    pub fn new(
        owner_id: OwnerId,
        tmux_executable: PathBuf,
        command_timeout: Duration,
    ) -> Result<Self, TerminalError> {
        let mut generation_bytes = [0_u8; 16];
        fill(&mut generation_bytes).map_err(|error| {
            TerminalError::Io(std::io::Error::other(format!(
                "random generation failed: {error}"
            )))
        })?;
        let mut generation = u128::from_be_bytes(generation_bytes);
        if generation == 0 {
            generation = 1;
        }
        let registry = PaneControllerRegistry::new(
            owner_id.clone(),
            DaemonGeneration::new(generation)?,
            DEFAULT_CONTROLLER_TTL_MS,
            termdeck_web_core::MAX_CONTROLLED_PANES,
        )?;
        Ok(Self {
            owner_id,
            state: Arc::new(Mutex::new(ControlState {
                registry,
                slots: BTreeMap::new(),
            })),
            tmux_executable: Arc::new(tmux_executable),
            command_timeout,
            operation_slots: Arc::new(Semaphore::new(MAX_TERMINAL_OPERATIONS)),
        })
    }

    pub async fn status(
        &self,
        pane_id: &str,
        device_id: &str,
        now_unix_ms: u64,
    ) -> Result<TerminalControlView, TerminalError> {
        let pane_id = PaneId::new(pane_id)?;
        let device_id = DeviceId::new(device_id)?;
        let mut state = self.state.lock().await;
        Ok(view_locked(&mut state, &pane_id, &device_id, now_unix_ms))
    }

    pub async fn acquire(
        &self,
        pane_id: &str,
        device_id: &str,
        now_unix_ms: u64,
        takeover: bool,
    ) -> Result<TerminalControlView, TerminalError> {
        self.acquire_control(pane_id, device_id, now_unix_ms, takeover, false)
            .await
    }

    pub(crate) async fn focus(
        &self,
        pane_id: &str,
        device_id: &str,
        now_unix_ms: u64,
    ) -> Result<TerminalControlView, TerminalError> {
        // Revalidate explicit focus atomically. Keeping an already-owned lease
        // preserves pending input and its replay receipt; a different owner
        // requires a new generation before any browser input can be sent.
        self.acquire_control(pane_id, device_id, now_unix_ms, true, true)
            .await
    }

    async fn acquire_control(
        &self,
        pane_id: &str,
        device_id: &str,
        now_unix_ms: u64,
        takeover: bool,
        retain_owned: bool,
    ) -> Result<TerminalControlView, TerminalError> {
        let pane_id = PaneId::new(pane_id)?;
        let device_id = DeviceId::new(device_id)?;
        let device = AuthenticatedDevice::new(self.owner_id.clone(), device_id.clone());
        let mut state = self.state.lock().await;
        if let Some(current) = state.registry.current(&pane_id, now_unix_ms)
            && current.device_id() == &device_id
            && (!takeover || retain_owned)
        {
            return Ok(view_locked(&mut state, &pane_id, &device_id, now_unix_ms));
        }
        let token = state.registry.acquire(
            &device,
            pane_id.clone(),
            LeaseId::new(random_lease_id()?)?,
            now_unix_ms,
            DEFAULT_CONTROLLER_TTL_MS,
            if takeover {
                AcquireMode::Takeover
            } else {
                AcquireMode::IfVacant
            },
        )?;
        state.slots.insert(
            pane_id.clone(),
            ControllerSlot {
                token,
                next_sequence: 1,
                indeterminate: false,
                in_flight: None,
                last_receipt: None,
            },
        );
        Ok(view_locked(&mut state, &pane_id, &device_id, now_unix_ms))
    }

    pub async fn heartbeat(
        &self,
        pane_id: &str,
        device_id: &str,
        now_unix_ms: u64,
    ) -> Result<TerminalControlView, TerminalError> {
        let pane_id = PaneId::new(pane_id)?;
        let device_id = DeviceId::new(device_id)?;
        let mut state = self.state.lock().await;
        let token = owned_token(&mut state, &pane_id, &device_id, now_unix_ms)?;
        let refreshed = state
            .registry
            .heartbeat(&token, now_unix_ms, DEFAULT_CONTROLLER_TTL_MS)?;
        let slot = state
            .slots
            .get_mut(&pane_id)
            .ok_or(TerminalError::NotController)?;
        slot.token = refreshed;
        Ok(view_locked(&mut state, &pane_id, &device_id, now_unix_ms))
    }

    pub async fn release(
        &self,
        pane_id: &str,
        device_id: &str,
        now_unix_ms: u64,
    ) -> Result<TerminalControlView, TerminalError> {
        let pane_id = PaneId::new(pane_id)?;
        let device_id = DeviceId::new(device_id)?;
        let mut state = self.state.lock().await;
        let token = owned_token(&mut state, &pane_id, &device_id, now_unix_ms)?;
        state.registry.release(&token, now_unix_ms)?;
        state.slots.remove(&pane_id);
        Ok(view_locked(&mut state, &pane_id, &device_id, now_unix_ms))
    }

    pub async fn capture(
        &self,
        pane_id: &str,
        tmux_target: &str,
    ) -> Result<TerminalCapture, TerminalError> {
        let pane_id = PaneId::new(pane_id)?;
        let _permit = Arc::clone(&self.operation_slots)
            .try_acquire_owned()
            .map_err(|_| TerminalError::Busy)?;
        let mut command = crate::tmux_runtime::command(self.tmux_executable.as_ref()).await?;
        command
            .arg("capture-pane")
            .arg("-p")
            .arg("-J")
            .arg("-S")
            .arg(format!("-{MAX_CAPTURE_HISTORY_LINES}"))
            .arg("-t")
            .arg(tmux_target)
            .kill_on_drop(true);
        let output = timeout(self.command_timeout, command.output())
            .await
            .map_err(|_| TerminalError::TmuxTimeout)??;
        if !output.status.success() {
            return Err(TerminalError::TmuxFailed(bounded_stderr(&output.stderr)));
        }
        if output.stdout.len() > MAX_TERMINAL_CAPTURE_BYTES {
            return Err(TerminalError::OutputTooLarge {
                actual: output.stdout.len(),
                maximum: MAX_TERMINAL_CAPTURE_BYTES,
            });
        }
        Ok(TerminalCapture {
            schema: "termdeck.terminal-capture.v1",
            pane_id: pane_id.as_str().to_owned(),
            text: String::from_utf8(output.stdout)?,
            truncated: false,
        })
    }

    pub async fn send_text(
        &self,
        pane_id: &str,
        device_id: &str,
        tmux_target: &str,
        sequence: u64,
        text: &str,
        now_unix_ms: u64,
    ) -> Result<TerminalControlView, TerminalError> {
        if text.is_empty() || text.len() > MAX_TERMINAL_INPUT_BYTES {
            return Err(TerminalError::InputTooLarge {
                actual: text.len(),
                maximum: MAX_TERMINAL_INPUT_BYTES,
            });
        }
        let _operation_permit = Arc::clone(&self.operation_slots)
            .try_acquire_owned()
            .map_err(|_| TerminalError::Busy)?;
        let decision = self
            .authorize_input(
                pane_id,
                device_id,
                sequence,
                b"text",
                text.as_bytes(),
                now_unix_ms,
            )
            .await?;
        let permit = match decision {
            InputDecision::Duplicate(view) => return Ok(view),
            InputDecision::Execute(value) => value,
        };
        let buffer_name = format!(
            "termdeck-web-{}-{sequence}",
            permit.token.lease_id().as_str()
        );
        match self
            .load_and_paste(tmux_target, buffer_name.as_str(), text.as_bytes())
            .await
        {
            Ok(()) => {
                self.complete_input(permit, InputOutcome::Committed, now_unix_ms)
                    .await
            }
            Err(error) => {
                let _result = self
                    .complete_input(permit, InputOutcome::Indeterminate, now_unix_ms)
                    .await;
                Err(error)
            }
        }
    }

    pub async fn send_key(
        &self,
        pane_id: &str,
        device_id: &str,
        tmux_target: &str,
        sequence: u64,
        key: TerminalKey,
        now_unix_ms: u64,
    ) -> Result<TerminalControlView, TerminalError> {
        let _operation_permit = Arc::clone(&self.operation_slots)
            .try_acquire_owned()
            .map_err(|_| TerminalError::Busy)?;
        let decision = self
            .authorize_input(
                pane_id,
                device_id,
                sequence,
                b"key",
                key.tmux_name().as_bytes(),
                now_unix_ms,
            )
            .await?;
        let permit = match decision {
            InputDecision::Duplicate(view) => return Ok(view),
            InputDecision::Execute(value) => value,
        };
        let result = async {
            let mut command = crate::tmux_runtime::command(self.tmux_executable.as_ref()).await?;
            command
                .arg("send-keys")
                .arg("-t")
                .arg(tmux_target)
                .arg(key.tmux_name())
                .kill_on_drop(true);
            let output = timeout(self.command_timeout, command.output())
                .await
                .map_err(|_| TerminalError::TmuxTimeout)??;
            if output.status.success() {
                Ok(())
            } else {
                Err(TerminalError::TmuxFailed(bounded_stderr(&output.stderr)))
            }
        }
        .await;
        match result {
            Ok(()) => {
                self.complete_input(permit, InputOutcome::Committed, now_unix_ms)
                    .await
            }
            Err(error) => {
                let _result = self
                    .complete_input(permit, InputOutcome::Indeterminate, now_unix_ms)
                    .await;
                Err(error)
            }
        }
    }

    #[must_use]
    pub(crate) fn tmux_executable(&self) -> Arc<PathBuf> {
        Arc::clone(&self.tmux_executable)
    }

    #[must_use]
    pub(crate) fn command_timeout(&self) -> Duration {
        self.command_timeout
    }

    pub(crate) async fn authorize_input(
        &self,
        pane_id: &str,
        device_id: &str,
        sequence: u64,
        domain: &[u8],
        bytes: &[u8],
        now_unix_ms: u64,
    ) -> Result<InputDecision, TerminalError> {
        if bytes.is_empty() || bytes.len() > MAX_TERMINAL_INPUT_BYTES {
            return Err(TerminalError::InputTooLarge {
                actual: bytes.len(),
                maximum: MAX_TERMINAL_INPUT_BYTES,
            });
        }
        let pane_id = PaneId::new(pane_id)?;
        let device_id = DeviceId::new(device_id)?;
        let digest = input_digest(domain, bytes);
        let mut state = self.state.lock().await;
        let token = owned_token(&mut state, &pane_id, &device_id, now_unix_ms)?;
        let slot = state
            .slots
            .get(&pane_id)
            .ok_or(TerminalError::NotController)?;
        if slot.input_is_indeterminate() {
            return Err(TerminalError::Indeterminate);
        }
        if slot.in_flight.is_some() {
            return Err(TerminalError::Busy);
        }
        if sequence == slot.next_sequence {
            state.registry.authorize_input(&token, now_unix_ms)?;
            let in_flight = Arc::new(AtomicBool::new(true));
            state
                .slots
                .get_mut(&pane_id)
                .ok_or(TerminalError::NotController)?
                .in_flight = Some(Arc::clone(&in_flight));
            return Ok(InputDecision::Execute(InputPermit {
                pane_id,
                device_id,
                token,
                sequence,
                digest,
                in_flight,
            }));
        }
        if slot.last_receipt.is_some_and(|receipt| {
            receipt.sequence == sequence
                && receipt.digest == digest
                && slot.next_sequence == sequence.saturating_add(1)
        }) {
            return Ok(InputDecision::Duplicate(view_locked(
                &mut state,
                &pane_id,
                &device_id,
                now_unix_ms,
            )));
        }
        Err(TerminalError::InvalidSequence {
            expected: slot.next_sequence,
            actual: sequence,
        })
    }

    pub(crate) async fn complete_input(
        &self,
        permit: InputPermit,
        outcome: InputOutcome,
        now_unix_ms: u64,
    ) -> Result<TerminalControlView, TerminalError> {
        let mut state = self.state.lock().await;
        let token = owned_token(&mut state, &permit.pane_id, &permit.device_id, now_unix_ms)?;
        // A heartbeat changes the expiry but not the lease that reserved this
        // input. A takeover or release must never complete a newer reservation.
        if token.generation() != permit.token.generation()
            || token.epoch() != permit.token.epoch()
            || token.lease_id() != permit.token.lease_id()
        {
            return Err(TerminalError::NotController);
        }
        state.registry.authorize_input(&token, now_unix_ms)?;
        let slot = state
            .slots
            .get_mut(&permit.pane_id)
            .ok_or(TerminalError::NotController)?;
        if slot.next_sequence != permit.sequence
            || slot.indeterminate
            || !slot
                .in_flight
                .as_ref()
                .is_some_and(|active| Arc::ptr_eq(active, &permit.in_flight))
        {
            return Err(TerminalError::NotController);
        }
        match outcome {
            InputOutcome::Committed => {
                let next_sequence = slot
                    .next_sequence
                    .checked_add(1)
                    .ok_or(TerminalError::SequenceOverflow)?;
                slot.last_receipt = Some(InputReceipt {
                    sequence: permit.sequence,
                    digest: permit.digest,
                });
                slot.next_sequence = next_sequence;
            }
            InputOutcome::Indeterminate => slot.indeterminate = true,
        }
        slot.in_flight = None;
        Ok(view_locked(
            &mut state,
            &permit.pane_id,
            &permit.device_id,
            now_unix_ms,
        ))
    }

    async fn load_and_paste(
        &self,
        tmux_target: &str,
        buffer_name: &str,
        bytes: &[u8],
    ) -> Result<(), TerminalError> {
        let mut load = crate::tmux_runtime::command(self.tmux_executable.as_ref()).await?;
        load.arg("load-buffer")
            .arg("-b")
            .arg(buffer_name)
            .arg("-")
            .stdin(Stdio::piped())
            .kill_on_drop(true);
        let mut child = load.spawn()?;
        let mut stdin = child.stdin.take().ok_or_else(|| {
            TerminalError::Io(std::io::Error::other("tmux stdin was unavailable"))
        })?;
        timeout(self.command_timeout, async {
            stdin.write_all(bytes).await?;
            stdin.shutdown().await
        })
        .await
        .map_err(|_| TerminalError::TmuxTimeout)??;
        // tmux load-buffer reads until EOF. AsyncWrite::shutdown does not close
        // this pipe; retaining the taken handle makes both processes wait.
        drop(stdin);
        let output = timeout(self.command_timeout, child.wait_with_output())
            .await
            .map_err(|_| TerminalError::TmuxTimeout)??;
        if !output.status.success() {
            return Err(TerminalError::TmuxFailed(bounded_stderr(&output.stderr)));
        }
        let mut paste = crate::tmux_runtime::command(self.tmux_executable.as_ref()).await?;
        paste
            .arg("paste-buffer")
            .arg("-b")
            .arg(buffer_name)
            .arg("-d")
            .arg("-t")
            .arg(tmux_target)
            .kill_on_drop(true);
        let output = timeout(self.command_timeout, paste.output())
            .await
            .map_err(|_| TerminalError::TmuxTimeout)??;
        if output.status.success() {
            Ok(())
        } else {
            self.delete_buffer(buffer_name).await;
            Err(TerminalError::TmuxFailed(bounded_stderr(&output.stderr)))
        }
    }

    async fn delete_buffer(&self, buffer_name: &str) {
        let Ok(mut command) = crate::tmux_runtime::command(self.tmux_executable.as_ref()).await
        else {
            return;
        };
        command
            .arg("delete-buffer")
            .arg("-b")
            .arg(buffer_name)
            .kill_on_drop(true);
        let _result = timeout(self.command_timeout, command.output()).await;
    }
}

fn input_digest(domain: &[u8], bytes: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"termdeck-terminal-input-v1\0");
    hasher.update(
        u64::try_from(domain.len())
            .unwrap_or(u64::MAX)
            .to_be_bytes(),
    );
    hasher.update(domain);
    hasher.update(u64::try_from(bytes.len()).unwrap_or(u64::MAX).to_be_bytes());
    hasher.update(bytes);
    hasher.finalize().into()
}

fn owned_token(
    state: &mut ControlState,
    pane_id: &PaneId,
    device_id: &DeviceId,
    now_unix_ms: u64,
) -> Result<ControllerToken, TerminalError> {
    let Some(current) = state.registry.current(pane_id, now_unix_ms) else {
        state.slots.remove(pane_id);
        return Err(TerminalError::NotController);
    };
    if current.device_id() != device_id {
        return Err(TerminalError::NotController);
    }
    let slot = state
        .slots
        .get(pane_id)
        .ok_or(TerminalError::NotController)?;
    if slot.token != current {
        return Err(TerminalError::NotController);
    }
    Ok(current)
}

fn view_locked(
    state: &mut ControlState,
    pane_id: &PaneId,
    device_id: &DeviceId,
    now_unix_ms: u64,
) -> TerminalControlView {
    let current = state.registry.current(pane_id, now_unix_ms);
    if current.is_none() {
        state.slots.remove(pane_id);
    }
    let slot = state.slots.get(pane_id);
    let mine = current
        .as_ref()
        .is_some_and(|token| token.device_id() == device_id);
    TerminalControlView {
        schema: "termdeck.terminal-control.v1",
        pane_id: pane_id.as_str().to_owned(),
        state: if current.is_some() {
            "controlled"
        } else {
            "viewer"
        },
        controller_device_id: current
            .as_ref()
            .map(|token| token.device_id().as_str().to_owned()),
        mine,
        epoch: current.as_ref().map(ControllerToken::epoch),
        expires_at_unix_ms: current.as_ref().map(ControllerToken::expires_at_unix_ms),
        next_sequence: if mine {
            slot.map(|value| value.next_sequence)
        } else {
            None
        },
        indeterminate: mine && slot.is_some_and(ControllerSlot::input_is_indeterminate),
    }
}

fn random_lease_id() -> Result<String, TerminalError> {
    let mut bytes = [0_u8; 24];
    fill(&mut bytes).map_err(|error| {
        TerminalError::Io(std::io::Error::other(format!(
            "random lease failed: {error}"
        )))
    })?;
    let mut output = String::with_capacity(6 + bytes.len() * 2);
    output.push_str("lease-");
    for byte in bytes {
        use std::fmt::Write as _;
        write!(&mut output, "{byte:02x}")
            .map_err(|_| TerminalError::Io(std::io::Error::other("lease formatting failed")))?;
    }
    Ok(output)
}

fn bounded_stderr(value: &[u8]) -> String {
    let bounded = &value[..value.len().min(2_048)];
    String::from_utf8_lossy(bounded).trim().to_owned()
}

#[cfg(test)]
mod tests {
    use super::{InputDecision, InputOutcome, TerminalError, TerminalKey, TerminalService};
    use termdeck_web_core::OwnerId;

    fn service() -> TerminalService {
        TerminalService::new(
            OwnerId::new("owner-test").unwrap_or_else(|error| panic!("owner: {error}")),
            std::path::PathBuf::from("/bin/false"),
            std::time::Duration::from_millis(100),
        )
        .unwrap_or_else(|error| panic!("service: {error}"))
    }

    #[cfg(unix)]
    struct CommandFixture {
        directory: std::path::PathBuf,
        executable: std::path::PathBuf,
    }

    #[cfg(unix)]
    impl CommandFixture {
        fn new(script: &str) -> Self {
            use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
            let directory = std::env::temp_dir().join(format!(
                "t1000-input-test-{}",
                super::random_lease_id().unwrap_or_else(|error| panic!("random: {error}"))
            ));
            std::fs::DirBuilder::new()
                .mode(0o700)
                .create(&directory)
                .unwrap_or_else(|error| panic!("directory: {error}"));
            let executable = directory.join("tmux-fixture");
            std::fs::write(&executable, script).unwrap_or_else(|error| panic!("fixture: {error}"));
            std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o700))
                .unwrap_or_else(|error| panic!("permissions: {error}"));
            Self {
                directory,
                executable,
            }
        }

        fn service(&self, command_timeout: std::time::Duration) -> TerminalService {
            TerminalService::new(
                OwnerId::new("owner-test").unwrap_or_else(|error| panic!("owner: {error}")),
                self.executable.clone(),
                command_timeout,
            )
            .unwrap_or_else(|error| panic!("service: {error}"))
        }

        fn deliveries(&self) -> Vec<u8> {
            std::fs::read(self.directory.join("tmux-fixture.deliveries"))
                .unwrap_or_else(|error| panic!("deliveries: {error}"))
        }

        async fn wait_started(&self) {
            let started = self.directory.join("tmux-fixture.started");
            tokio::time::timeout(std::time::Duration::from_secs(5), async {
                while !started.exists() {
                    tokio::time::sleep(std::time::Duration::from_millis(5)).await;
                }
            })
            .await
            .unwrap_or_else(|error| panic!("fixture did not start: {error}"));
        }
    }

    #[cfg(unix)]
    impl Drop for CommandFixture {
        fn drop(&mut self) {
            let _result = std::fs::remove_dir_all(&self.directory);
        }
    }

    #[cfg(unix)]
    #[tokio::test(flavor = "current_thread")]
    async fn overlapping_key_requests_deliver_once_and_acknowledge_completed_retry() {
        let fixture = CommandFixture::new(
            "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$4\" >> \"$0.deliveries\"\n: > \"$0.started\"\nwhile [ ! -e \"$0.release\" ]; do sleep 0.01; done\n",
        );
        let service = fixture.service(std::time::Duration::from_secs(10));
        service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        let first_service = service.clone();
        let first = tokio::spawn(async move {
            first_service
                .send_key("pane-1", "device-1", "fixture", 1, TerminalKey::Enter, 11)
                .await
        });
        fixture.wait_started().await;
        for (sequence, key) in [
            (1, TerminalKey::Enter),
            (1, TerminalKey::CtrlC),
            (2, TerminalKey::Enter),
        ] {
            assert!(matches!(
                service
                    .send_key("pane-1", "device-1", "fixture", sequence, key, 12)
                    .await,
                Err(TerminalError::Busy)
            ));
        }
        assert_eq!(fixture.deliveries(), b"Enter\n");
        std::fs::write(fixture.directory.join("tmux-fixture.release"), b"")
            .unwrap_or_else(|error| panic!("release fixture: {error}"));
        let committed = first
            .await
            .unwrap_or_else(|error| panic!("join first input: {error}"))
            .unwrap_or_else(|error| panic!("first input: {error}"));
        assert_eq!(committed.next_sequence, Some(2));
        assert!(!committed.indeterminate);
        let duplicate = service
            .send_key("pane-1", "device-1", "fixture", 1, TerminalKey::Enter, 13)
            .await
            .unwrap_or_else(|error| panic!("duplicate: {error}"));
        assert_eq!(duplicate.next_sequence, Some(2));
        assert_eq!(fixture.deliveries(), b"Enter\n");
    }

    #[cfg(unix)]
    #[tokio::test(flavor = "current_thread")]
    async fn overlapping_text_requests_deliver_once_and_preserve_the_buffer() {
        let fixture = CommandFixture::new(
            "#!/bin/sh\nset -eu\ncase \"$1\" in\nload-buffer) cat > \"$0.buffer\";;\npaste-buffer) cat \"$0.buffer\" >> \"$0.deliveries\"; : > \"$0.started\"; while [ ! -e \"$0.release\" ]; do sleep 0.01; done;;\n*) exit 1;;\nesac\n",
        );
        let service = fixture.service(std::time::Duration::from_secs(10));
        service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        let first_service = service.clone();
        let first = tokio::spawn(async move {
            first_service
                .send_text("pane-1", "device-1", "fixture", 1, "original", 11)
                .await
        });
        fixture.wait_started().await;
        for text in ["original", "changed"] {
            assert!(matches!(
                service
                    .send_text("pane-1", "device-1", "fixture", 1, text, 12)
                    .await,
                Err(TerminalError::Busy)
            ));
        }
        assert_eq!(fixture.deliveries(), b"original");
        assert_eq!(
            std::fs::read(fixture.directory.join("tmux-fixture.buffer"))
                .unwrap_or_else(|error| panic!("buffer: {error}")),
            b"original"
        );
        std::fs::write(fixture.directory.join("tmux-fixture.release"), b"")
            .unwrap_or_else(|error| panic!("release fixture: {error}"));
        let committed = first
            .await
            .unwrap_or_else(|error| panic!("join first input: {error}"))
            .unwrap_or_else(|error| panic!("first input: {error}"));
        assert_eq!(committed.next_sequence, Some(2));
        let duplicate = service
            .send_text("pane-1", "device-1", "fixture", 1, "original", 13)
            .await
            .unwrap_or_else(|error| panic!("duplicate: {error}"));
        assert_eq!(duplicate.next_sequence, Some(2));
        assert_eq!(fixture.deliveries(), b"original");
    }

    #[cfg(unix)]
    #[tokio::test(flavor = "current_thread")]
    async fn cancelled_key_request_keeps_an_indeterminate_outcome() {
        let fixture = CommandFixture::new(
            "#!/bin/sh\nset -eu\nprintf '%s\\n' \"$4\" >> \"$0.deliveries\"\n: > \"$0.started\"\nexec sleep 30\n",
        );
        let service = fixture.service(std::time::Duration::from_secs(10));
        service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        let first_service = service.clone();
        let first = tokio::spawn(async move {
            first_service
                .send_key("pane-1", "device-1", "fixture", 1, TerminalKey::Enter, 11)
                .await
        });
        fixture.wait_started().await;
        first.abort();
        assert!(first.await.is_err_and(|error| error.is_cancelled()));
        let view = service
            .status("pane-1", "device-1", 12)
            .await
            .unwrap_or_else(|error| panic!("status: {error}"));
        assert!(view.indeterminate);
        assert_eq!(view.next_sequence, Some(1));
        assert!(matches!(
            service
                .send_key("pane-1", "device-1", "fixture", 1, TerminalKey::Enter, 13)
                .await,
            Err(TerminalError::Indeterminate)
        ));
        assert_eq!(fixture.deliveries(), b"Enter\n");
    }

    #[cfg(unix)]
    #[tokio::test(flavor = "current_thread")]
    async fn key_spawn_timeout_and_exit_failures_keep_indeterminate_outcomes() {
        for failure in ["spawn", "timeout", "exit"] {
            let fixture = CommandFixture::new(if failure == "timeout" {
                "#!/bin/sh\nexec sleep 30\n"
            } else {
                "#!/bin/sh\nexit 1\n"
            });
            if failure == "spawn" {
                std::fs::remove_file(&fixture.executable)
                    .unwrap_or_else(|error| panic!("remove executable: {error}"));
            }
            let service = fixture.service(if failure == "timeout" {
                std::time::Duration::from_millis(100)
            } else {
                std::time::Duration::from_secs(2)
            });
            service
                .acquire("pane-1", "device-1", 10, false)
                .await
                .unwrap_or_else(|error| panic!("acquire: {error}"));
            let result = service
                .send_key("pane-1", "device-1", "fixture", 1, TerminalKey::Enter, 11)
                .await;
            assert!(match failure {
                "spawn" => matches!(result, Err(TerminalError::Io(_))),
                "timeout" => matches!(result, Err(TerminalError::TmuxTimeout)),
                _ => matches!(result, Err(TerminalError::TmuxFailed(_))),
            });
            let view = service
                .status("pane-1", "device-1", 12)
                .await
                .unwrap_or_else(|error| panic!("status: {error}"));
            assert!(view.indeterminate, "{failure} lost its outcome");
            assert!(matches!(
                service
                    .send_key("pane-1", "device-1", "fixture", 1, TerminalKey::Enter, 13)
                    .await,
                Err(TerminalError::Indeterminate)
            ));
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn heartbeat_preserves_the_reserved_input_completion() {
        let service = service();
        service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        let decision = service
            .authorize_input("pane-1", "device-1", 1, b"raw", b"abc", 11)
            .await
            .unwrap_or_else(|error| panic!("authorize: {error}"));
        let InputDecision::Execute(permit) = decision else {
            panic!("input was not executable");
        };
        service
            .heartbeat("pane-1", "device-1", 12)
            .await
            .unwrap_or_else(|error| panic!("heartbeat: {error}"));
        let committed = service
            .complete_input(permit, InputOutcome::Committed, 13)
            .await
            .unwrap_or_else(|error| panic!("complete: {error}"));
        assert_eq!(committed.next_sequence, Some(2));
        assert!(!committed.indeterminate);
        assert!(matches!(
            service
                .authorize_input("pane-1", "device-1", 1, b"raw", b"abc", 14)
                .await,
            Ok(InputDecision::Duplicate(_))
        ));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn explicit_focus_preserves_owned_pending_input_and_receipt() {
        let service = service();
        let acquired = service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        let decision = service
            .authorize_input("pane-1", "device-1", 1, b"raw", b"abc", 11)
            .await
            .unwrap_or_else(|error| panic!("authorize: {error}"));
        let InputDecision::Execute(permit) = decision else {
            panic!("not executable");
        };
        let focused = service
            .focus("pane-1", "device-1", 12)
            .await
            .unwrap_or_else(|error| panic!("focus: {error}"));
        assert_eq!(focused.epoch, acquired.epoch);
        assert!(matches!(
            service
                .authorize_input("pane-1", "device-1", 1, b"raw", b"abc", 13)
                .await,
            Err(TerminalError::Busy)
        ));
        let committed = service
            .complete_input(permit, InputOutcome::Committed, 14)
            .await
            .unwrap_or_else(|error| panic!("complete: {error}"));
        let focused = service
            .focus("pane-1", "device-1", 15)
            .await
            .unwrap_or_else(|error| panic!("focus: {error}"));
        assert_eq!(focused.epoch, committed.epoch);
        assert_eq!(focused.next_sequence, Some(2));
        assert!(matches!(
            service
                .authorize_input("pane-1", "device-1", 1, b"raw", b"abc", 16)
                .await,
            Ok(InputDecision::Duplicate(_))
        ));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn explicit_focus_reclaims_changed_owner_with_a_fresh_sequence() {
        let service = service();
        let acquired = service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        service
            .acquire("pane-1", "manager", 11, true)
            .await
            .unwrap_or_else(|error| panic!("manager: {error}"));
        let focused = service
            .focus("pane-1", "device-1", 12)
            .await
            .unwrap_or_else(|error| panic!("focus: {error}"));
        assert!(focused.mine);
        assert_ne!(focused.epoch, acquired.epoch);
        assert_eq!(focused.next_sequence, Some(1));
        assert!(
            !service
                .status("pane-1", "manager", 13)
                .await
                .unwrap_or_else(|error| panic!("status: {error}"))
                .mine
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn stale_completion_cannot_clear_a_new_lease_reservation() {
        let service = service();
        service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        let decision = service
            .authorize_input("pane-1", "device-1", 1, b"raw", b"old", 11)
            .await
            .unwrap_or_else(|error| panic!("authorize old: {error}"));
        let InputDecision::Execute(old) = decision else {
            panic!("old input was not executable");
        };
        service
            .acquire("pane-1", "device-1", 12, true)
            .await
            .unwrap_or_else(|error| panic!("takeover: {error}"));
        let decision = service
            .authorize_input("pane-1", "device-1", 1, b"raw", b"new", 13)
            .await
            .unwrap_or_else(|error| panic!("authorize new: {error}"));
        let InputDecision::Execute(new) = decision else {
            panic!("new input was not executable");
        };
        assert!(matches!(
            service
                .complete_input(old, InputOutcome::Committed, 14)
                .await,
            Err(TerminalError::NotController)
        ));
        assert!(matches!(
            service
                .authorize_input("pane-1", "device-1", 1, b"raw", b"new", 15)
                .await,
            Err(TerminalError::Busy)
        ));
        let committed = service
            .complete_input(new, InputOutcome::Committed, 16)
            .await
            .unwrap_or_else(|error| panic!("complete new: {error}"));
        assert_eq!(committed.next_sequence, Some(2));
        assert!(!committed.indeterminate);
    }

    #[cfg(unix)]
    #[tokio::test(flavor = "current_thread")]
    async fn text_paste_closes_stdin_before_waiting_and_does_not_repeat_input() {
        use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
        struct Directory(std::path::PathBuf);
        impl Drop for Directory {
            fn drop(&mut self) {
                let _result = std::fs::remove_dir_all(&self.0);
            }
        }
        let directory = Directory(std::env::temp_dir().join(format!(
            "t1000-paste-test-{}",
            super::random_lease_id().unwrap_or_else(|error| panic!("random: {error}"))
        )));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&directory.0)
            .unwrap_or_else(|error| panic!("directory: {error}"));
        let executable = directory.0.join("tmux-fixture");
        std::fs::write(
            &executable,
            "#!/bin/sh\nset -eu\ncase \"$1\" in\nload-buffer) cat > \"$0.buffer\";;\npaste-buffer) cat \"$0.buffer\" >> \"$0.pasted\";;\n*) exit 1;;\nesac\n",
        )
        .unwrap_or_else(|error| panic!("fixture: {error}"));
        std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o700))
            .unwrap_or_else(|error| panic!("permissions: {error}"));
        let service = TerminalService::new(
            OwnerId::new("owner-test").unwrap_or_else(|error| panic!("owner: {error}")),
            executable,
            std::time::Duration::from_secs(2),
        )
        .unwrap_or_else(|error| panic!("service: {error}"));
        service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        let text = "x".repeat(super::MAX_TERMINAL_INPUT_BYTES);
        for now in [11, 12] {
            let view = service
                .send_text("pane-1", "device-1", "fixture", 1, &text, now)
                .await
                .unwrap_or_else(|error| panic!("paste: {error}"));
            assert_eq!(view.next_sequence, Some(2));
            assert!(!view.indeterminate);
        }
        assert_eq!(
            std::fs::read(directory.0.join("tmux-fixture.pasted"))
                .unwrap_or_else(|error| panic!("pasted input: {error}")),
            text.as_bytes()
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn one_controller_per_pane_and_takeover_are_enforced() {
        let service = service();
        let first = service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("first acquire: {error}"));
        assert!(first.mine);
        let conflict = service.acquire("pane-1", "device-2", 11, false).await;
        assert!(matches!(conflict, Err(TerminalError::Control(_))));
        let takeover = service
            .acquire("pane-1", "device-2", 12, true)
            .await
            .unwrap_or_else(|error| panic!("takeover: {error}"));
        assert!(takeover.mine);
        assert_eq!(takeover.epoch, Some(2));
        let old = service
            .status("pane-1", "device-1", 13)
            .await
            .unwrap_or_else(|error| panic!("old status: {error}"));
        assert!(!old.mine);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn release_and_expiry_return_to_viewer_state() {
        let service = service();
        service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        let released = service
            .release("pane-1", "device-1", 11)
            .await
            .unwrap_or_else(|error| panic!("release: {error}"));
        assert_eq!(released.state, "viewer");
        service
            .acquire("pane-1", "device-1", 20, false)
            .await
            .unwrap_or_else(|error| panic!("second acquire: {error}"));
        let expired = service
            .status("pane-1", "device-1", 40_021)
            .await
            .unwrap_or_else(|error| panic!("expired status: {error}"));
        assert_eq!(expired.state, "viewer");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn exact_duplicate_input_is_acknowledged_without_reexecution() {
        let service = service();
        service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        let first = service
            .authorize_input("pane-1", "device-1", 1, b"raw", b"abc", 11)
            .await
            .unwrap_or_else(|error| panic!("authorize first: {error}"));
        let InputDecision::Execute(permit) = first else {
            panic!("first input was not executable");
        };
        let committed = service
            .complete_input(permit, InputOutcome::Committed, 12)
            .await
            .unwrap_or_else(|error| panic!("complete first: {error}"));
        assert_eq!(committed.next_sequence, Some(2));

        let duplicate = service
            .authorize_input("pane-1", "device-1", 1, b"raw", b"abc", 13)
            .await
            .unwrap_or_else(|error| panic!("authorize duplicate: {error}"));
        let InputDecision::Duplicate(view) = duplicate else {
            panic!("exact duplicate was not acknowledged");
        };
        assert_eq!(view.next_sequence, Some(2));
        assert!(matches!(
            service
                .authorize_input("pane-1", "device-1", 1, b"raw", b"changed", 14)
                .await,
            Err(TerminalError::InvalidSequence {
                expected: 2,
                actual: 1
            })
        ));
        assert!(matches!(
            service
                .authorize_input("pane-1", "device-1", 3, b"raw", b"gap", 15)
                .await,
            Err(TerminalError::InvalidSequence {
                expected: 2,
                actual: 3
            })
        ));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn indeterminate_input_blocks_followups_until_a_new_lease() {
        let service = service();
        service
            .acquire("pane-1", "device-1", 10, false)
            .await
            .unwrap_or_else(|error| panic!("acquire: {error}"));
        let decision = service
            .authorize_input("pane-1", "device-1", 1, b"raw", b"abc", 11)
            .await
            .unwrap_or_else(|error| panic!("authorize: {error}"));
        let InputDecision::Execute(permit) = decision else {
            panic!("input was not executable");
        };
        let view = service
            .complete_input(permit, InputOutcome::Indeterminate, 12)
            .await
            .unwrap_or_else(|error| panic!("mark indeterminate: {error}"));
        assert!(view.indeterminate);
        assert!(matches!(
            service
                .authorize_input("pane-1", "device-1", 1, b"raw", b"abc", 13)
                .await,
            Err(TerminalError::Indeterminate)
        ));
        service
            .release("pane-1", "device-1", 14)
            .await
            .unwrap_or_else(|error| panic!("release: {error}"));
        let reacquired = service
            .acquire("pane-1", "device-1", 15, false)
            .await
            .unwrap_or_else(|error| panic!("reacquire: {error}"));
        assert_eq!(reacquired.next_sequence, Some(1));
        assert!(!reacquired.indeterminate);
    }

    #[test]
    fn key_parser_is_a_fixed_allowlist() {
        assert!(matches!(
            TerminalKey::parse("ctrl-c"),
            Ok(TerminalKey::CtrlC)
        ));
        assert!(matches!(
            TerminalKey::parse("run-shell"),
            Err(TerminalError::InvalidKey)
        ));
    }
}
