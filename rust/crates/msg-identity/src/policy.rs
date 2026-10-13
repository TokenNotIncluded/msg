//! Pure mode/scope/ceiling rules. None of these alone authorizes an operation.
use crate::{models::*, require, store::AuthorityStore};
use msg_core::{Error, Json, Result};
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};

pub const CERTGATE: u16 = 0o4000;
pub const SETGID: u16 = 0o2000;
pub const STICKY: u16 = 0o1000;

pub fn parse_mode(value: &str) -> Result<u16> {
    require(
        value.len() == 4 && value.bytes().all(|b| (b'0'..=b'7').contains(&b)),
        "invalid_mode",
    )?;
    u16::from_str_radix(value, 8).map_err(|_| Error("invalid_mode"))
}
pub fn class_bits(mode: u16, class: &str) -> Result<u16> {
    require(mode <= 0o7777, "invalid_mode")?;
    let shift = match class {
        "owner" => 6,
        "group" => 3,
        "other" => 0,
        _ => return Err(Error("invalid_permission_class")),
    };
    Ok((mode >> shift) & 7)
}
pub fn allows(
    resource: &ResourceAuthority,
    subject: Option<&str>,
    memberships: &BTreeSet<String>,
    permission: &str,
) -> Result<bool> {
    let class = if subject == Some(&resource.owner) {
        "owner"
    } else if subject.is_some() && memberships.contains(&resource.group) {
        "group"
    } else {
        "other"
    };
    let mask = match permission {
        "read" | "list" => 4,
        "write" => 2,
        "execute" | "traverse" => 1,
        _ => return Err(Error("invalid_permission")),
    };
    Ok(class_bits(parse_mode(&resource.mode)?, class)? & mask == mask)
}
pub fn scope_contains(scope: &Scope, id: &str, store: &dyn AuthorityStore) -> Result<bool> {
    if scope.resource_id == id {
        return Ok(true);
    }
    if !scope.descendants {
        return Ok(false);
    }
    Ok(store
        .ancestors(id)?
        .iter()
        .any(|r| r.id == scope.resource_id))
}
pub fn scope_subset(child: &Scope, parent: &Scope, store: &dyn AuthorityStore) -> Result<bool> {
    if child.resource_id == parent.resource_id {
        return Ok(!child.descendants || parent.descendants);
    }
    Ok(parent.descendants && scope_contains(parent, &child.resource_id, store)?)
}
pub fn grant_covers(
    grant: &Grant,
    capability: &str,
    operation: &str,
    id: &str,
    store: &dyn AuthorityStore,
) -> Result<bool> {
    Ok(grant.capability == capability
        && grant.version == 1
        && grant.operations.contains(operation)
        && scope_contains(&grant.scope, id, store)?)
}
pub fn constraints_subset(
    child: &BTreeMap<String, Json>,
    parent: &BTreeMap<String, Json>,
) -> Result<bool> {
    for (name, value) in parent {
        let Some(proposed) = child.get(name) else {
            return Ok(false);
        };
        match name.as_str() {
            "hosts" | "methods" | "ports" | "schemes" => {
                let allowed = value.as_array()?;
                if !proposed
                    .as_array()?
                    .iter()
                    .all(|v| allowed.iter().any(|a| python_equal(a, v)))
                {
                    return Ok(false);
                }
            }
            "max_response_bytes" | "timeout_ms" | "max_redirects" => {
                let Ok(p) = proposed.as_integer() else {
                    return Ok(false);
                };
                let q = integral_number(value).ok_or(Error("schema_validation"))?;
                if compare_integers(p, &q).is_gt() {
                    return Ok(false);
                }
            }
            _ if !python_equal(proposed, value) => return Ok(false),
            _ => (),
        }
    }
    Ok(true)
}
pub fn grant_subset(child: &Grant, parent: &Grant, store: &dyn AuthorityStore) -> Result<bool> {
    Ok(child.capability == parent.capability
        && child.version == parent.version
        && child.operations.is_subset(&parent.operations)
        && scope_subset(&child.scope, &parent.scope, store)?
        && constraints_subset(&child.constraints, &parent.constraints)?)
}

/// Trusted registry projection, NOT request data or a certificate-supplied schema.
#[derive(Clone, Deserialize)]
pub struct CapabilityPolicy {
    pub name: String,
    pub version: u32,
    pub scope_types: BTreeSet<String>,
    pub operations: BTreeSet<String>,
    pub ca_only: bool,
    pub constraints_schema: Option<ResourceRef>,
}
pub struct Registry {
    capabilities: BTreeMap<(String, u32), CapabilityPolicy>,
}
impl Registry {
    pub fn new(specs: Vec<CapabilityPolicy>) -> Result<Self> {
        let mut capabilities = BTreeMap::new();
        for spec in specs {
            require(
                capabilities
                    .insert((spec.name.clone(), spec.version), spec)
                    .is_none(),
                "duplicate_capability",
            )?;
        }
        Ok(Self { capabilities })
    }
    pub fn capability(&self, name: &str, version: u32) -> Result<&CapabilityPolicy> {
        self.capabilities
            .get(&(name.to_owned(), version))
            .ok_or(Error("unknown_capability"))
    }
    pub fn validate_grant(&self, grant: &Grant, store: &dyn AuthorityStore) -> Result<()> {
        let spec = self.capability(&grant.capability, grant.version)?;
        let resource = store.resource(&grant.scope.resource_id)?;
        require(
            spec.scope_types.contains(&resource.kind),
            "invalid_scope_type",
        )?;
        require(
            !grant.operations.is_empty() && grant.operations.is_subset(&spec.operations),
            "invalid_grant_operations",
        )?;
        match &spec.constraints_schema {
            None => require(grant.constraints.is_empty(), "unknown_grant_constraint"),
            Some(schema) if schema.id == "schema:network-constraints" => {
                validate_network(&grant.constraints)
            }
            _ => Err(Error("unsupported_constraints_schema")),
        }
    }
}
// Compare exact integer values, never round a large integer through f64.
fn compare_integers(left: &str, right: &str) -> std::cmp::Ordering {
    let (ln, rn) = (left.starts_with('-'), right.starts_with('-'));
    if ln != rn {
        return rn.cmp(&ln);
    }
    let (left, right) = (left.trim_start_matches('-'), right.trim_start_matches('-'));
    let order = left.len().cmp(&right.len()).then_with(|| left.cmp(right));
    if ln {
        order.reverse()
    } else {
        order
    }
}
fn integral_number(value: &Json) -> Option<String> {
    if let Ok(integer) = value.as_integer() {
        return Some(integer.to_owned());
    }
    // JSON Schema considers 1.0 an integer; Python's pure subset rule still
    // requires a literal int for the proposed upper bound, as checked above.
    let text = value.canonical().ok()?;
    let float: f64 = text.parse().ok()?;
    if !float.is_finite() || float.fract() != 0.0 {
        return None;
    }
    Some(if float == 0.0 {
        "0".to_owned()
    } else {
        format!("{float:.0}")
    })
}
fn python_equal(left: &Json, right: &Json) -> bool {
    if left == right {
        return true;
    }
    if let (Some(left), Some(right)) = (integral_number(left), integral_number(right)) {
        return left == right;
    }
    false
}
fn validate_network(values: &BTreeMap<String, Json>) -> Result<()> {
    for (key, value) in values {
        let valid = match key.as_str() {
            "hosts" | "methods" | "schemes" => value.as_array().is_ok_and(|values| {
                values.iter().all(|v| {
                    v.as_str().is_ok_and(|v| match key.as_str() {
                        "schemes" => matches!(v, "http" | "https"),
                        "methods" => matches!(
                            v,
                            "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE" | "OPTIONS"
                        ),
                        _ => true,
                    })
                })
            }),
            "ports" => value.as_array().is_ok_and(|values| {
                values.iter().all(|v| {
                    integral_number(v)
                        .and_then(|v| v.parse::<u16>().ok())
                        .is_some_and(|v| v > 0)
                })
            }),
            "max_redirects" | "timeout_ms" | "max_response_bytes" => integral_number(value)
                .is_some_and(|v| !v.starts_with('-') && (key == "max_redirects" || v != "0")),
            _ => false,
        };
        require(valid, "schema_validation")?;
    }
    Ok(())
}
