use crate::{DeviceId, LeaseId, OwnerId, PaneId};
use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;

pub const MAX_CONTROLLER_TTL_MS: u64 = 24 * 60 * 60 * 1_000;
pub const MAX_CONTROLLED_PANES: usize = 16_384;

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct DaemonGeneration(u128);

impl DaemonGeneration {
    pub fn new(value: u128) -> Result<Self, ControlError> {
        if value == 0 {
            return Err(ControlError::InvalidDaemonGeneration);
        }
        Ok(Self(value))
    }

    #[must_use]
    pub fn value(self) -> u128 {
        self.0
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AuthenticatedDevice {
    owner_id: OwnerId,
    device_id: DeviceId,
}

impl AuthenticatedDevice {
    #[must_use]
    pub fn new(owner_id: OwnerId, device_id: DeviceId) -> Self {
        Self {
            owner_id,
            device_id,
        }
    }

    #[must_use]
    pub fn owner_id(&self) -> &OwnerId {
        &self.owner_id
    }

    #[must_use]
    pub fn device_id(&self) -> &DeviceId {
        &self.device_id
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AcquireMode {
    IfVacant,
    Takeover,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ControllerToken {
    generation: DaemonGeneration,
    pane_id: PaneId,
    device_id: DeviceId,
    lease_id: LeaseId,
    epoch: u64,
    expires_at_unix_ms: u64,
}

impl ControllerToken {
    #[must_use]
    pub fn generation(&self) -> DaemonGeneration {
        self.generation
    }

    #[must_use]
    pub fn pane_id(&self) -> &PaneId {
        &self.pane_id
    }

    #[must_use]
    pub fn device_id(&self) -> &DeviceId {
        &self.device_id
    }

    #[must_use]
    pub fn lease_id(&self) -> &LeaseId {
        &self.lease_id
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
struct ActiveLease {
    token: ControllerToken,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ControlError {
    InvalidDaemonGeneration,
    InvalidMaximumTtl,
    InvalidMaximumControlledPanes,
    InvalidTtl {
        requested_ms: u64,
        maximum_ms: u64,
    },
    ForeignOwner {
        expected: OwnerId,
        actual: OwnerId,
    },
    TooManyControlledPanes {
        maximum: usize,
    },
    DuplicateActiveLeaseId(LeaseId),
    PaneAlreadyControlled {
        pane_id: PaneId,
        device_id: DeviceId,
        expires_at_unix_ms: u64,
    },
    GenerationMismatch {
        expected: DaemonGeneration,
        actual: DaemonGeneration,
    },
    LeaseExpired,
    StaleCapability,
    EpochExhausted(PaneId),
    TimeOverflow,
}

impl fmt::Display for ControlError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidDaemonGeneration => {
                formatter.write_str("daemon generation must be nonzero")
            }
            Self::InvalidMaximumTtl => formatter.write_str("maximum controller TTL is invalid"),
            Self::InvalidMaximumControlledPanes => {
                formatter.write_str("maximum controlled pane count is invalid")
            }
            Self::InvalidTtl {
                requested_ms,
                maximum_ms,
            } => write!(
                formatter,
                "controller TTL {requested_ms} ms must be between 1 and {maximum_ms} ms"
            ),
            Self::ForeignOwner { expected, actual } => {
                write!(
                    formatter,
                    "device owner {actual} does not match host owner {expected}"
                )
            }
            Self::TooManyControlledPanes { maximum } => {
                write!(formatter, "controlled pane count reached maximum {maximum}")
            }
            Self::DuplicateActiveLeaseId(lease_id) => {
                write!(formatter, "lease id {lease_id} is already active")
            }
            Self::PaneAlreadyControlled {
                pane_id,
                device_id,
                expires_at_unix_ms,
            } => write!(
                formatter,
                "pane {pane_id} is controlled by device {device_id} until {expires_at_unix_ms}"
            ),
            Self::GenerationMismatch { expected, actual } => write!(
                formatter,
                "controller capability generation {} does not match daemon generation {}",
                actual.value(),
                expected.value()
            ),
            Self::LeaseExpired => formatter.write_str("controller lease expired"),
            Self::StaleCapability => formatter.write_str("controller capability is stale"),
            Self::EpochExhausted(pane_id) => {
                write!(formatter, "controller epoch exhausted for pane {pane_id}")
            }
            Self::TimeOverflow => formatter.write_str("controller lease time overflow"),
        }
    }
}

impl Error for ControlError {}

#[derive(Clone, Debug)]
pub struct PaneControllerRegistry {
    owner_id: OwnerId,
    generation: DaemonGeneration,
    maximum_ttl_ms: u64,
    maximum_controlled_panes: usize,
    active_by_pane: BTreeMap<PaneId, ActiveLease>,
    pane_by_lease: BTreeMap<LeaseId, PaneId>,
    last_epoch_by_pane: BTreeMap<PaneId, u64>,
}

impl PaneControllerRegistry {
    pub fn new(
        owner_id: OwnerId,
        generation: DaemonGeneration,
        maximum_ttl_ms: u64,
        maximum_controlled_panes: usize,
    ) -> Result<Self, ControlError> {
        if maximum_ttl_ms == 0 || maximum_ttl_ms > MAX_CONTROLLER_TTL_MS {
            return Err(ControlError::InvalidMaximumTtl);
        }
        if maximum_controlled_panes == 0 || maximum_controlled_panes > MAX_CONTROLLED_PANES {
            return Err(ControlError::InvalidMaximumControlledPanes);
        }
        Ok(Self {
            owner_id,
            generation,
            maximum_ttl_ms,
            maximum_controlled_panes,
            active_by_pane: BTreeMap::new(),
            pane_by_lease: BTreeMap::new(),
            last_epoch_by_pane: BTreeMap::new(),
        })
    }

    #[must_use]
    pub fn generation(&self) -> DaemonGeneration {
        self.generation
    }

    #[must_use]
    pub fn active_count(&self) -> usize {
        self.active_by_pane.len()
    }

    pub fn acquire(
        &mut self,
        device: &AuthenticatedDevice,
        pane_id: PaneId,
        lease_id: LeaseId,
        now_unix_ms: u64,
        requested_ttl_ms: u64,
        mode: AcquireMode,
    ) -> Result<ControllerToken, ControlError> {
        self.validate_device(device)?;
        self.validate_ttl(requested_ttl_ms)?;
        self.expire_pane(&pane_id, now_unix_ms);

        if let Some(active) = self.active_by_pane.get(&pane_id)
            && mode == AcquireMode::IfVacant
        {
            return Err(ControlError::PaneAlreadyControlled {
                pane_id,
                device_id: active.token.device_id.clone(),
                expires_at_unix_ms: active.token.expires_at_unix_ms,
            });
        }
        if self.pane_by_lease.contains_key(&lease_id) {
            return Err(ControlError::DuplicateActiveLeaseId(lease_id));
        }
        if !self.active_by_pane.contains_key(&pane_id)
            && self.active_by_pane.len() >= self.maximum_controlled_panes
        {
            return Err(ControlError::TooManyControlledPanes {
                maximum: self.maximum_controlled_panes,
            });
        }

        let previous_epoch = self.last_epoch_by_pane.get(&pane_id).copied().unwrap_or(0);
        let Some(epoch) = previous_epoch.checked_add(1) else {
            return Err(ControlError::EpochExhausted(pane_id));
        };
        let Some(expires_at_unix_ms) = now_unix_ms.checked_add(requested_ttl_ms) else {
            return Err(ControlError::TimeOverflow);
        };
        if self.active_by_pane.contains_key(&pane_id) {
            self.remove_active(&pane_id);
        }
        let token = ControllerToken {
            generation: self.generation,
            pane_id: pane_id.clone(),
            device_id: device.device_id.clone(),
            lease_id: lease_id.clone(),
            epoch,
            expires_at_unix_ms,
        };
        self.last_epoch_by_pane.insert(pane_id.clone(), epoch);
        self.pane_by_lease.insert(lease_id, pane_id.clone());
        self.active_by_pane.insert(
            pane_id,
            ActiveLease {
                token: token.clone(),
            },
        );
        Ok(token)
    }

    pub fn authorize_input(
        &mut self,
        token: &ControllerToken,
        now_unix_ms: u64,
    ) -> Result<(), ControlError> {
        self.validate_active(token, now_unix_ms)
    }

    pub fn heartbeat(
        &mut self,
        token: &ControllerToken,
        now_unix_ms: u64,
        requested_ttl_ms: u64,
    ) -> Result<ControllerToken, ControlError> {
        self.validate_ttl(requested_ttl_ms)?;
        self.validate_active(token, now_unix_ms)?;
        let Some(expires_at_unix_ms) = now_unix_ms.checked_add(requested_ttl_ms) else {
            return Err(ControlError::TimeOverflow);
        };
        let Some(active) = self.active_by_pane.get_mut(token.pane_id()) else {
            return Err(ControlError::StaleCapability);
        };
        active.token.expires_at_unix_ms = expires_at_unix_ms;
        Ok(active.token.clone())
    }

    pub fn release(
        &mut self,
        token: &ControllerToken,
        now_unix_ms: u64,
    ) -> Result<(), ControlError> {
        self.validate_active(token, now_unix_ms)?;
        self.remove_active(token.pane_id());
        Ok(())
    }

    pub fn current(&mut self, pane_id: &PaneId, now_unix_ms: u64) -> Option<ControllerToken> {
        self.expire_pane(pane_id, now_unix_ms);
        self.active_by_pane
            .get(pane_id)
            .map(|active| active.token.clone())
    }

    pub fn purge_expired(&mut self, now_unix_ms: u64) -> usize {
        let expired: Vec<PaneId> = self
            .active_by_pane
            .iter()
            .filter(|(_, active)| active.token.expires_at_unix_ms <= now_unix_ms)
            .map(|(pane_id, _)| pane_id.clone())
            .collect();
        let count = expired.len();
        for pane_id in expired {
            self.remove_active(&pane_id);
        }
        count
    }

    fn validate_device(&self, device: &AuthenticatedDevice) -> Result<(), ControlError> {
        if device.owner_id != self.owner_id {
            return Err(ControlError::ForeignOwner {
                expected: self.owner_id.clone(),
                actual: device.owner_id.clone(),
            });
        }
        Ok(())
    }

    fn validate_ttl(&self, requested_ttl_ms: u64) -> Result<(), ControlError> {
        if requested_ttl_ms == 0 || requested_ttl_ms > self.maximum_ttl_ms {
            return Err(ControlError::InvalidTtl {
                requested_ms: requested_ttl_ms,
                maximum_ms: self.maximum_ttl_ms,
            });
        }
        Ok(())
    }

    fn validate_active(
        &mut self,
        token: &ControllerToken,
        now_unix_ms: u64,
    ) -> Result<(), ControlError> {
        if token.generation != self.generation {
            return Err(ControlError::GenerationMismatch {
                expected: self.generation,
                actual: token.generation,
            });
        }
        if self
            .active_by_pane
            .get(token.pane_id())
            .is_some_and(|active| active.token.expires_at_unix_ms <= now_unix_ms)
        {
            self.remove_active(token.pane_id());
            return Err(ControlError::LeaseExpired);
        }
        let Some(active) = self.active_by_pane.get(token.pane_id()) else {
            return Err(ControlError::StaleCapability);
        };
        if active.token != *token {
            return Err(ControlError::StaleCapability);
        }
        Ok(())
    }

    fn expire_pane(&mut self, pane_id: &PaneId, now_unix_ms: u64) {
        if self
            .active_by_pane
            .get(pane_id)
            .is_some_and(|active| active.token.expires_at_unix_ms <= now_unix_ms)
        {
            self.remove_active(pane_id);
        }
    }

    fn remove_active(&mut self, pane_id: &PaneId) {
        if let Some(active) = self.active_by_pane.remove(pane_id) {
            self.pane_by_lease.remove(active.token.lease_id());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        AcquireMode, AuthenticatedDevice, ControlError, DaemonGeneration, PaneControllerRegistry,
    };
    use crate::{DeviceId, LeaseId, OwnerId, PaneId};

    fn id<T>(result: Result<T, crate::IdError>) -> T {
        match result {
            Ok(value) => value,
            Err(error) => panic!("invalid test id: {error}"),
        }
    }

    fn generation(value: u128) -> DaemonGeneration {
        match DaemonGeneration::new(value) {
            Ok(generation) => generation,
            Err(error) => panic!("invalid test generation: {error}"),
        }
    }

    fn owner() -> OwnerId {
        id(OwnerId::new("owner-1"))
    }

    fn device(value: &str) -> AuthenticatedDevice {
        AuthenticatedDevice::new(owner(), id(DeviceId::new(value)))
    }

    fn foreign_device(value: &str) -> AuthenticatedDevice {
        AuthenticatedDevice::new(id(OwnerId::new("owner-2")), id(DeviceId::new(value)))
    }

    fn registry(generation_value: u128) -> PaneControllerRegistry {
        match PaneControllerRegistry::new(owner(), generation(generation_value), 10_000, 16) {
            Ok(registry) => registry,
            Err(error) => panic!("invalid test registry: {error}"),
        }
    }

    fn acquire(
        registry: &mut PaneControllerRegistry,
        device: &AuthenticatedDevice,
        pane: &str,
        lease: &str,
        now: u64,
        mode: AcquireMode,
    ) -> super::ControllerToken {
        match registry.acquire(
            device,
            id(PaneId::new(pane)),
            id(LeaseId::new(lease)),
            now,
            1_000,
            mode,
        ) {
            Ok(token) => token,
            Err(error) => panic!("unexpected acquire error: {error}"),
        }
    }

    #[test]
    fn authenticated_devices_are_viewers_until_one_holds_the_pane_lease() {
        let mut registry = registry(1);
        let devices: Vec<AuthenticatedDevice> = (0..1_000)
            .map(|index| device(&format!("device-{index}")))
            .collect();
        let pane_id = id(PaneId::new("pane-1"));
        assert_eq!(registry.active_count(), 0);
        assert_eq!(registry.current(&pane_id, 10), None);

        let first = match devices.first() {
            Some(value) => value,
            None => panic!("missing first authenticated device"),
        };
        let first_token = acquire(
            &mut registry,
            first,
            "pane-1",
            "lease-1",
            10,
            AcquireMode::IfVacant,
        );
        assert_eq!(first_token.device_id(), first.device_id());

        let last = match devices.last() {
            Some(value) => value,
            None => panic!("missing last authenticated device"),
        };
        let takeover = acquire(
            &mut registry,
            last,
            "pane-1",
            "lease-2",
            11,
            AcquireMode::Takeover,
        );
        assert_eq!(takeover.device_id(), last.device_id());
        assert_eq!(registry.active_count(), 1);
    }

    #[test]
    fn one_pane_has_at_most_one_controller_without_explicit_takeover() {
        let mut registry = registry(1);
        let first = device("device-1");
        let second = device("device-2");
        let token = acquire(
            &mut registry,
            &first,
            "pane-1",
            "lease-1",
            100,
            AcquireMode::IfVacant,
        );
        let result = registry.acquire(
            &second,
            id(PaneId::new("pane-1")),
            id(LeaseId::new("lease-2")),
            101,
            1_000,
            AcquireMode::IfVacant,
        );
        assert!(matches!(
            result,
            Err(ControlError::PaneAlreadyControlled { .. })
        ));
        assert_eq!(registry.current(token.pane_id(), 101), Some(token));
    }

    #[test]
    fn different_panes_can_be_controlled_concurrently() {
        let mut registry = registry(1);
        let first = device("device-1");
        let second = device("device-2");
        acquire(
            &mut registry,
            &first,
            "pane-1",
            "lease-1",
            100,
            AcquireMode::IfVacant,
        );
        acquire(
            &mut registry,
            &second,
            "pane-2",
            "lease-2",
            100,
            AcquireMode::IfVacant,
        );
        assert_eq!(registry.active_count(), 2);
    }

    #[test]
    fn takeover_invalidates_old_input_and_delayed_release() {
        let mut registry = registry(1);
        let first = device("device-1");
        let second = device("device-2");
        let old = acquire(
            &mut registry,
            &first,
            "pane-1",
            "lease-1",
            100,
            AcquireMode::IfVacant,
        );
        let current = acquire(
            &mut registry,
            &second,
            "pane-1",
            "lease-2",
            101,
            AcquireMode::Takeover,
        );
        assert_eq!(old.epoch(), 1);
        assert_eq!(current.epoch(), 2);
        assert!(matches!(
            registry.authorize_input(&old, 102),
            Err(ControlError::StaleCapability)
        ));
        assert!(matches!(
            registry.release(&old, 102),
            Err(ControlError::StaleCapability)
        ));
        assert_eq!(registry.current(current.pane_id(), 102), Some(current));
    }

    #[test]
    fn heartbeat_replaces_the_old_expiry_capability_without_changing_epoch() {
        let mut registry = registry(1);
        let owner_device = device("device-1");
        let old = acquire(
            &mut registry,
            &owner_device,
            "pane-1",
            "lease-1",
            100,
            AcquireMode::IfVacant,
        );
        let current = match registry.heartbeat(&old, 500, 2_000) {
            Ok(token) => token,
            Err(error) => panic!("unexpected heartbeat error: {error}"),
        };
        assert_eq!(current.epoch(), old.epoch());
        assert_eq!(current.expires_at_unix_ms(), 2_500);
        assert!(matches!(
            registry.authorize_input(&old, 501),
            Err(ControlError::StaleCapability)
        ));
        assert!(registry.authorize_input(&current, 501).is_ok());
    }

    #[test]
    fn expiry_releases_the_pane_but_epoch_remains_monotonic() {
        let mut registry = registry(1);
        let owner_device = device("device-1");
        let old = acquire(
            &mut registry,
            &owner_device,
            "pane-1",
            "lease-1",
            100,
            AcquireMode::IfVacant,
        );
        assert!(matches!(
            registry.authorize_input(&old, 1_100),
            Err(ControlError::LeaseExpired)
        ));
        let current = acquire(
            &mut registry,
            &owner_device,
            "pane-1",
            "lease-2",
            1_100,
            AcquireMode::IfVacant,
        );
        assert_eq!(current.epoch(), 2);
    }

    #[test]
    fn daemon_restart_rejects_a_numerically_similar_old_token() {
        let owner_device = device("device-1");
        let mut old_registry = registry(1);
        let old = acquire(
            &mut old_registry,
            &owner_device,
            "pane-1",
            "lease-1",
            100,
            AcquireMode::IfVacant,
        );

        let mut new_registry = registry(2);
        let current = acquire(
            &mut new_registry,
            &owner_device,
            "pane-1",
            "lease-1",
            100,
            AcquireMode::IfVacant,
        );
        assert_eq!(old.epoch(), current.epoch());
        assert_eq!(old.lease_id(), current.lease_id());
        assert!(matches!(
            new_registry.authorize_input(&old, 101),
            Err(ControlError::GenerationMismatch { .. })
        ));
        assert!(new_registry.authorize_input(&current, 101).is_ok());
    }

    #[test]
    fn foreign_owner_and_duplicate_active_lease_ids_fail_closed() {
        let mut registry = registry(1);
        let foreign = foreign_device("foreign-device");
        assert!(matches!(
            registry.acquire(
                &foreign,
                id(PaneId::new("pane-1")),
                id(LeaseId::new("lease-1")),
                0,
                1_000,
                AcquireMode::IfVacant,
            ),
            Err(ControlError::ForeignOwner { .. })
        ));

        let owner_device = device("device-1");
        acquire(
            &mut registry,
            &owner_device,
            "pane-1",
            "lease-1",
            0,
            AcquireMode::IfVacant,
        );
        assert!(matches!(
            registry.acquire(
                &owner_device,
                id(PaneId::new("pane-2")),
                id(LeaseId::new("lease-1")),
                0,
                1_000,
                AcquireMode::IfVacant,
            ),
            Err(ControlError::DuplicateActiveLeaseId(_))
        ));
    }

    #[test]
    fn failed_takeover_preserves_the_existing_controller() {
        let mut registry = registry(1);
        let first = device("device-1");
        let second = device("device-2");
        let current = acquire(
            &mut registry,
            &first,
            "pane-1",
            "lease-1",
            100,
            AcquireMode::IfVacant,
        );
        registry
            .last_epoch_by_pane
            .insert(current.pane_id().clone(), u64::MAX);
        let result = registry.acquire(
            &second,
            current.pane_id().clone(),
            id(LeaseId::new("lease-2")),
            101,
            1_000,
            AcquireMode::Takeover,
        );
        assert!(matches!(result, Err(ControlError::EpochExhausted(_))));
        assert_eq!(registry.current(current.pane_id(), 102), Some(current));
    }

    #[test]
    fn registry_capacity_and_ttl_are_bounded() {
        let mut registry = match PaneControllerRegistry::new(owner(), generation(1), 1_000, 1) {
            Ok(value) => value,
            Err(error) => panic!("invalid test registry: {error}"),
        };
        let owner_device = device("device-1");
        acquire(
            &mut registry,
            &owner_device,
            "pane-1",
            "lease-1",
            0,
            AcquireMode::IfVacant,
        );
        assert!(matches!(
            registry.acquire(
                &owner_device,
                id(PaneId::new("pane-2")),
                id(LeaseId::new("lease-2")),
                0,
                1_000,
                AcquireMode::IfVacant,
            ),
            Err(ControlError::TooManyControlledPanes { .. })
        ));
        let active = match registry.current(&id(PaneId::new("pane-1")), 1) {
            Some(token) => token,
            None => panic!("missing active token"),
        };
        assert!(matches!(
            registry.heartbeat(&active, 1, 1_001),
            Err(ControlError::InvalidTtl { .. })
        ));
    }
}
