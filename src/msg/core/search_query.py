"""Select the minimum SearchQuery contract shared by all read adapters."""
from collections.abc import Mapping

SEARCH_V5_FIELDS = frozenset({'revision', 'source_version', 'relation_to', 'relation_from',
                              'has_replies', 'has_references'})


def search_query_version(arguments):
    if (SEARCH_V5_FIELDS & arguments.keys() or isinstance(arguments.get('scope'), Mapping) or
            arguments.get('field') == 'title' or 'title' in arguments.get('fields', ())):
        return 5
    if 'suggest' in arguments:
        return 4
    if {'source_kind', 'relation_type'} & arguments.keys():
        return 3
    return 2 if 'facets' in arguments else 1
