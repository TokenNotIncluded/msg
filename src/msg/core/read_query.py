"""Version selection and shared budgets for explicit nested read projections."""
from collections.abc import Mapping
from dataclasses import dataclass
import time

from msg.core.codec import canonical
from msg.core.errors import require

MAX_READ_DEPTH=4
MAX_READ_NODES=100
MAX_READ_COST=1000
MAX_READ_SCANNED=4096
NESTED_FIELDS=('id','name','type','path','revision')
ROOT_FIELDS=(*NESTED_FIELDS,'generation','created_at','modified_at','owner','group','mode')


def read_query_version(arguments):
    if arguments.get('query_version')==3 or isinstance(arguments.get('expand'),Mapping):
        return 3
    return 2 if any(name in arguments for name in ('expand','collection','nested_first')) else 1


def expansion_schema(depth=MAX_READ_DEPTH):
    if depth==0:
        return {'type':'object','maxProperties':0}
    node={'type':'object','properties':{
        'limit':{'type':'integer','minimum':1,'maximum':10},
        'fields':{'type':'array','items':{'enum':list(NESTED_FIELDS)},
                  'minItems':1,'maxItems':len(NESTED_FIELDS),'uniqueItems':True},
        'expand':expansion_schema(depth-1)},'additionalProperties':False}
    return {'type':'object','properties':{'children':node,'replies':node},
            'additionalProperties':False}


@dataclass
class ReadBudget:
    deadline: float
    max_bytes: int
    nodes: int=0
    cost: int=0
    scanned: int=0

    def check(self):
        require(time.monotonic()<self.deadline,'query_cost_exceeded')

    def scan(self):
        self.check()
        self.scanned+=1
        require(self.scanned<=MAX_READ_SCANNED,'query_cost_exceeded')

    def node(self,fields):
        self.check()
        self.nodes+=1
        self.cost+=1+fields
        require(self.nodes<=MAX_READ_NODES and self.cost<=MAX_READ_COST,
                'query_cost_exceeded')

    def output(self,data):
        self.check()
        require(len(canonical(data))<=self.max_bytes,'response_too_large')
