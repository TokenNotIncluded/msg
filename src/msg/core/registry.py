"""Finite, explicit plugin contracts. No code loading from resources."""
from __future__ import annotations

from copy import deepcopy

from referencing.exceptions import Unresolvable
from msg.core.schema_policy import local_validator
from msg.core.codec import digest,wire
from msg.core.errors import Failure,require


# Older credential contracts remain in the registry so their published short
# codes and schemas can explain the migration path, but the executor rejects
# them. Keep this metadata separate from OperationSpec so contract identity and
# published operation rows remain unchanged.
_DEPRECATED_OPERATIONS={
    ('identity.temporary',1):('identity.temporary',3),
    ('identity.temporary',2):('identity.temporary',3),
    ('identity.custodial_create',1):('identity.custodial_create',2),
    ('identity.token_rotate',1):('identity.token_rotate',2),
    ('identity.token_create',1):('identity.token_create',2),
}


class Registry:
    def __init__(self):
        self._types={}
        self._capabilities={}
        self._operations={}
        self._schemas={}
        self._validators={}
        self._plugins={}
        self._frozen=False

    def _insert(self,collection,spec,kind):
        require(not self._frozen,'registry_frozen')
        require(spec.version>=1 and spec.name and '*' not in spec.name,'invalid_registry_name')
        key=(spec.name,spec.version)
        require(key not in collection,'duplicate_'+kind)
        collection[key]=spec

    def add_resource_type(self,spec):
        self._insert(self._types,spec,'resource_type')

    def add_capability(self,spec):
        self._insert(self._capabilities,spec,'capability')

    def add_operation(self,spec):
        require(not spec.name.startswith('root.') or spec.entries==frozenset({'local_admin'}),'local_only_contract')
        require(spec.entries and spec.entries<=frozenset({'local_admin','network','worker'}),'invalid_entries')
        self._insert(self._operations,spec,'operation')

    def add_schema(self,ref,schema):
        require(not self._frozen and ref.id not in self._schemas,'schema_conflict')
        # Keep one owned snapshot for both validation and publication. Caller
        # dictionaries and lookup results must not mutate a frozen contract.
        schema=deepcopy(schema)
        validator=local_validator(schema)
        self._schemas[ref.id]=schema
        self._validators[ref.id]=validator

    def add(self,manifest):
        from msg.plugins.features import validate_feature_claims
        validate_feature_claims(manifest)
        require(not self._frozen,'registry_frozen')
        require(manifest.name not in self._plugins,'duplicate_plugin')
        require(all(d in self._plugins for d in manifest.dependencies),'missing_plugin_dependency')
        # Validate the entire manifest before modifying any registration map.
        for collection,values in ((self._types,manifest.resource_types),
                                  (self._capabilities,manifest.capabilities),
                                  (self._operations,manifest.operations)):
            keys=[(v.name,v.version) for v in values]
            require(len(keys)==len(set(keys)) and not set(keys)&collection.keys(),'plugin_conflict')
        for item in (*manifest.resource_types,*manifest.capabilities,*manifest.operations):
            require(item.version>=1 and item.name and '*' not in item.name,'invalid_registry_name')
        for item in manifest.operations:
            require(not item.name.startswith('root.') or item.entries==frozenset({'local_admin'}),'local_only_contract')
            require(item.entries and item.entries<=frozenset({'local_admin','network','worker'}),'invalid_entries')
        for item in manifest.resource_types:
            self.add_resource_type(item)
        for item in manifest.capabilities:
            self.add_capability(item)
        for item in manifest.operations:
            self.add_operation(item)
        self._plugins[manifest.name]=manifest

    def features(self,plugin_name):
        """Resolve a plugin's defaults/checks from the sole bootstrap inventory."""
        from msg.bootstrap import feature_manifest
        require(plugin_name in self._plugins,'unknown_plugin')
        claims=self._plugins[plugin_name].feature_ids
        return tuple(deepcopy(row) for row in feature_manifest()
                     if row['feature_id'] in claims)

    def freeze(self):
        from msg.bootstrap import feature_manifest
        feature_manifest()
        from msg.bootstrap import RULE_PATHS
        require('identity' in self._plugins,'identity_plugin_required')
        for spec in self._operations.values():
            require(spec.requires_rules and len(set(spec.requires_rules))==len(spec.requires_rules)
                    and all(rule_id in RULE_PATHS for rule_id in spec.requires_rules),
                    'dangling_requires_rules')
            require(spec.input_schema.id in self._schemas and spec.output_schema.id in self._schemas,'missing_schema')
        operation_ids={f'{s.name}@{s.version}' for s in self._operations.values()}
        for resource in self._types.values():
            require(resource.content_schema is None or
                    resource.content_schema.id in self._schemas,'missing_schema')
            require(resource.operations<=operation_ids,'unknown_resource_operation')
        for cap in self._capabilities.values():
            require(cap.constraints_schema is None or
                    cap.constraints_schema.id in self._schemas,'missing_schema')
            require(cap.scope_types<=set(n for n,v in self._types),'unknown_scope_type')
            require(cap.operations<=operation_ids,'unknown_capability_operation')
        self._frozen=True

    @property
    def frozen(self):
        return self._frozen

    def _get(self,collection,name,version,kind):
        try:
            return collection[(name,version)]
        except KeyError as exc:
            raise Failure('unknown_'+kind) from exc

    def operation(self,name,version=1):
        return self._get(self._operations,name,version,'operation')

    def capability(self,name,version=1):
        return self._get(self._capabilities,name,version,'capability')

    def resource_type(self,name,version=1):
        return self._get(self._types,name,version,'resource_type')

    def capabilities(self):
        return tuple(self._capabilities[k] for k in sorted(self._capabilities))

    def operations(self,entry=None):
        return tuple(self._operations[k] for k in sorted(self._operations)
                     if entry is None or entry in self._operations[k].entries)

    def resource_types(self):
        return tuple(self._types[k] for k in sorted(self._types))

    def schema(self,ref):
        key=ref.id if hasattr(ref,'id') else ref
        require(key in self._schemas,'schema_not_found')
        return deepcopy(self._schemas[key])

    def validate(self,ref,value):
        try:
            errors=sorted(self._validators[ref.id if hasattr(ref,"id") else ref].iter_errors(wire(value)),key=lambda e:str(e.path))
        except Unresolvable as exc:
            # Reference exception messages can include filesystem paths/URIs.
            raise Failure('schema_reference_unresolvable') from exc
        if errors:
            raise Failure('schema_validation','.'.join(str(i) for i in errors[0].path) or 'arguments')

    def describe(self,spec):
        from msg.bootstrap import RULE_PATHS
        rule_ids=spec.requires_rules
        result={'name':spec.name,'version':spec.version,'effect':spec.effect,
                'entries':sorted(spec.entries),'require_signature':spec.require_signature,
                'input_schema':wire(spec.input_schema),'output_schema':wire(spec.output_schema),
                'requires_rules':[{'rule_id':rule_id,'path':RULE_PATHS[rule_id]}
                                  for rule_id in rule_ids]}
        replacement=_DEPRECATED_OPERATIONS.get((spec.name,spec.version))
        if replacement is not None:
            result.update(deprecated=True,
                          replaced_by=f'{replacement[0]}@{replacement[1]}')
        return result

    def catalog(self,entry='network'):
        values=[self.describe(s) for s in self.operations(entry)]
        return {'version':1,'digest':digest(values),'operations':values}
