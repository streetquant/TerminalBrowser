use crate::WindowId;
use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

pub const INTERNAL_ROUTE_PREFIX: &str = "_termdeck";
pub const MAX_SLUG_BYTES: usize = 64;
pub const MAX_WINDOW_NAME_BYTES: usize = 1_024;
pub const MAX_ROUTED_WINDOWS: usize = 4_096;
pub const MAX_RETIRED_SLUGS: usize = 16_384;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WindowRouteInput {
    pub id: WindowId,
    pub name: String,
}

impl WindowRouteInput {
    #[must_use]
    pub fn new(id: WindowId, name: impl Into<String>) -> Self {
        Self {
            id,
            name: name.into(),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WindowRoute {
    id: WindowId,
    slug: String,
}

impl WindowRoute {
    pub fn restored(id: WindowId, slug: impl Into<String>) -> Result<Self, RouteError> {
        let slug = slug.into();
        validate_slug(&slug)?;
        Ok(Self { id, slug })
    }

    #[must_use]
    pub fn id(&self) -> &WindowId {
        &self.id
    }

    #[must_use]
    pub fn slug(&self) -> &str {
        &self.slug
    }

    #[must_use]
    pub fn path(&self) -> String {
        format!("/{}", self.slug)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RouteTarget {
    Home,
    Internal,
    Window(WindowId),
    NotFound,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum RouteError {
    TooManyWindows {
        actual: usize,
        maximum: usize,
    },
    TooManyRetiredSlugs {
        actual: usize,
        maximum: usize,
    },
    NameTooLong {
        window_id: WindowId,
        actual_bytes: usize,
        maximum_bytes: usize,
    },
    DuplicateWindowId(WindowId),
    DuplicateSlug(String),
    ActiveSlugIsRetired(String),
    InvalidSlug(String),
    SlugSpaceExhausted(String),
}

impl fmt::Display for RouteError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooManyWindows { actual, maximum } => {
                write!(
                    formatter,
                    "{actual} routed windows exceeds maximum {maximum}"
                )
            }
            Self::TooManyRetiredSlugs { actual, maximum } => {
                write!(
                    formatter,
                    "{actual} retired slugs exceeds maximum {maximum}"
                )
            }
            Self::NameTooLong {
                window_id,
                actual_bytes,
                maximum_bytes,
            } => write!(
                formatter,
                "window {window_id} name is {actual_bytes} bytes; maximum is {maximum_bytes}"
            ),
            Self::DuplicateWindowId(window_id) => {
                write!(formatter, "duplicate window id {window_id}")
            }
            Self::DuplicateSlug(slug) => write!(formatter, "duplicate window slug {slug}"),
            Self::ActiveSlugIsRetired(slug) => {
                write!(formatter, "active window slug {slug} is also retired")
            }
            Self::InvalidSlug(slug) => write!(formatter, "invalid window slug {slug}"),
            Self::SlugSpaceExhausted(base) => {
                write!(formatter, "slug allocation space exhausted for {base}")
            }
        }
    }
}

impl Error for RouteError {}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct RouteRegistry {
    assignments: BTreeMap<WindowId, String>,
    owners: BTreeMap<String, WindowId>,
    retired: BTreeSet<String>,
}

impl RouteRegistry {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn restore(
        routes: impl IntoIterator<Item = WindowRoute>,
        retired: impl IntoIterator<Item = String>,
    ) -> Result<Self, RouteError> {
        let mut registry = Self::new();
        for slug in retired {
            validate_slug(&slug)?;
            if !registry.retired.insert(slug.clone()) {
                return Err(RouteError::DuplicateSlug(slug));
            }
            if registry.retired.len() > MAX_RETIRED_SLUGS {
                return Err(RouteError::TooManyRetiredSlugs {
                    actual: registry.retired.len(),
                    maximum: MAX_RETIRED_SLUGS,
                });
            }
        }
        for route in routes {
            if registry.assignments.len() >= MAX_ROUTED_WINDOWS {
                return Err(RouteError::TooManyWindows {
                    actual: registry.assignments.len() + 1,
                    maximum: MAX_ROUTED_WINDOWS,
                });
            }
            validate_slug(route.slug())?;
            if registry.retired.contains(route.slug()) {
                return Err(RouteError::ActiveSlugIsRetired(route.slug().to_owned()));
            }
            if registry.assignments.contains_key(route.id()) {
                return Err(RouteError::DuplicateWindowId(route.id().clone()));
            }
            if registry.owners.contains_key(route.slug()) {
                return Err(RouteError::DuplicateSlug(route.slug().to_owned()));
            }
            registry
                .assignments
                .insert(route.id().clone(), route.slug().to_owned());
            registry
                .owners
                .insert(route.slug().to_owned(), route.id().clone());
        }
        Ok(registry)
    }

    pub fn reconcile(
        &mut self,
        windows: &[WindowRouteInput],
    ) -> Result<Vec<WindowRoute>, RouteError> {
        let mut candidate = self.clone();
        let routes = candidate.reconcile_in_place(windows)?;
        *self = candidate;
        Ok(routes)
    }

    fn reconcile_in_place(
        &mut self,
        windows: &[WindowRouteInput],
    ) -> Result<Vec<WindowRoute>, RouteError> {
        if windows.len() > MAX_ROUTED_WINDOWS {
            return Err(RouteError::TooManyWindows {
                actual: windows.len(),
                maximum: MAX_ROUTED_WINDOWS,
            });
        }

        let mut current_ids = BTreeSet::new();
        for window in windows {
            if window.name.len() > MAX_WINDOW_NAME_BYTES {
                return Err(RouteError::NameTooLong {
                    window_id: window.id.clone(),
                    actual_bytes: window.name.len(),
                    maximum_bytes: MAX_WINDOW_NAME_BYTES,
                });
            }
            if !current_ids.insert(window.id.clone()) {
                return Err(RouteError::DuplicateWindowId(window.id.clone()));
            }
        }

        let removed: Vec<WindowId> = self
            .assignments
            .keys()
            .filter(|window_id| !current_ids.contains(*window_id))
            .cloned()
            .collect();
        for window_id in removed {
            if let Some(slug) = self.assignments.remove(&window_id) {
                self.owners.remove(&slug);
                self.retired.insert(slug);
            }
        }
        if self.retired.len() > MAX_RETIRED_SLUGS {
            return Err(RouteError::TooManyRetiredSlugs {
                actual: self.retired.len(),
                maximum: MAX_RETIRED_SLUGS,
            });
        }

        let names_by_id: BTreeMap<&WindowId, &str> = windows
            .iter()
            .map(|window| (&window.id, window.name.as_str()))
            .collect();
        for (window_id, name) in names_by_id {
            if self.assignments.contains_key(window_id) {
                continue;
            }
            let base = slug_base(name);
            let slug = self.allocate_slug(&base)?;
            self.assignments.insert(window_id.clone(), slug.clone());
            self.owners.insert(slug, window_id.clone());
        }

        let mut routes = Vec::with_capacity(windows.len());
        for window in windows {
            let Some(slug) = self.assignments.get(&window.id) else {
                return Err(RouteError::SlugSpaceExhausted(slug_base(&window.name)));
            };
            routes.push(WindowRoute {
                id: window.id.clone(),
                slug: slug.clone(),
            });
        }
        Ok(routes)
    }

    #[must_use]
    pub fn resolve(&self, path: &str) -> RouteTarget {
        if path == "/" {
            return RouteTarget::Home;
        }
        if path == "/_termdeck" || path.starts_with("/_termdeck/") {
            return RouteTarget::Internal;
        }
        let Some(slug) = path.strip_prefix('/') else {
            return RouteTarget::NotFound;
        };
        if slug.is_empty() || slug.contains('/') {
            return RouteTarget::NotFound;
        }
        self.owners
            .get(slug)
            .cloned()
            .map_or(RouteTarget::NotFound, RouteTarget::Window)
    }

    #[must_use]
    pub fn slug_for(&self, window_id: &WindowId) -> Option<&str> {
        self.assignments.get(window_id).map(String::as_str)
    }

    pub fn retired_slugs(&self) -> impl Iterator<Item = &str> {
        self.retired.iter().map(String::as_str)
    }

    fn allocate_slug(&self, base: &str) -> Result<String, RouteError> {
        for ordinal in 1..=MAX_RETIRED_SLUGS + MAX_ROUTED_WINDOWS + 1 {
            let suffix = if ordinal == 1 {
                String::new()
            } else {
                format!("-{ordinal}")
            };
            let available = MAX_SLUG_BYTES.saturating_sub(suffix.len());
            let mut prefix = base[..base.len().min(available)].trim_end_matches('-');
            if prefix.is_empty() {
                prefix = "w";
            }
            let candidate = format!("{prefix}{suffix}");
            if !self.owners.contains_key(&candidate) && !self.retired.contains(&candidate) {
                return Ok(candidate);
            }
        }
        Err(RouteError::SlugSpaceExhausted(base.to_owned()))
    }
}

fn validate_slug(slug: &str) -> Result<(), RouteError> {
    if slug.is_empty()
        || slug.len() > MAX_SLUG_BYTES
        || slug.starts_with('-')
        || slug.ends_with('-')
        || slug.contains("--")
        || !slug
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
        || slug == INTERNAL_ROUTE_PREFIX
    {
        return Err(RouteError::InvalidSlug(slug.to_owned()));
    }
    Ok(())
}

#[must_use]
pub fn slug_base(name: &str) -> String {
    let mut output = String::with_capacity(name.len().min(MAX_SLUG_BYTES));
    let mut separator_pending = false;
    for character in name.chars() {
        if character.is_ascii_alphanumeric() {
            if separator_pending && !output.is_empty() {
                output.push('-');
            }
            separator_pending = false;
            output.push(character.to_ascii_lowercase());
        } else if character.is_ascii() {
            separator_pending = !output.is_empty();
        } else {
            if separator_pending && !output.is_empty() {
                output.push('-');
            }
            if !output.is_empty() && !output.ends_with('-') {
                output.push('-');
            }
            output.push('u');
            output.push_str(&format!("{:x}", u32::from(character)));
            separator_pending = true;
        }
        if output.len() >= MAX_SLUG_BYTES {
            break;
        }
    }
    output.truncate(output.len().min(MAX_SLUG_BYTES));
    while output.ends_with('-') {
        output.pop();
    }
    if output.is_empty() {
        output.push_str("window");
    }
    output
}

#[cfg(test)]
mod tests {
    use super::{
        MAX_RETIRED_SLUGS, MAX_SLUG_BYTES, RouteError, RouteRegistry, RouteTarget, WindowRoute,
        WindowRouteInput, slug_base,
    };
    use crate::WindowId;

    fn window_id(value: &str) -> WindowId {
        match WindowId::new(value) {
            Ok(window_id) => window_id,
            Err(error) => panic!("invalid test window id: {error}"),
        }
    }

    fn route_input(id: &str, name: &str) -> WindowRouteInput {
        WindowRouteInput::new(window_id(id), name)
    }

    fn reconcile(registry: &mut RouteRegistry, windows: &[WindowRouteInput]) -> Vec<WindowRoute> {
        match registry.reconcile(windows) {
            Ok(routes) => routes,
            Err(error) => panic!("unexpected route error: {error}"),
        }
    }

    #[test]
    fn simple_window_name_has_direct_top_level_url() {
        let mut registry = RouteRegistry::new();
        let routes = reconcile(&mut registry, &[route_input("window-1", "Random")]);
        assert_eq!(routes[0].slug(), "random");
        assert_eq!(routes[0].path(), "/random");
        assert_eq!(
            registry.resolve("/random"),
            RouteTarget::Window(window_id("window-1"))
        );
    }

    #[test]
    fn duplicate_names_are_allocated_by_stable_window_id_not_input_order() {
        let left = vec![
            route_input("window-b", "Random"),
            route_input("window-a", "Random"),
        ];
        let right = vec![
            route_input("window-a", "Random"),
            route_input("window-b", "Random"),
        ];
        let mut first = RouteRegistry::new();
        let mut second = RouteRegistry::new();
        reconcile(&mut first, &left);
        reconcile(&mut second, &right);
        assert_eq!(first.slug_for(&window_id("window-a")), Some("random"));
        assert_eq!(first.slug_for(&window_id("window-b")), Some("random-2"));
        assert_eq!(first, second);
    }

    #[test]
    fn existing_route_survives_reorder_and_window_rename() {
        let mut registry = RouteRegistry::new();
        reconcile(
            &mut registry,
            &[
                route_input("window-a", "Random"),
                route_input("window-b", "Build"),
            ],
        );
        reconcile(
            &mut registry,
            &[
                route_input("window-b", "Build Renamed"),
                route_input("window-a", "Random Renamed"),
            ],
        );
        assert_eq!(registry.slug_for(&window_id("window-a")), Some("random"));
        assert_eq!(registry.slug_for(&window_id("window-b")), Some("build"));
    }

    #[test]
    fn removed_route_is_retired_and_never_silently_reassigned() {
        let mut registry = RouteRegistry::new();
        reconcile(&mut registry, &[route_input("window-a", "Random")]);
        reconcile(&mut registry, &[]);
        reconcile(&mut registry, &[route_input("window-b", "Random")]);
        assert_eq!(registry.slug_for(&window_id("window-b")), Some("random-2"));
        assert_eq!(registry.resolve("/random"), RouteTarget::NotFound);
        assert!(registry.retired_slugs().any(|slug| slug == "random"));
    }

    #[test]
    fn unicode_and_empty_names_have_ascii_bounded_routes() {
        assert_eq!(slug_base("日本 Window"), "u65e5-u672c-window");
        assert_eq!(slug_base("!!!"), "window");
        let long = slug_base(&"A".repeat(1_024));
        assert_eq!(long.len(), MAX_SLUG_BYTES);
        assert!(long.bytes().all(|byte| byte == b'a'));
    }

    #[test]
    fn internal_routes_cannot_be_shadowed_by_window_names() {
        let mut registry = RouteRegistry::new();
        reconcile(&mut registry, &[route_input("window-a", "_termdeck")]);
        assert_eq!(registry.resolve("/_termdeck"), RouteTarget::Internal);
        assert_eq!(registry.resolve("/_termdeck/api/v1"), RouteTarget::Internal);
        assert_eq!(registry.slug_for(&window_id("window-a")), Some("termdeck"));
    }

    #[test]
    fn restore_rejects_collisions_and_active_retired_overlap() {
        let first = match WindowRoute::restored(window_id("window-a"), "random") {
            Ok(route) => route,
            Err(error) => panic!("unexpected route error: {error}"),
        };
        let second = match WindowRoute::restored(window_id("window-b"), "random") {
            Ok(route) => route,
            Err(error) => panic!("unexpected route error: {error}"),
        };
        assert!(matches!(
            RouteRegistry::restore([first.clone(), second], []),
            Err(RouteError::DuplicateSlug(slug)) if slug == "random"
        ));
        assert!(matches!(
            RouteRegistry::restore([first], [String::from("random")]),
            Err(RouteError::ActiveSlugIsRetired(slug)) if slug == "random"
        ));
    }

    #[test]
    fn failed_reconcile_preserves_the_previous_registry() {
        let active = match WindowRoute::restored(window_id("window-a"), "active") {
            Ok(route) => route,
            Err(error) => panic!("unexpected route error: {error}"),
        };
        let retired = (0..MAX_RETIRED_SLUGS).map(|index| format!("retired-{index}"));
        let mut registry = match RouteRegistry::restore([active], retired) {
            Ok(value) => value,
            Err(error) => panic!("unexpected restore error: {error}"),
        };
        let before = registry.clone();
        let result = registry.reconcile(&[]);
        assert!(matches!(
            result,
            Err(RouteError::TooManyRetiredSlugs { .. })
        ));
        assert_eq!(registry, before);
    }

    #[test]
    fn malformed_paths_do_not_resolve() {
        let mut registry = RouteRegistry::new();
        reconcile(&mut registry, &[route_input("window-a", "Random")]);
        assert_eq!(registry.resolve("random"), RouteTarget::NotFound);
        assert_eq!(registry.resolve("/random/extra"), RouteTarget::NotFound);
        assert_eq!(registry.resolve("//random"), RouteTarget::NotFound);
    }
}
