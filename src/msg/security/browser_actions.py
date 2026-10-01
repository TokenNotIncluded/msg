"""Explicit browser consent covers only these non-signature post writes."""

BROWSER_POST_WRITES = frozenset({
    'content.post_create',
    'content.public_board_update',
    'content.post_edit',
    'discussion.like',
    'discussion.unlike',
    'discussion.bookmark',
    'discussion.unbookmark',
    'discussion.reply',
    'discussion.fork',
    'discussion.prove',
    'communication.follow',
    'communication.unfollow',
})
