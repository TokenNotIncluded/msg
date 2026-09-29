"""Shared ReadQuery membership predicates; callers enforce current authorization."""
from msg.plugins.common import resolve


async def read_predicates(app, tx, a, parent):
    filters=['r.state=?']
    parameters=[a.get('state','active')]
    if parent:
        filters.append('r.parent=?'); parameters.append(parent)
    if a.get('type'):
        app.registry.resource_type(a['type'],1)
        filters.append('r.type=?'); parameters.append(a['type'])
    if a.get('author'):
        filters.append('r.owner=?'); parameters.append(await resolve(tx,a['author']))
    if a.get('query'):
        text=a['query'].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        filters.append("(r.name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM projections p WHERE p.resource_id=r.id AND p.text LIKE ? ESCAPE '\\'))")
        parameters.extend(['%'+text+'%','%'+text+'%'])
    if a.get('tag'):
        filters.append('EXISTS (SELECT 1 FROM resource_tags rt WHERE rt.resource_id=r.id AND rt.tag=?)')
        parameters.append(a['tag'])
    return filters, parameters
