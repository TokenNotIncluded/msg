"""Deterministic committed-operation event identity shared by all producers."""

from msg.core.codec import digest


def event_id(request, subject):
    return 'e_' + digest((subject, request.request_id))[7:39]
