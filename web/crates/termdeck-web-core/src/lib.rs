#![forbid(unsafe_code)]

//! Dependency-free domain invariants for the TermDeck web host.
//!
//! This crate deliberately contains no networking, persistence, process control,
//! terminal transport, or authentication-secret handling. It defines the bounded
//! immutable state and authority contracts that later adapters must satisfy.

mod control;
mod ids;
mod route;
mod snapshot;

pub use control::{
    AcquireMode, AuthenticatedDevice, ControlError, ControllerToken, DaemonGeneration,
    MAX_CONTROLLED_PANES, MAX_CONTROLLER_TTL_MS, PaneControllerRegistry,
};
pub use ids::{DeviceId, HostId, IdError, LeaseId, OwnerId, PaneId, SessionId, WindowId};
pub use route::{
    INTERNAL_ROUTE_PREFIX, MAX_RETIRED_SLUGS, MAX_ROUTED_WINDOWS, MAX_SLUG_BYTES, RouteError,
    RouteRegistry, RouteTarget, WindowRoute, WindowRouteInput, slug_base,
};
pub use snapshot::{
    ControllerView, HostSnapshot, MAX_CWD_BYTES, MAX_PANE_TITLE_BYTES, MAX_PANES_PER_WINDOW,
    MAX_SNAPSHOT_PANES, MAX_SNAPSHOT_WINDOWS, MAX_TMUX_TARGET_BYTES, MAX_WINDOW_NAME_BYTES,
    PaneSnapshot, PaneStatus, SnapshotError, WindowSnapshot,
};
