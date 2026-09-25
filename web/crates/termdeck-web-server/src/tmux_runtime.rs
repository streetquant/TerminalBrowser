//! Persistent tmux routing for every web inventory, input and stream command.
//! Storage admission happens in the release-bound Python service wrapper.
//! Recheck its path/identity contract here after exec and before each operation.

use std::fs;
use std::io;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::path::{Component, Path, PathBuf};
use std::sync::OnceLock;
use std::time::Duration;
use tokio::net::UnixStream;
use tokio::process::Command;

#[derive(Debug)]
struct Runtime {
    root: PathBuf,
    device: u64,
    inode: u64,
    uid: u32,
}

fn refused(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}

impl Runtime {
    fn from_environment() -> io::Result<Option<Self>> {
        let root = std::env::var_os("TERMDECK_TMUX_RUNTIME");
        let state = std::env::var_os("TERMDECK_STATE_ROOT");
        if root.is_none() && state.is_none() {
            // Explicit disposable/development invocations keep their private
            // TMUX_TMPDIR. A managed service can never enter this branch.
            return Ok(None);
        }
        let root = PathBuf::from(root.ok_or_else(|| refused("managed tmux runtime is missing"))?);
        if let Some(state) = state {
            if root != PathBuf::from(state).join("tmux") {
                return Err(refused("managed tmux runtime disagrees with state root"));
            }
        }
        if std::env::var_os("TMUX_TMPDIR").as_deref() != Some(root.as_os_str()) {
            return Err(refused("managed tmux environment drifted"));
        }
        let identity = std::env::var("TERMDECK_TMUX_IDENTITY")
            .map_err(|_| refused("managed tmux admission identity is missing"))?;
        let parts: Vec<&str> = identity.split(':').collect();
        if parts.len() != 3 {
            return Err(refused("managed tmux admission identity is malformed"));
        }
        let result = Self {
            root,
            device: parts[0]
                .parse()
                .map_err(|_| refused("invalid tmux device"))?,
            inode: parts[1]
                .parse()
                .map_err(|_| refused("invalid tmux inode"))?,
            uid: parts[2]
                .parse()
                .map_err(|_| refused("invalid tmux owner"))?,
        };
        if result.uid != fs::metadata("/proc/self")?.uid() {
            return Err(refused("managed tmux owner differs from service user"));
        }
        result.validate(false)?;
        Ok(Some(result))
    }

    fn socket(&self) -> PathBuf {
        self.root.join(format!("tmux-{}", self.uid)).join("default")
    }

    fn validate(&self, require_socket: bool) -> io::Result<()> {
        if !self.root.is_absolute()
            || self
                .root
                .components()
                .any(|c| !matches!(c, Component::RootDir | Component::Normal(_)))
            || self.root.starts_with("/proc")
            || self.root.starts_with("/dev/fd")
        {
            return Err(refused("tmux runtime must be a persistent absolute path"));
        }
        let mut current = PathBuf::new();
        for part in self.root.components() {
            current.push(part);
            let info = fs::symlink_metadata(&current)?;
            if !info.is_dir() || info.file_type().is_symlink() {
                return Err(refused("tmux runtime contains a symlink or non-directory"));
            }
        }
        let root = fs::symlink_metadata(&self.root)?;
        if root.dev() != self.device
            || root.ino() != self.inode
            || root.uid() != self.uid
            || root.mode() & 0o777 != 0o700
        {
            return Err(refused(
                "tmux runtime identity, owner or permissions changed",
            ));
        }
        let socket = self.socket();
        let parent = socket
            .parent()
            .ok_or_else(|| refused("invalid tmux socket path"))?;
        for (path, is_socket) in [(parent, false), (socket.as_path(), true)] {
            let info = match fs::symlink_metadata(path) {
                Ok(info) => info,
                Err(error) if error.kind() == io::ErrorKind::NotFound && !require_socket => {
                    return Ok(());
                }
                Err(error) => return Err(error),
            };
            if info.file_type().is_symlink()
                || info.uid() != self.uid
                || info.dev() != self.device
                || (is_socket && !matches!(info.mode() & 0o777, 0o600 | 0o660 | 0o700 | 0o770))
                || (is_socket && !info.file_type().is_socket())
                || (!is_socket && (!info.is_dir() || info.mode() & 0o777 != 0o700))
            {
                return Err(refused(
                    "tmux socket owner, type, device or permissions are unsafe",
                ));
            }
        }
        Ok(())
    }

    async fn validate_peer(&self, executable: &Path) -> io::Result<()> {
        let stream = tokio::time::timeout(
            Duration::from_millis(250),
            UnixStream::connect(self.socket()),
        )
        .await
        .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "tmux peer check timed out"))??;
        let peer = stream.peer_cred()?;
        let pid = peer
            .pid()
            .filter(|pid| *pid > 1)
            .ok_or_else(|| refused("tmux peer PID is unavailable"))?;
        if peer.uid() != self.uid {
            return Err(refused("tmux peer owner is foreign"));
        }
        let peer_exe = fs::metadata(format!("/proc/{pid}/exe"))?;
        let expected_exe = fs::metadata(executable)?;
        if (peer_exe.dev(), peer_exe.ino()) != (expected_exe.dev(), expected_exe.ino()) {
            return Err(refused("tmux peer executable is foreign"));
        }
        let environment = fs::read(format!("/proc/{pid}/environ"))?;
        let expected = format!("TMUX_TMPDIR={}", self.root.display());
        if !environment
            .split(|byte| *byte == 0)
            .any(|field| field == expected.as_bytes())
        {
            return Err(refused("tmux peer runtime is foreign"));
        }
        self.validate(true)
    }
}

static RUNTIME: OnceLock<Result<Option<Runtime>, String>> = OnceLock::new();

fn admitted() -> io::Result<Option<&'static Runtime>> {
    match RUNTIME.get_or_init(|| Runtime::from_environment().map_err(|e| e.to_string())) {
        Ok(runtime) => Ok(runtime.as_ref()),
        Err(error) => Err(refused(error)),
    }
}

pub(crate) fn initialize() -> io::Result<()> {
    admitted().map(|_| ())
}

pub(crate) async fn command(executable: &Path) -> io::Result<Command> {
    let mut command = Command::new(executable);
    command.env_remove("TMUX");
    if let Some(runtime) = admitted()? {
        runtime.validate(true)?;
        runtime.validate_peer(executable).await?;
        command.arg("-S").arg(runtime.socket());
        command.env("TMUX_TMPDIR", &runtime.root);
    }
    Ok(command)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use std::os::unix::net::UnixListener;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    struct Fixture(Runtime);

    impl Fixture {
        fn new() -> Result<Self, Box<dyn std::error::Error>> {
            let root = std::env::temp_dir().join(format!(
                "td-rt-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root)?;
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700))?;
            let info = fs::metadata(&root)?;
            Ok(Self(Runtime {
                root,
                device: info.dev(),
                inode: info.ino(),
                uid: info.uid(),
            }))
        }

        fn listen(&self) -> io::Result<UnixListener> {
            let socket = self.0.socket();
            let parent = socket.parent().ok_or_else(|| refused("missing parent"))?;
            fs::create_dir(parent)?;
            fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
            let listener = UnixListener::bind(&socket)?;
            fs::set_permissions(socket, fs::Permissions::from_mode(0o600))?;
            Ok(listener)
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _result = fs::remove_dir_all(&self.0.root);
        }
    }

    #[test]
    fn absent_socket_allows_startup_but_never_terminal_operations()
    -> Result<(), Box<dyn std::error::Error>> {
        let fixture = Fixture::new()?;
        fixture.0.validate(false)?;
        assert_eq!(
            fixture.0.validate(true).err().map(|e| e.kind()),
            Some(io::ErrorKind::NotFound)
        );
        assert!(!fixture.0.socket().exists());
        Ok(())
    }

    #[test]
    fn runtime_owner_device_identity_and_modes_are_checked()
    -> Result<(), Box<dyn std::error::Error>> {
        let mut fixture = Fixture::new()?;
        let _listener = fixture.listen()?;
        fixture.0.validate(true)?;
        fixture.0.uid += 1;
        assert!(fixture.0.validate(true).is_err());
        fixture.0.uid -= 1;
        fixture.0.device += 1;
        assert!(fixture.0.validate(true).is_err());
        fixture.0.device -= 1;
        fixture.0.inode += 1;
        assert!(fixture.0.validate(true).is_err());
        fixture.0.inode -= 1;
        fs::set_permissions(&fixture.0.root, fs::Permissions::from_mode(0o755))?;
        assert!(fixture.0.validate(true).is_err());
        Ok(())
    }

    #[test]
    fn socket_modes_types_and_symlinks_are_rejected() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = Fixture::new()?;
        let listener = fixture.listen()?;
        let socket = fixture.0.socket();
        fs::set_permissions(&socket, fs::Permissions::from_mode(0o666))?;
        assert!(fixture.0.validate(true).is_err());
        drop(listener);
        fs::remove_file(&socket)?;
        fs::write(&socket, b"not a socket")?;
        assert!(fixture.0.validate(true).is_err());
        fs::remove_file(&socket)?;
        symlink("/tmp/foreign-tmux", &socket)?;
        assert!(fixture.0.validate(true).is_err());
        Ok(())
    }

    #[test]
    fn foreign_listener_fails_peer_executable_check() -> Result<(), Box<dyn std::error::Error>> {
        let fixture = Fixture::new()?;
        let _listener = fixture.listen()?;
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        let result = runtime.block_on(fixture.0.validate_peer(Path::new("/usr/bin/tmux")));
        assert!(
            result
                .err()
                .is_some_and(|error| error.to_string().contains("executable is foreign"))
        );
        Ok(())
    }

    #[test]
    fn parent_replacement_fails_without_using_replacement() -> Result<(), Box<dyn std::error::Error>>
    {
        let fixture = Fixture::new()?;
        let replacement = Fixture::new()?;
        let moved = fixture.0.root.with_extension("moved");
        fs::rename(&fixture.0.root, &moved)?;
        symlink(&replacement.0.root, &fixture.0.root)?;
        assert!(fixture.0.validate(false).is_err());
        fs::remove_file(&fixture.0.root)?;
        fs::rename(&moved, &fixture.0.root)?;
        Ok(())
    }
}
