//! Fixed internal Core transport identity.
//!
//! The active value is selected by the compiled binary profile. Protocol
//! messages, environment values, CLI arguments, and user configuration cannot
//! construct or select a transport profile.

pub(crate) const PRODUCTION_PRINCIPAL_TYPE: &str = "browser_bridge";
pub(crate) const PRODUCTION_HANDLER_PATH: &str = "plugins/_a0_connector/ws_connector";
pub(crate) const PRODUCTION_HANDLER_ID: &str = "ws_connector.WsConnector";
pub(crate) const DEVELOPMENT_PRINCIPAL_TYPE: &str = "browser_bridge_development";
pub(crate) const DEVELOPMENT_HANDLER_PATH: &str = "plugins/_a0_connector/ws_browser_development";
pub(crate) const DEVELOPMENT_HANDLER_ID: &str = "ws_browser_development.WsBrowserDevelopment";
pub(crate) const NAMESPACE: &str = "/ws";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ProfileKind {
    Production,
    LocalDevelopment,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct BrowserTransportProfile(ProfileKind);

impl BrowserTransportProfile {
    #[cfg(not(feature = "local-development"))]
    pub(crate) const fn compiled() -> Self {
        Self(ProfileKind::Production)
    }

    #[cfg(feature = "local-development")]
    pub(crate) const fn compiled() -> Self {
        Self(ProfileKind::LocalDevelopment)
    }

    pub(crate) const fn principal_type(self) -> &'static str {
        match self.0 {
            ProfileKind::Production => PRODUCTION_PRINCIPAL_TYPE,
            ProfileKind::LocalDevelopment => DEVELOPMENT_PRINCIPAL_TYPE,
        }
    }

    pub(crate) const fn handler_path(self) -> &'static str {
        match self.0 {
            ProfileKind::Production => PRODUCTION_HANDLER_PATH,
            ProfileKind::LocalDevelopment => DEVELOPMENT_HANDLER_PATH,
        }
    }

    pub(crate) const fn handler_id(self) -> &'static str {
        match self.0 {
            ProfileKind::Production => PRODUCTION_HANDLER_ID,
            ProfileKind::LocalDevelopment => DEVELOPMENT_HANDLER_ID,
        }
    }

    pub(crate) const fn namespace(self) -> &'static str {
        NAMESPACE
    }

    /// Context transport is intentionally limited to the production profile.
    /// A future development runtime admission must not implicitly inherit it.
    pub(crate) const fn permits_context(self) -> bool {
        matches!(self.0, ProfileKind::Production)
    }

    /// The development profile is a frozen, useful subset. Keeping this gate
    /// on the private compile-selected profile prevents a valid codec from
    /// accidentally forwarding a locally implemented but unadmitted action.
    pub(crate) fn permits_browser_action(self, action: &str) -> bool {
        match self.0 {
            ProfileKind::Production => true,
            ProfileKind::LocalDevelopment => matches!(
                action,
                "content" | "ensure" | "list" | "navigate" | "open" | "scroll" | "state" | "status"
            ),
        }
    }

    pub(crate) fn permits_browser_capability(self, capability: &str) -> bool {
        match self.0 {
            ProfileKind::Production => true,
            ProfileKind::LocalDevelopment => {
                self.permits_browser_action(capability)
                    || matches!(
                        capability,
                        "cursor_v1" | "semantic_dom_v1" | "tab_groups_v1" | "tab_leases_v1"
                    )
            }
        }
    }

    pub(crate) const fn is_limited_development(self) -> bool {
        matches!(self.0, ProfileKind::LocalDevelopment)
    }

    /// Only navigation/site challenges are part of limited development
    /// control. Consequential click/type approval remains production-only.
    pub(crate) const fn permits_action_challenge(self) -> bool {
        matches!(self.0, ProfileKind::Production)
    }

    #[cfg(test)]
    pub(crate) const fn fixture_production() -> Self {
        Self(ProfileKind::Production)
    }

    #[cfg(test)]
    pub(crate) const fn fixture_development() -> Self {
        Self(ProfileKind::LocalDevelopment)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transport_profiles_are_two_exact_fixed_identities() {
        let production = BrowserTransportProfile::fixture_production();
        assert_eq!(production.principal_type(), PRODUCTION_PRINCIPAL_TYPE);
        assert_eq!(production.handler_path(), PRODUCTION_HANDLER_PATH);
        assert_eq!(production.handler_id(), PRODUCTION_HANDLER_ID);
        assert_eq!(production.namespace(), NAMESPACE);
        assert!(production.permits_context());
        assert!(production.permits_browser_action("type"));
        assert!(production.permits_browser_capability("artifacts_v1"));
        assert!(production.permits_action_challenge());

        let development = BrowserTransportProfile::fixture_development();
        assert_eq!(development.principal_type(), DEVELOPMENT_PRINCIPAL_TYPE);
        assert_eq!(development.handler_path(), DEVELOPMENT_HANDLER_PATH);
        assert_eq!(development.handler_id(), DEVELOPMENT_HANDLER_ID);
        assert_eq!(development.namespace(), NAMESPACE);
        assert!(!development.permits_context());
        assert!(development.permits_browser_action("navigate"));
        assert!(!development.permits_browser_action("click"));
        assert!(development.permits_browser_capability("semantic_dom_v1"));
        assert!(!development.permits_browser_capability("trusted_input_v1"));
        assert!(!development.permits_action_challenge());
        assert_ne!(production, development);

        #[cfg(not(feature = "local-development"))]
        assert_eq!(BrowserTransportProfile::compiled(), production);
        #[cfg(feature = "local-development")]
        assert_eq!(BrowserTransportProfile::compiled(), development);
    }
}
