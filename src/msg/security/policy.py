"""Small pure mode/scope primitives. Certificate rights never replace entry checks."""
from __future__ import annotations
from msg.core.errors import require

READ,WRITE,EXECUTE=4,2,1
CERTGATE,SETGID,STICKY=0o4000,0o2000,0o1000


def selected_class(resource,subject,memberships):
    if subject is not None and subject==resource.owner:
        return 'owner'
    if subject is not None and resource.group in memberships:
        return 'group'
    return 'other'


def class_bits(mode,permission_class):
    require(type(mode) is int and 0<=mode<=0o7777,'invalid_mode')
    return (mode>>{'owner':6,'group':3,'other':0}[permission_class])&7


def allows(resource,subject,memberships,permission):
    mask={'read':READ,'write':WRITE,'execute':EXECUTE,'list':READ,'traverse':EXECUTE}[permission]
    return class_bits(resource.mode,selected_class(resource,subject,memberships))&mask==mask


def inherit_group(parent,primary_group):
    return parent.group if parent.mode&SETGID else primary_group


def format_mode(mode):
    require(type(mode) is int and 0<=mode<=0o7777,'invalid_mode')
    return f'{mode:04o}'


async def scope_contains(scope,resource_id,session):
    if scope.resource_id==resource_id:
        return True
    return scope.descendants and any(r.id==scope.resource_id for r in await session.ancestors(resource_id))


async def scope_subset(child,parent,session):
    if child.resource_id==parent.resource_id:
        return not child.descendants or parent.descendants
    return parent.descendants and await scope_contains(parent,child.resource_id,session)


async def grant_covers(grant,capability,operation,resource_id,session):
    return (grant.capability==capability and grant.version==1 and operation in grant.operations
            and await scope_contains(grant.scope,resource_id,session))


def constraints_subset(child,parent):
    """Lists narrow allowlists; upper bounds may only decrease. Unknown keys reject upstream."""
    for name,value in parent.items():
        if name not in child:
            return False
        proposed=child[name]
        if name in {'hosts','methods','ports','schemes'}:
            if not set(proposed)<=set(value):
                return False
        elif name in {'max_response_bytes','timeout_ms','max_redirects'}:
            if type(proposed) is not int or proposed>value:
                return False
        elif proposed!=value:
            return False
    return True
