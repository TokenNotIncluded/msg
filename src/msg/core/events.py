"""Deterministic committed-operation event identity shared by all producers."""

from msg.core.codec import digest


def event_id(request, subject):
    return 'e_' + digest((subject, request.request_id))[7:39]


# Event.type retains its original audit meaning. Delivery categories are a
# projection of that fact, shared by polling and domain webhooks.
RESOURCE_EVENT_TYPES = {
    'content.post_create': 'resource.created',
    'content.topic_create': 'resource.created',
    'content.mkdir': 'resource.created',
    'discussion.reply': 'resource.created',
    'discussion.quote': 'resource.created',
    'content.post_edit': 'resource.updated',
    'content.move': 'resource.moved',
    'content.archive': 'resource.archived',
    'content.purge': 'resource.purged',
    'content.restore': 'resource.restored',
    'content.chmod': 'resource.permissions_changed',
    'content.chgrp': 'resource.permissions_changed',
    'content.chown': 'resource.permissions_changed',
}


def event_envelope(event, service, resources, *, actor=None, subject=None):
    """Project authorized references only; arbitrary audit data stays private."""
    from msg.core.addressing import resource_address, service_origin
    from msg.core.codec import wire

    return {
        'version': 1,
        'id': event.id,
        'source': service_origin(service),
        'type': RESOURCE_EVENT_TYPES.get(event.type, event.type),
        'operation': event.type,
        'time': wire(event.time),
        'request_id': event.request_id,
        'actor': actor,
        'subject': subject,
        'resources': [resource_address(service, ref) for ref in resources],
    }
