use serde::Serialize;
use serde_json::Value;
use std::error::Error;
use std::ffi::OsString;
use std::fmt;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tokio::process::Command;
use tokio::sync::Semaphore;
use tokio::time::timeout;

pub const MAX_TAB_NAME_BYTES: usize = 256;
pub const MAX_TAB_CWD_BYTES: usize = 8 * 1024;
pub const MAX_TAB_COMMAND_BYTES: usize = 16 * 1024;
pub const MAX_TAB_ACTION_OUTPUT_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_TAB_ACTION_OPERATIONS: usize = 4;

#[derive(Clone)]
pub(crate) struct TabActionService {
    executable: Arc<PathBuf>,
    database_path: Arc<PathBuf>,
    command_timeout: Duration,
    operation_slots: Arc<Semaphore>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ExistingTabAction {
    Pause,
    Resume,
    Pin,
    Unpin,
    Hide,
    Kill,
}

impl ExistingTabAction {
    pub(crate) fn parse(value: &str) -> Result<Self, TabActionError> {
        match value {
            "pause" => Ok(Self::Pause),
            "resume" => Ok(Self::Resume),
            "pin" => Ok(Self::Pin),
            "unpin" => Ok(Self::Unpin),
            "hide" => Ok(Self::Hide),
            "kill" => Ok(Self::Kill),
            _ => Err(TabActionError::InvalidAction),
        }
    }

    fn command(self) -> &'static str {
        match self {
            Self::Pause => "pause",
            Self::Resume => "resume",
            Self::Pin => "pin",
            Self::Unpin => "unpin",
            Self::Hide => "hide",
            Self::Kill => "kill",
        }
    }
}

#[derive(Debug)]
pub enum TabActionError {
    Busy,
    InvalidAction,
    InvalidCommand,
    InvalidConfirmation,
    InvalidCwd,
    InvalidIdentity,
    InvalidName,
    InvalidPath(&'static str),
    Io(std::io::Error),
    Json(serde_json::Error),
    OutputTooLarge { actual: usize, maximum: usize },
    ProcessFailed { status: Option<i32>, detail: String },
    Timeout,
}

impl fmt::Display for TabActionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Busy => formatter.write_str("tab action capacity reached"),
            Self::InvalidAction => formatter.write_str("tab action is not allowed"),
            Self::InvalidCommand => formatter.write_str("tab command is invalid"),
            Self::InvalidConfirmation => {
                formatter.write_str("destructive confirmation did not match the tab identity")
            }
            Self::InvalidCwd => formatter.write_str("tab working directory is invalid"),
            Self::InvalidIdentity => formatter.write_str("tab or window identity is invalid"),
            Self::InvalidName => formatter.write_str("tab name is invalid"),
            Self::InvalidPath(field) => write!(formatter, "{field} path is invalid"),
            Self::Io(error) => write!(formatter, "tab action I/O failed: {error}"),
            Self::Json(error) => write!(formatter, "tab action output was not JSON: {error}"),
            Self::OutputTooLarge { actual, maximum } => write!(
                formatter,
                "tab action output is {actual} bytes; maximum is {maximum}"
            ),
            Self::ProcessFailed { status, detail } => write!(
                formatter,
                "t1000 action failed with status {status:?}: {detail}"
            ),
            Self::Timeout => formatter.write_str("t1000 action timed out"),
        }
    }
}

impl Error for TabActionError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::Json(error) => Some(error),
            _ => None,
        }
    }
}

impl From<std::io::Error> for TabActionError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value)
    }
}

impl From<serde_json::Error> for TabActionError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json(value)
    }
}

#[derive(Clone, Debug, Serialize)]
pub(crate) struct TabActionResult {
    pub schema: &'static str,
    pub action: &'static str,
    pub payload: Value,
}

impl TabActionService {
    pub(crate) fn new(
        executable: PathBuf,
        database_path: PathBuf,
        command_timeout: Duration,
    ) -> Result<Self, TabActionError> {
        validate_absolute_path(executable.as_path(), "t1000 executable")?;
        validate_absolute_path(database_path.as_path(), "database")?;
        Ok(Self {
            executable: Arc::new(executable),
            database_path: Arc::new(database_path),
            command_timeout,
            operation_slots: Arc::new(Semaphore::new(MAX_TAB_ACTION_OPERATIONS)),
        })
    }

    pub(crate) async fn create(
        &self,
        window_id: &str,
        window_title: &str,
        cwd: &str,
        name: Option<&str>,
        command: Option<&str>,
        pinned: bool,
    ) -> Result<TabActionResult, TabActionError> {
        validate_identity(window_id)?;
        validate_text(window_title, 2_048, false).map_err(|_| TabActionError::InvalidIdentity)?;
        validate_cwd(cwd)?;
        if let Some(value) = name {
            validate_text(value, MAX_TAB_NAME_BYTES, false)
                .map_err(|_| TabActionError::InvalidName)?;
        }
        if let Some(value) = command {
            validate_text(value, MAX_TAB_COMMAND_BYTES, false)
                .map_err(|_| TabActionError::InvalidCommand)?;
        }
        let mut arguments = vec![
            OsString::from("new"),
            OsString::from("--db"),
            self.database_path.as_os_str().to_owned(),
            OsString::from("--cwd"),
            OsString::from(cwd),
            OsString::from("--window-id"),
            OsString::from(window_id),
            OsString::from("--window-title"),
            OsString::from(window_title),
        ];
        if let Some(value) = name {
            arguments.push(OsString::from("--name"));
            arguments.push(OsString::from(value));
        }
        if let Some(value) = command {
            arguments.push(OsString::from("--command"));
            arguments.push(OsString::from(value));
        }
        if pinned {
            arguments.push(OsString::from("--pinned"));
        }
        let payload = self.run(arguments.as_slice()).await?;
        Ok(TabActionResult {
            schema: "termdeck.tab-action.v1",
            action: "new",
            payload,
        })
    }

    pub(crate) async fn rename(
        &self,
        session_id: &str,
        name: &str,
    ) -> Result<TabActionResult, TabActionError> {
        validate_identity(session_id)?;
        validate_text(name, MAX_TAB_NAME_BYTES, false).map_err(|_| TabActionError::InvalidName)?;
        let payload = self
            .run(&[
                OsString::from("rename"),
                OsString::from("--db"),
                self.database_path.as_os_str().to_owned(),
                OsString::from(session_id),
                OsString::from(name),
            ])
            .await?;
        Ok(TabActionResult {
            schema: "termdeck.tab-action.v1",
            action: "rename",
            payload,
        })
    }

    pub(crate) async fn existing(
        &self,
        session_id: &str,
        action: ExistingTabAction,
        confirmation: Option<&str>,
    ) -> Result<TabActionResult, TabActionError> {
        validate_identity(session_id)?;
        if action == ExistingTabAction::Kill && confirmation != Some(session_id) {
            return Err(TabActionError::InvalidConfirmation);
        }
        let mut arguments = vec![
            OsString::from(action.command()),
            OsString::from("--db"),
            self.database_path.as_os_str().to_owned(),
        ];
        if matches!(action, ExistingTabAction::Pause | ExistingTabAction::Resume) {
            arguments.push(OsString::from("--yes"));
        }
        if action == ExistingTabAction::Hide {
            arguments.push(OsString::from("--include-pinned"));
        }
        if action == ExistingTabAction::Kill {
            arguments.push(OsString::from("--yes"));
            arguments.push(OsString::from("--include-pinned"));
        }
        arguments.push(OsString::from(session_id));
        let payload = self.run(arguments.as_slice()).await?;
        Ok(TabActionResult {
            schema: "termdeck.tab-action.v1",
            action: action.command(),
            payload,
        })
    }

    async fn run(&self, arguments: &[OsString]) -> Result<Value, TabActionError> {
        let _permit = Arc::clone(&self.operation_slots)
            .try_acquire_owned()
            .map_err(|_| TabActionError::Busy)?;
        let mut command = Command::new(self.executable.as_ref());
        command.args(arguments).kill_on_drop(true);
        let output = timeout(self.command_timeout, command.output())
            .await
            .map_err(|_| TabActionError::Timeout)??;
        let total = output.stdout.len().checked_add(output.stderr.len()).ok_or(
            TabActionError::OutputTooLarge {
                actual: usize::MAX,
                maximum: MAX_TAB_ACTION_OUTPUT_BYTES,
            },
        )?;
        if total > MAX_TAB_ACTION_OUTPUT_BYTES {
            return Err(TabActionError::OutputTooLarge {
                actual: total,
                maximum: MAX_TAB_ACTION_OUTPUT_BYTES,
            });
        }
        if !output.status.success() {
            return Err(TabActionError::ProcessFailed {
                status: output.status.code(),
                detail: bounded_lossy(output.stderr.as_slice()),
            });
        }
        Ok(serde_json::from_slice(output.stdout.as_slice())?)
    }
}

fn validate_absolute_path(path: &Path, field: &'static str) -> Result<(), TabActionError> {
    if !path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
    {
        return Err(TabActionError::InvalidPath(field));
    }
    Ok(())
}

fn validate_cwd(value: &str) -> Result<(), TabActionError> {
    validate_text(value, MAX_TAB_CWD_BYTES, false).map_err(|_| TabActionError::InvalidCwd)?;
    let path = Path::new(value);
    validate_absolute_path(path, "working directory").map_err(|_| TabActionError::InvalidCwd)?;
    let metadata = std::fs::metadata(path).map_err(|_| TabActionError::InvalidCwd)?;
    if !metadata.is_dir() {
        return Err(TabActionError::InvalidCwd);
    }
    Ok(())
}

fn validate_identity(value: &str) -> Result<(), TabActionError> {
    validate_text(value, 256, false).map_err(|_| TabActionError::InvalidIdentity)?;
    if !value
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
    {
        return Err(TabActionError::InvalidIdentity);
    }
    Ok(())
}

fn validate_text(value: &str, maximum: usize, allow_empty: bool) -> Result<(), ()> {
    if (!allow_empty && value.trim().is_empty())
        || value.len() > maximum
        || value.chars().any(char::is_control)
    {
        return Err(());
    }
    Ok(())
}

fn bounded_lossy(value: &[u8]) -> String {
    String::from_utf8_lossy(&value[..value.len().min(4_096)])
        .trim()
        .to_owned()
}

#[cfg(test)]
mod tests {
    use super::{
        ExistingTabAction, TabActionError, validate_cwd, validate_identity, validate_text,
    };

    #[test]
    fn action_parser_is_a_fixed_allowlist() {
        assert!(matches!(
            ExistingTabAction::parse("pause"),
            Ok(ExistingTabAction::Pause)
        ));
        assert!(matches!(
            ExistingTabAction::parse("hide"),
            Ok(ExistingTabAction::Hide)
        ));
        assert!(matches!(
            ExistingTabAction::parse("kill"),
            Ok(ExistingTabAction::Kill)
        ));
        assert!(matches!(
            ExistingTabAction::parse("run-shell"),
            Err(TabActionError::InvalidAction)
        ));
    }

    #[test]
    fn identifiers_and_text_are_bounded() {
        assert!(validate_identity("td-1234_ab").is_ok());
        assert!(validate_identity("../other").is_err());
        assert!(validate_text("New tab", 256, false).is_ok());
        assert!(validate_text("bad\nname", 256, false).is_err());
    }

    #[test]
    fn working_directory_must_be_absolute_and_live() {
        assert!(validate_cwd("/").is_ok());
        assert!(validate_cwd("relative").is_err());
    }
}
