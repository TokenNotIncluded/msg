"""只组织已授权的本页帖子；缺失父节点不推测，也不补读。"""

from dataclasses import dataclass

from msg.core.identifiers import hex_id


@dataclass(frozen=True)
class ThreadNode:
    item: dict
    parent: str | None
    children: tuple[str, ...]
    incomplete: bool = False
    invalid: bool = False


def thread_tree(items, root):
    root = hex_id(root)
    posts = {}
    duplicates = set()
    for item in items:
        rid = hex_id(item['id'])
        if rid in posts:
            duplicates.add(rid)
        posts.setdefault(rid, item)
    parents, incomplete, invalid = {}, set(), set(duplicates)
    for rid, item in posts.items():
        targets = [
            hex_id(relation['target']['id'])
            for relation in item.get('relations', ())
            if relation['type'] == 'reply_to'
        ]
        if rid == root:
            parents[rid] = None
            if targets:
                invalid.add(rid)
        elif len(targets) != 1 or targets[0] == rid:
            parents[rid] = None
            invalid.add(rid)
        elif targets[0] not in posts:
            parents[rid] = None
            incomplete.add(rid)
        else:
            parents[rid] = targets[0]

    # 正常写入只能回复已经存在的帖；历史导入或坏数据仍须有限结束。
    checked = set()
    for start in posts:
        trail, positions = [], {}
        current = start
        while current is not None and current not in checked:
            if current in positions:
                for rid in trail[positions[current] :]:
                    parents[rid] = None
                    invalid.add(rid)
                break
            positions[current] = len(trail)
            trail.append(current)
            current = parents[current]
        checked.update(trail)

    children = {rid: [] for rid in posts}
    roots = []
    for rid, parent in parents.items():
        if parent is None:
            roots.append(rid)
        else:
            children[parent].append(rid)
    if root in roots:
        roots.remove(root)
        roots.insert(0, root)
    nodes = {
        rid: ThreadNode(item, parents[rid], tuple(children[rid]), rid in incomplete, rid in invalid)
        for rid, item in posts.items()
    }
    return nodes, tuple(roots)
