//! Release-owned contracts. No request can register executable code or schemas.
use msg_core::{Error, Json, Result};
use msg_identity::{
    authentication::OperationPolicy,
    authorization::ResourceTypePolicy,
    models::{Grant, ResourceRef},
    policy::{CapabilityPolicy, Registry},
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Deserialize, Serialize)]
pub struct ResourceTypeSpec {
    pub name: String,
    pub version: u32,
    pub container: bool,
    pub taggable: bool,
    pub purchasable: bool,
    pub content_schema: Option<ResourceRef>,
    pub relations: BTreeSet<String>,
    pub operations: BTreeSet<String>,
}

#[derive(Clone, Deserialize)]
pub struct AccessSpec {
    pub argument: String,
    pub check: String,
    pub default: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct OperationSpec {
    pub name: String,
    pub version: u32,
    pub effect: String,
    pub entries: BTreeSet<String>,
    pub require_signature: bool,
    pub anonymous_only: bool,
    pub enabled: bool,
    pub input_schema: ResourceRef,
    pub output_schema: ResourceRef,
    pub requires_rules: Vec<Value>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub deprecated: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub replaced_by: Option<String>,
    #[serde(default, skip_serializing)]
    pub access: Option<AccessSpec>,
    #[serde(skip_serializing)]
    pub handler: String,
    #[serde(skip_serializing)]
    pub requirements: String,
}
impl OperationSpec {
    pub fn id(&self) -> String {
        format!("{}@{}", self.name, self.version)
    }
    pub fn policy(&self) -> OperationPolicy {
        OperationPolicy {
            name: self.name.clone(),
            version: self.version,
            effect: self.effect.clone(),
            entries: self.entries.clone(),
            require_signature: self.require_signature,
            anonymous_only: self.anonymous_only,
        }
    }
    pub fn describe(&self) -> Result<Json> {
        Json::parse(&serde_json::to_string(self).map_err(|_| Error("invalid_registry"))?)
    }
}

#[derive(Deserialize)]
struct Manifest {
    dependencies: Vec<String>,
    operations: Vec<String>,
}
#[derive(Deserialize)]
struct Reference {
    version: u32,
    operations: Vec<OperationSpec>,
    schemas: BTreeMap<String, Value>,
    capabilities: Vec<Value>,
    resource_types: Vec<ResourceTypeSpec>,
    base_grants: Vec<Grant>,
    primary_ceiling: Vec<Grant>,
    temporary_ceiling: Vec<Grant>,
    base_families: BTreeSet<String>,
    plugins: Vec<String>,
    #[serde(default)]
    plugin_manifests: BTreeMap<String, Manifest>,
}

pub struct Catalog {
    operations: BTreeMap<(String, u32), OperationSpec>,
    resource_types: BTreeMap<(String, u32), ResourceTypeSpec>,
    schemas: BTreeMap<String, Value>,
    validators: BTreeMap<String, jsonschema::Validator>,
    capabilities: Vec<CapabilityPolicy>,
    published_capabilities: Vec<Value>,
    pub base_grants: Vec<Grant>,
    pub primary_ceiling: Vec<Grant>,
    pub temporary_ceiling: Vec<Grant>,
    pub base_families: BTreeSet<String>,
}
impl Catalog {
    pub fn builtins() -> Result<Self> {
        Self::load(None)
    }

    pub fn load(enabled: Option<&[String]>) -> Result<Self> {
        let mut reference: Reference =
            serde_json::from_str(include_str!("../../../assets/catalog.json"))
                .map_err(|_| Error("invalid_registry"))?;
        if reference.version != 1 {
            return Err(Error("invalid_registry"));
        }
        if let Some(enabled) = enabled {
            let selected: BTreeSet<_> = enabled.iter().cloned().collect();
            if selected.len() != enabled.len()
                || !selected.contains("identity")
                || !selected.iter().all(|x| reference.plugins.contains(x))
            {
                return Err(Error("invalid_plugin_list"));
            }
            if selected.len() != reference.plugins.len() {
                if reference.plugin_manifests.is_empty() {
                    return Err(Error("invalid_registry"));
                }
                let mut allowed = BTreeSet::new();
                for (name, manifest) in &reference.plugin_manifests {
                    let active = selected.contains(name)
                        || (name == "file"
                            && ["content", "discovery", "batch"]
                                .iter()
                                .all(|p| selected.contains(*p)))
                        || (name == "game" && selected.contains("communication"));
                    if active {
                        if !manifest.dependencies.iter().all(|p| selected.contains(p)) {
                            return Err(Error("missing_plugin_dependency"));
                        }
                        allowed.extend(manifest.operations.iter().cloned());
                    }
                }
                reference.operations.retain(|s| allowed.contains(&s.id()));
                for grants in [
                    &mut reference.base_grants,
                    &mut reference.primary_ceiling,
                    &mut reference.temporary_ceiling,
                ] {
                    for grant in grants.iter_mut() {
                        grant.operations.retain(|op| allowed.contains(op));
                    }
                    grants.retain(|g| !g.operations.is_empty());
                }
            }
        }
        let mut operations = BTreeMap::new();
        for spec in reference.operations {
            if spec.version == 0
                || spec.name.is_empty()
                || spec.name.contains('*')
                || !matches!(spec.effect.as_str(), "read" | "transaction" | "external")
                || spec.entries.is_empty()
                || !spec
                    .entries
                    .iter()
                    .all(|x| matches!(x.as_str(), "network" | "worker" | "local_admin"))
                || (spec.name.starts_with("root.")
                    && spec.entries != BTreeSet::from(["local_admin".to_owned()]))
                || !reference.schemas.contains_key(&spec.input_schema.id)
                || !reference.schemas.contains_key(&spec.output_schema.id)
            {
                return Err(Error("invalid_registry"));
            }
            if operations
                .insert((spec.name.clone(), spec.version), spec)
                .is_some()
            {
                return Err(Error("duplicate_operation"));
            }
        }
        let mut resource_types = BTreeMap::new();
        for spec in reference.resource_types {
            if resource_types
                .insert((spec.name.clone(), spec.version), spec)
                .is_some()
            {
                return Err(Error("duplicate_resource_type"));
            }
        }
        let mut validators = BTreeMap::new();
        for (name, schema) in &reference.schemas {
            local_schema(schema)?;
            let validator = jsonschema::options()
                .with_draft(jsonschema::Draft::Draft202012)
                .should_validate_formats(false)
                .build(schema)
                .map_err(|_| Error("invalid_schema"))?;
            validators.insert(name.clone(), validator);
        }
        let capabilities = reference
            .capabilities
            .iter()
            .cloned()
            .map(serde_json::from_value)
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(|_| Error("invalid_registry"))?;
        Ok(Self {
            operations,
            resource_types,
            schemas: reference.schemas,
            validators,
            capabilities,
            published_capabilities: reference.capabilities,
            base_grants: reference.base_grants,
            primary_ceiling: reference.primary_ceiling,
            temporary_ceiling: reference.temporary_ceiling,
            base_families: reference.base_families,
        })
    }
    pub fn operation(&self, name: &str, version: u32) -> Result<&OperationSpec> {
        self.operations
            .get(&(name.to_owned(), version))
            .ok_or(Error("unknown_operation"))
    }
    pub fn operations(&self) -> impl Iterator<Item = &OperationSpec> {
        self.operations.values()
    }
    pub fn policies(&self) -> Vec<OperationPolicy> {
        self.operations().map(OperationSpec::policy).collect()
    }
    pub fn resource_type(&self, name: &str, version: u32) -> Result<&ResourceTypeSpec> {
        self.resource_types
            .get(&(name.to_owned(), version))
            .ok_or(Error("unknown_resource_type"))
    }
    pub fn resource_types(&self) -> impl Iterator<Item = &ResourceTypeSpec> {
        self.resource_types.values()
    }
    pub fn resource_policies(&self) -> Vec<ResourceTypePolicy> {
        self.resource_types()
            .map(|s| ResourceTypePolicy {
                name: s.name.clone(),
                version: s.version,
                container: s.container,
            })
            .collect()
    }
    pub fn capability_registry(&self) -> Result<Registry> {
        Registry::new(self.capabilities.clone())
    }
    pub fn capabilities(&self) -> Result<Value> {
        Ok(Value::Array(self.published_capabilities.clone()))
    }
    pub fn catalog(&self) -> Result<Value> {
        let operations = self
            .operations()
            .filter(|s| s.entries.contains("network"))
            .map(|s| serde_json::to_value(s).map_err(|_| Error("invalid_registry")))
            .collect::<Result<Vec<_>>>()?;
        let digest = msg_core::digest(
            Json::parse(
                &serde_json::to_string(&operations).map_err(|_| Error("invalid_registry"))?,
            )?
            .canonical()?
            .as_bytes(),
        );
        Ok(serde_json::json!({"version":1,"digest":digest,"operations":operations}))
    }
    pub fn schema(&self, id: &str) -> Result<&Value> {
        self.schemas.get(id).ok_or(Error("schema_not_found"))
    }
    pub fn validate(&self, id: &str, instance: &Json) -> Result<()> {
        let value: Value =
            serde_json::from_str(&instance.canonical()?).map_err(|_| Error("invalid_json"))?;
        if !self
            .validators
            .get(id)
            .ok_or(Error("schema_not_found"))?
            .is_valid(&value)
        {
            return Err(Error("schema_validation"));
        }
        Ok(())
    }
}

// Only schema-bearing keywords are traversed: an instance property named "$ref"
// or text inside examples/defaults is data, not a schema to resolve.
pub fn local_schema(schema: &Value) -> Result<()> {
    let Some(object) = schema.as_object() else {
        return Ok(());
    };
    for key in ["$ref", "$dynamicRef", "$recursiveRef"] {
        if let Some(value) = object.get(key) {
            if !value
                .as_str()
                .is_some_and(|v| v.is_empty() || v.starts_with('#'))
            {
                return Err(Error("remote_schema_reference_forbidden"));
            }
        }
    }
    for key in [
        "$defs",
        "definitions",
        "properties",
        "patternProperties",
        "dependentSchemas",
    ] {
        if let Some(values) = object.get(key).and_then(Value::as_object) {
            for value in values.values() {
                local_schema(value)?;
            }
        }
    }
    for key in ["allOf", "anyOf", "oneOf", "prefixItems"] {
        if let Some(values) = object.get(key).and_then(Value::as_array) {
            for value in values {
                local_schema(value)?;
            }
        }
    }
    for key in [
        "not",
        "if",
        "then",
        "else",
        "items",
        "contains",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "propertyNames",
        "contentSchema",
    ] {
        if let Some(value) = object.get(key) {
            local_schema(value)?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn contract_reference_is_self_contained() {
        let c = Catalog::builtins().unwrap();
        assert!(c.operations().count() >= 444);
        assert_eq!(
            c.operation("identity.temporary", 3).unwrap().effect,
            "transaction"
        );
        assert!(c.operation("caller.install", 1).is_err());
        assert!(c
            .validate("schema:identity.register:2", &Json::parse("{}").unwrap())
            .is_err());
    }
    #[test]
    fn remote_schemas_cannot_read_files_or_make_requests() {
        for uri in [
            "file:///etc/passwd",
            "http://localhost/private",
            "schema:other",
        ] {
            assert!(local_schema(&json!({"allOf":[{"$ref":uri}]})).is_err());
        }
        assert!(local_schema(&json!({"properties":{"$ref":{"type":"string"}},"default":{"$ref":"https://example.org"}})).is_ok());
    }
}
