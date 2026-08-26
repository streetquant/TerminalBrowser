use std::error::Error;
use std::fmt;

const MAX_ID_BYTES: usize = 256;

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum IdError {
    Empty {
        kind: &'static str,
    },
    SurroundingWhitespace {
        kind: &'static str,
    },
    ControlCharacter {
        kind: &'static str,
    },
    TooLong {
        kind: &'static str,
        actual_bytes: usize,
        max_bytes: usize,
    },
}

impl fmt::Display for IdError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty { kind } => write!(formatter, "{kind} must not be empty"),
            Self::SurroundingWhitespace { kind } => {
                write!(formatter, "{kind} must not contain surrounding whitespace")
            }
            Self::ControlCharacter { kind } => {
                write!(formatter, "{kind} must not contain control characters")
            }
            Self::TooLong {
                kind,
                actual_bytes,
                max_bytes,
            } => write!(
                formatter,
                "{kind} is {actual_bytes} bytes; maximum is {max_bytes} bytes"
            ),
        }
    }
}

impl Error for IdError {}

fn validate_id(kind: &'static str, value: &str) -> Result<(), IdError> {
    if value.is_empty() {
        return Err(IdError::Empty { kind });
    }
    if value.chars().any(char::is_control) {
        return Err(IdError::ControlCharacter { kind });
    }
    if value.trim() != value {
        return Err(IdError::SurroundingWhitespace { kind });
    }
    if value.len() > MAX_ID_BYTES {
        return Err(IdError::TooLong {
            kind,
            actual_bytes: value.len(),
            max_bytes: MAX_ID_BYTES,
        });
    }
    Ok(())
}

macro_rules! define_id {
    ($name:ident, $kind:literal) => {
        #[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
        pub struct $name(String);

        impl $name {
            pub fn new(value: impl Into<String>) -> Result<Self, IdError> {
                let value = value.into();
                validate_id($kind, &value)?;
                Ok(Self(value))
            }

            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str(&self.0)
            }
        }
    };
}

define_id!(HostId, "host id");
define_id!(OwnerId, "owner id");
define_id!(DeviceId, "device id");
define_id!(WindowId, "window id");
define_id!(PaneId, "pane id");
define_id!(SessionId, "session id");
define_id!(LeaseId, "lease id");

#[cfg(test)]
mod tests {
    use super::{DeviceId, IdError, LeaseId};

    #[test]
    fn opaque_ids_reject_ambiguous_or_unbounded_values() {
        assert!(matches!(DeviceId::new(""), Err(IdError::Empty { .. })));
        assert!(matches!(
            DeviceId::new(" device"),
            Err(IdError::SurroundingWhitespace { .. })
        ));
        assert!(matches!(
            DeviceId::new("device\n"),
            Err(IdError::ControlCharacter { .. })
        ));
        assert!(matches!(
            LeaseId::new("x".repeat(257)),
            Err(IdError::TooLong { .. })
        ));
    }

    #[test]
    fn opaque_ids_preserve_unicode_as_data() {
        let result = DeviceId::new("owner-device-日本");
        match result {
            Ok(value) => assert_eq!(value.as_str(), "owner-device-日本"),
            Err(error) => panic!("unexpected id error: {error}"),
        }
    }
}
