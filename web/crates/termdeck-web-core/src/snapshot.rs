use crate::{DeviceId, HostId, OwnerId, PaneId, SessionId, WindowId, WindowRoute};
use std::collections::BTreeSet;
use std::error::Error;
use std::fmt;

pub const MAX_SNAPSHOT_WINDOWS: usize = 4_096;
pub const MAX_PANES_PER_WINDOW: usize = 4_096;
pub const MAX_SNAPSHOT_PANES: usize = 16_384;
pub const MAX_WINDOW_NAME_BYTES: usize = 1_024;
pub const MAX_PANE_TITLE_BYTES: usize = 2_048;
pub const MAX_CWD_BYTES: usize = 8_192;
pub const MAX_TMUX_TARGET_BYTES: usize = 512;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PaneStatus {
    New,
    Running,
    Paused,
    Missing,
    RecoveryPending,
    StartError,
    Dead,
    Stopped,
    Archived,
    Unknown,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ControllerView {
    device_id: DeviceId,
    epoch: u64,
    expires_at_unix_ms: u64,
}

impl ControllerView {
    pub fn new(
        device_id: DeviceId,
        epoch: u64,
        expires_at_unix_ms: u64,
    ) -> Result<Self, SnapshotError> {
        if epoch == 0 {
            return Err(SnapshotError::InvalidControllerEpoch);
        }
        Ok(Self {
            device_id,
            epoch,
            expires_at_unix_ms,
        })
    }

    #[must_use]
    pub fn device_id(&self) -> &DeviceId {
        &self.device_id
    }

    #[must_use]
    pub fn epoch(&self) -> u64 {
        self.epoch
    }

    #[must_use]
    pub fn expires_at_unix_ms(&self) -> u64 {
        self.expires_at_unix_ms
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PaneSnapshot {
    id: PaneId,
    session_id: Option<SessionId>,
    title: String,
    cwd: String,
    status: PaneStatus,
    pinned: bool,
    tmux_target: Option<String>,
    controller: Option<ControllerView>,
}

impl PaneSnapshot {
    pub fn new(
        id: PaneId,
        session_id: Option<SessionId>,
        title: impl Into<String>,
        cwd: impl Into<String>,
        status: PaneStatus,
        tmux_target: Option<String>,
        controller: Option<ControllerView>,
    ) -> Result<Self, SnapshotError> {
        let title = title.into();
        let cwd = cwd.into();
        validate_text("pane title", &title, MAX_PANE_TITLE_BYTES)?;
        validate_text("pane cwd", &cwd, MAX_CWD_BYTES)?;
        if let Some(target) = tmux_target.as_deref() {
            validate_text("tmux target", target, MAX_TMUX_TARGET_BYTES)?;
        }
        Ok(Self {
            id,
            session_id,
            title,
            cwd,
            status,
            pinned: false,
            tmux_target,
            controller,
        })
    }

    #[must_use]
    pub fn id(&self) -> &PaneId {
        &self.id
    }

    #[must_use]
    pub fn session_id(&self) -> Option<&SessionId> {
        self.session_id.as_ref()
    }

    #[must_use]
    pub fn title(&self) -> &str {
        &self.title
    }

    #[must_use]
    pub fn cwd(&self) -> &str {
        &self.cwd
    }

    #[must_use]
    pub fn status(&self) -> PaneStatus {
        self.status
    }

    #[must_use]
    pub fn with_pinned(mut self, pinned: bool) -> Self {
        self.pinned = pinned;
        self
    }

    #[must_use]
    pub fn pinned(&self) -> bool {
        self.pinned
    }

    #[must_use]
    pub fn tmux_target(&self) -> Option<&str> {
        self.tmux_target.as_deref()
    }

    #[must_use]
    pub fn controller(&self) -> Option<&ControllerView> {
        self.controller.as_ref()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WindowSnapshot {
    id: WindowId,
    name: String,
    route: WindowRoute,
    panes: Vec<PaneSnapshot>,
    active_pane_id: Option<PaneId>,
}

impl WindowSnapshot {
    pub fn new(
        id: WindowId,
        name: impl Into<String>,
        route: WindowRoute,
        panes: Vec<PaneSnapshot>,
        active_pane_id: Option<PaneId>,
    ) -> Result<Self, SnapshotError> {
        let name = name.into();
        validate_text("window name", &name, MAX_WINDOW_NAME_BYTES)?;
        if route.id() != &id {
            return Err(SnapshotError::RouteWindowMismatch {
                window_id: id,
                route_window_id: route.id().clone(),
            });
        }
        if panes.len() > MAX_PANES_PER_WINDOW {
            return Err(SnapshotError::TooManyPanesInWindow {
                window_id: route.id().clone(),
                actual: panes.len(),
                maximum: MAX_PANES_PER_WINDOW,
            });
        }
        let mut pane_ids = BTreeSet::new();
        for pane in &panes {
            if !pane_ids.insert(pane.id().clone()) {
                return Err(SnapshotError::DuplicatePaneId(pane.id().clone()));
            }
        }
        if let Some(active) = active_pane_id.as_ref()
            && !pane_ids.contains(active)
        {
            return Err(SnapshotError::ActivePaneMissing {
                window_id: route.id().clone(),
                pane_id: active.clone(),
            });
        }
        Ok(Self {
            id,
            name,
            route,
            panes,
            active_pane_id,
        })
    }

    #[must_use]
    pub fn id(&self) -> &WindowId {
        &self.id
    }

    #[must_use]
    pub fn name(&self) -> &str {
        &self.name
    }

    #[must_use]
    pub fn route(&self) -> &WindowRoute {
        &self.route
    }

    #[must_use]
    pub fn panes(&self) -> &[PaneSnapshot] {
        &self.panes
    }

    #[must_use]
    pub fn active_pane_id(&self) -> Option<&PaneId> {
        self.active_pane_id.as_ref()
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HostSnapshot {
    host_id: HostId,
    owner_id: OwnerId,
    revision: u64,
    generated_at_unix_ms: u64,
    windows: Vec<WindowSnapshot>,
    total_panes: usize,
}

impl HostSnapshot {
    pub fn new(
        host_id: HostId,
        owner_id: OwnerId,
        revision: u64,
        generated_at_unix_ms: u64,
        windows: Vec<WindowSnapshot>,
    ) -> Result<Self, SnapshotError> {
        if windows.len() > MAX_SNAPSHOT_WINDOWS {
            return Err(SnapshotError::TooManyWindows {
                actual: windows.len(),
                maximum: MAX_SNAPSHOT_WINDOWS,
            });
        }
        let mut window_ids = BTreeSet::new();
        let mut route_slugs = BTreeSet::new();
        let mut pane_ids = BTreeSet::new();
        let mut total_panes = 0usize;
        for window in &windows {
            if !window_ids.insert(window.id().clone()) {
                return Err(SnapshotError::DuplicateWindowId(window.id().clone()));
            }
            if !route_slugs.insert(window.route().slug().to_owned()) {
                return Err(SnapshotError::DuplicateRouteSlug(
                    window.route().slug().to_owned(),
                ));
            }
            total_panes = total_panes.saturating_add(window.panes().len());
            if total_panes > MAX_SNAPSHOT_PANES {
                return Err(SnapshotError::TooManyPanes {
                    actual: total_panes,
                    maximum: MAX_SNAPSHOT_PANES,
                });
            }
            for pane in window.panes() {
                if !pane_ids.insert(pane.id().clone()) {
                    return Err(SnapshotError::DuplicatePaneId(pane.id().clone()));
                }
            }
        }
        Ok(Self {
            host_id,
            owner_id,
            revision,
            generated_at_unix_ms,
            windows,
            total_panes,
        })
    }

    #[must_use]
    pub fn host_id(&self) -> &HostId {
        &self.host_id
    }

    #[must_use]
    pub fn owner_id(&self) -> &OwnerId {
        &self.owner_id
    }

    #[must_use]
    pub fn revision(&self) -> u64 {
        self.revision
    }

    #[must_use]
    pub fn generated_at_unix_ms(&self) -> u64 {
        self.generated_at_unix_ms
    }

    #[must_use]
    pub fn windows(&self) -> &[WindowSnapshot] {
        &self.windows
    }

    #[must_use]
    pub fn total_panes(&self) -> usize {
        self.total_panes
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SnapshotError {
    TextTooLong {
        field: &'static str,
        actual_bytes: usize,
        maximum_bytes: usize,
    },
    ControlCharacter {
        field: &'static str,
    },
    InvalidControllerEpoch,
    TooManyWindows {
        actual: usize,
        maximum: usize,
    },
    TooManyPanesInWindow {
        window_id: WindowId,
        actual: usize,
        maximum: usize,
    },
    TooManyPanes {
        actual: usize,
        maximum: usize,
    },
    DuplicateWindowId(WindowId),
    DuplicateRouteSlug(String),
    DuplicatePaneId(PaneId),
    RouteWindowMismatch {
        window_id: WindowId,
        route_window_id: WindowId,
    },
    ActivePaneMissing {
        window_id: WindowId,
        pane_id: PaneId,
    },
}

impl fmt::Display for SnapshotError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
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
            Self::InvalidControllerEpoch => formatter.write_str("controller epoch must be nonzero"),
            Self::TooManyWindows { actual, maximum } => {
                write!(formatter, "{actual} windows exceeds maximum {maximum}")
            }
            Self::TooManyPanesInWindow {
                window_id,
                actual,
                maximum,
            } => write!(
                formatter,
                "window {window_id} has {actual} panes; maximum is {maximum}"
            ),
            Self::TooManyPanes { actual, maximum } => {
                write!(formatter, "{actual} panes exceeds maximum {maximum}")
            }
            Self::DuplicateWindowId(window_id) => {
                write!(formatter, "duplicate window id {window_id}")
            }
            Self::DuplicateRouteSlug(slug) => {
                write!(formatter, "duplicate window route slug {slug}")
            }
            Self::DuplicatePaneId(pane_id) => write!(formatter, "duplicate pane id {pane_id}"),
            Self::RouteWindowMismatch {
                window_id,
                route_window_id,
            } => write!(
                formatter,
                "window {window_id} cannot use route owned by {route_window_id}"
            ),
            Self::ActivePaneMissing { window_id, pane_id } => write!(
                formatter,
                "window {window_id} active pane {pane_id} is absent from its pane list"
            ),
        }
    }
}

impl Error for SnapshotError {}

fn validate_text(
    field: &'static str,
    value: &str,
    maximum_bytes: usize,
) -> Result<(), SnapshotError> {
    if value.len() > maximum_bytes {
        return Err(SnapshotError::TextTooLong {
            field,
            actual_bytes: value.len(),
            maximum_bytes,
        });
    }
    if value.chars().any(char::is_control) {
        return Err(SnapshotError::ControlCharacter { field });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        ControllerView, HostSnapshot, MAX_PANE_TITLE_BYTES, PaneSnapshot, PaneStatus,
        SnapshotError, WindowSnapshot,
    };
    use crate::{DeviceId, HostId, OwnerId, PaneId, SessionId, WindowId, WindowRoute};

    fn id<T>(result: Result<T, crate::IdError>) -> T {
        match result {
            Ok(value) => value,
            Err(error) => panic!("invalid test id: {error}"),
        }
    }

    fn route(window_id: &WindowId, slug: &str) -> WindowRoute {
        match WindowRoute::restored(window_id.clone(), slug) {
            Ok(value) => value,
            Err(error) => panic!("invalid test route: {error}"),
        }
    }

    fn pane(pane_id: &str) -> PaneSnapshot {
        match PaneSnapshot::new(
            id(PaneId::new(pane_id)),
            Some(id(SessionId::new(format!("session-{pane_id}")))),
            format!("Pane {pane_id}"),
            "/tmp",
            PaneStatus::Running,
            Some(format!("%{pane_id}")),
            None,
        ) {
            Ok(value) => value,
            Err(error) => panic!("invalid test pane: {error}"),
        }
    }

    fn window(window_id: &str, slug: &str, panes: Vec<PaneSnapshot>) -> WindowSnapshot {
        let window_id = id(WindowId::new(window_id));
        let active = panes.first().map(|value| value.id().clone());
        match WindowSnapshot::new(
            window_id.clone(),
            format!("Window {window_id}"),
            route(&window_id, slug),
            panes,
            active,
        ) {
            Ok(value) => value,
            Err(error) => panic!("invalid test window: {error}"),
        }
    }

    #[test]
    fn immutable_snapshot_preserves_host_window_and_pane_identity() {
        let snapshot = match HostSnapshot::new(
            id(HostId::new("host-1")),
            id(OwnerId::new("owner-1")),
            42,
            1_700_000_000_000,
            vec![window("window-1", "random", vec![pane("1")])],
        ) {
            Ok(value) => value,
            Err(error) => panic!("invalid test snapshot: {error}"),
        };
        assert_eq!(snapshot.revision(), 42);
        assert_eq!(snapshot.windows()[0].route().path(), "/random");
        assert_eq!(snapshot.windows()[0].panes()[0].cwd(), "/tmp");
        assert_eq!(snapshot.total_panes(), 1);
    }

    #[test]
    fn active_pane_must_belong_to_its_window() {
        let window_id = id(WindowId::new("window-1"));
        let result = WindowSnapshot::new(
            window_id.clone(),
            "Random",
            route(&window_id, "random"),
            vec![pane("1")],
            Some(id(PaneId::new("missing"))),
        );
        assert!(matches!(
            result,
            Err(SnapshotError::ActivePaneMissing { .. })
        ));
    }

    #[test]
    fn route_must_belong_to_the_same_window() {
        let owner = id(WindowId::new("window-route-owner"));
        let result = WindowSnapshot::new(
            id(WindowId::new("window-1")),
            "Random",
            route(&owner, "random"),
            vec![],
            None,
        );
        assert!(matches!(
            result,
            Err(SnapshotError::RouteWindowMismatch { .. })
        ));
    }

    #[test]
    fn pane_ids_are_unique_across_the_host() {
        let result = HostSnapshot::new(
            id(HostId::new("host-1")),
            id(OwnerId::new("owner-1")),
            1,
            1,
            vec![
                window("window-1", "one", vec![pane("shared")]),
                window("window-2", "two", vec![pane("shared")]),
            ],
        );
        assert!(matches!(result, Err(SnapshotError::DuplicatePaneId(_))));
    }

    #[test]
    fn unbounded_or_control_bearing_text_fails_closed() {
        let too_long = PaneSnapshot::new(
            id(PaneId::new("pane-1")),
            None,
            "x".repeat(MAX_PANE_TITLE_BYTES + 1),
            "/tmp",
            PaneStatus::Unknown,
            None,
            None,
        );
        assert!(matches!(too_long, Err(SnapshotError::TextTooLong { .. })));

        let control = PaneSnapshot::new(
            id(PaneId::new("pane-1")),
            None,
            "unsafe\ntitle",
            "/tmp",
            PaneStatus::Unknown,
            None,
            None,
        );
        assert!(matches!(
            control,
            Err(SnapshotError::ControlCharacter { .. })
        ));
    }

    #[test]
    fn controller_view_exposes_no_lease_secret_and_requires_nonzero_epoch() {
        assert!(matches!(
            ControllerView::new(id(DeviceId::new("device-1")), 0, 10),
            Err(SnapshotError::InvalidControllerEpoch)
        ));
        let view = match ControllerView::new(id(DeviceId::new("device-1")), 3, 10) {
            Ok(value) => value,
            Err(error) => panic!("invalid controller view: {error}"),
        };
        assert_eq!(view.epoch(), 3);
        assert_eq!(view.device_id().as_str(), "device-1");
    }
}
