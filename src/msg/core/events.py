"""Stable request Event identity shared by execution and domain adapters."""
from .codec import digest


def event_id(request, subject):
    return 'e_' + digest((subject, request.request_id))[7:39]
