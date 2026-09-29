"""GraphQL is a thin adapter over OperationExecutor, never another business API."""

from __future__ import annotations

import inspect

from msg.core.codec import freeze_json, wire
from msg.core.errors import Failure, require
from msg.core.executor import result_wire
from msg.transports.packet import decode_packet


class GraphQLAdapter:
    def __init__(self, service):
        try:
            import graphql
        except ImportError as exc:
            raise Failure('dependency_unavailable', details={'dependency': 'graphql-core'}) from exc
        self.graphql = graphql
        self.service = service
        scalar = graphql.GraphQLScalarType(
            'JSON',
            serialize=wire,
            parse_value=lambda value: wire(freeze_json(value)),
            parse_literal=lambda node, variables=None: graphql.value_from_ast_untyped(
                node, variables
            ),
        )

        def field(effect, name=None):
            async def resolve(root, info, packet):
                try:
                    request = decode_packet(
                        packet, service.settings.server.limits.max_request_bytes
                    )
                    spec = service.registry.operation(request.operation, request.contract_version)
                    require('network' in spec.entries, 'entry_not_allowed')
                    require(
                        (spec.effect == 'read') == (effect == 'read'), 'graphql_effect_mismatch'
                    )
                    require(name is None or request.operation == name, 'operation_mismatch')
                    return result_wire(await service.executor.execute(request, entry='network'))
                except Failure as exc:
                    raise graphql.GraphQLError(exc.code, extensions={'code': exc.code}) from exc

            return graphql.GraphQLField(
                scalar,
                args={'packet': graphql.GraphQLArgument(graphql.GraphQLNonNull(scalar))},
                resolve=resolve,
            )

        queries = {'call': field('read')}
        mutations = {'call': field('transaction')}
        for spec in service.registry.operations('network'):
            base_name = spec.name.replace('.', '_')
            name = base_name if spec.version == 1 else f'{base_name}_v{spec.version}'
            table = queries if spec.effect == 'read' else mutations
            require(name not in table, 'graphql_name_conflict')
            table[name] = field(spec.effect, spec.name)
        self.schema = graphql.GraphQLSchema(
            query=graphql.GraphQLObjectType('Query', queries),
            mutation=graphql.GraphQLObjectType('Mutation', mutations),
        )

    async def handle(self, body, *, operation_kind=None):
        require(
            isinstance(body, dict) and set(body) <= {'query', 'variables', 'operationName'},
            'invalid_graphql_request',
        )
        require(
            isinstance(body.get('query'), str) and len(body['query']) <= 65536,
            'invalid_graphql_query',
        )
        require(
            body.get('variables') is None or isinstance(body['variables'], dict),
            'invalid_graphql_variables',
        )
        require(operation_kind in {None, 'query', 'mutation'}, 'invalid_graphql_operation')
        g = self.graphql
        try:
            document = g.parse(body['query'], max_tokens=10000)
            operation = g.get_operation_ast(document, body.get('operationName'))
            require(operation is not None, 'graphql_operation_required')
            if operation_kind is not None:
                require(operation.operation.value == operation_kind, 'graphql_effect_mismatch')
            errors = g.validate(self.schema, document, max_errors=20)
            if errors:
                return {
                    'errors': [
                        {
                            'message': 'graphql_validation_error',
                            'extensions': {'code': 'graphql_validation_error'},
                        }
                    ]
                }
            result = g.execute(
                self.schema,
                document,
                variable_values=body.get('variables'),
                operation_name=body.get('operationName'),
            )
            if inspect.isawaitable(result):
                result = await result
        except g.GraphQLError, RecursionError:
            return {
                'errors': [
                    {
                        'message': 'graphql_parse_error',
                        'extensions': {'code': 'graphql_parse_error'},
                    }
                ]
            }
        output = {}
        if result.data is not None:
            output['data'] = result.data
        if result.errors:
            # GraphQL's default variable-coercion errors may contain user inputs.
            # Return stable codes without echoing credentials, payloads, or URLs.
            output['errors'] = [
                {
                    'message': e.extensions.get('code', 'graphql_execution_error'),
                    'extensions': {'code': e.extensions.get('code', 'graphql_execution_error')},
                    **({'path': e.path} if e.path else {}),
                }
                for e in result.errors
            ]
        return output
