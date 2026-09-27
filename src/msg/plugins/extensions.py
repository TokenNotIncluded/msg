"""Packaged extensions register ordinary operations on the common executor."""
from msg.plugins.common import registration


def install(app):
    op,finish=registration(app,'extensions',('identity','content','discovery','transfer'))
    from msg.extensions import tools,keystore,hosting,repositories,ssh,rss
    for extension in (tools,keystore,hosting,repositories,ssh,rss):
        extension.register(app,op)
    finish()
