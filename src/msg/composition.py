"""One trusted registration sequence for executable and metadata-only roles."""
import importlib

from msg.core.errors import require
from msg.security.capabilities import install_capabilities


PLUGIN_ORDER = ('identity', 'content', 'discussion', 'communication', 'discovery',
                'achievements', 'recovery', 'sharing', 'money', 'offers', 'store',
                'bounty', 'orders', 'delivery', 'transfer', 'extensions', 'system', 'batch')


def install_contracts(target):
    # Installers declare contracts; they neither initialize storage nor run jobs.
    configured = set(target.settings.server.plugins)
    require(configured <= set(PLUGIN_ORDER), 'unknown_plugin')
    for plugin in PLUGIN_ORDER:
        if plugin in configured:
            importlib.import_module('msg.plugins.' + plugin).install(target)
    install_capabilities(target.registry)
    target.registry.freeze()
