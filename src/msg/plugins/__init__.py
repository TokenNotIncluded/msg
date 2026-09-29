"""The installed plugin order has one owner for every runtime role."""
import importlib

from msg.core.errors import require


BUILTINS = (
    'identity', 'content', 'discussion', 'communication', 'discovery',
    'achievements', 'recovery', 'sharing', 'money', 'offers', 'store',
    'bounty', 'orders', 'delivery', 'transfer', 'extensions', 'system', 'batch',
)


def install_registry(context, plugins):
    """Register installed code only; the context determines available capabilities.

    The write Application supplies its own context. A read-only host supplies a
    disposable registration context and a registry which discards callbacks.
    No storage connection, runtime secret or business operation is needed here.
    """
    from msg.security.capabilities import install_capabilities
    configured = set(plugins)
    require(configured <= set(BUILTINS), 'unknown_plugin')
    for name in BUILTINS:
        if name in configured:
            importlib.import_module('msg.plugins.' + name).install(context)
    if {'content','discovery','batch'} <= configured:
        importlib.import_module('msg.plugins.files').install(context)
    install_capabilities(context.registry)
    context.registry.freeze()
