"""The installed plugin order has one owner for every runtime role."""
import importlib

from msg.core.errors import require


BUILTINS = (
    'identity', 'content', 'discussion', 'communication', 'discovery',
    'achievements', 'recovery', 'sharing', 'money', 'offers', 'store',
    'bounty', 'orders', 'delivery', 'transfer', 'extensions', 'system', 'batch',
)


def validate_plugins(plugins):
    """Validate configured names before importing any installed plugin."""
    require(isinstance(plugins, (list, tuple)) and
            all(isinstance(name, str) for name in plugins), 'invalid_plugin_list')
    require(len(set(plugins)) == len(plugins), 'invalid_plugin_list')
    require(set(plugins) <= set(BUILTINS), 'unknown_plugin')
    require('identity' in plugins, 'identity_plugin_required')
    return tuple(plugins)


def install_registry(context, plugins):
    """Register installed code only; the context determines available capabilities.

    The write Application supplies its own context. A read-only host supplies a
    disposable registration context and a registry which discards callbacks.
    No storage connection, runtime secret or business operation is needed here.
    """
    from msg.security.capabilities import install_capabilities
    configured = set(validate_plugins(plugins))
    for name in BUILTINS:
        if name in configured:
            importlib.import_module('msg.plugins.' + name).install(context)
    if {'content','discovery','batch'} <= configured:
        importlib.import_module('msg.plugins.files').install(context)
    install_capabilities(context.registry)
    context.registry.freeze()
