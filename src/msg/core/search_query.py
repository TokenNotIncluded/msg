"""Select the minimum SearchQuery contract shared by all read adapters."""
from collections.abc import Mapping

SEARCH_V5_FIELDS = frozenset({'revision', 'source_version', 'relation_to', 'relation_from',
                              'has_replies', 'has_references', 'spell'})
SEARCH_V5_RELATIONS = frozenset({'reference', 'state', 'target', 'content'})


def search_query_version(arguments):
    if (SEARCH_V5_FIELDS & arguments.keys() or isinstance(arguments.get('scope'), Mapping) or
            arguments.get('relation_type') in SEARCH_V5_RELATIONS or
            arguments.get('field') == 'title' or 'title' in arguments.get('fields', ())):
        return 5
    if 'suggest' in arguments:
        return 4
    if {'source_kind', 'relation_type'} & arguments.keys():
        return 3
    return 2 if 'facets' in arguments else 1


def spelling_distance(left, right, maximum):
    """Bounded Levenshtein distance; over-budget values share one sentinel."""
    if abs(len(left)-len(right)) > maximum:
        return maximum+1
    previous = list(range(len(right)+1))
    for index, char in enumerate(left, 1):
        current = [index]
        for other, target in enumerate(right, 1):
            current.append(min(current[-1]+1, previous[other]+1,
                               previous[other-1]+(char != target)))
        if min(current) > maximum:
            return maximum+1
        previous = current
    return previous[-1]
