"""Profile artwork stays in isolated SVG image documents, never inline user HTML."""

import base64
from hashlib import sha256
from html import escape
from urllib.parse import quote

from msg.core.profile_art import generate_svg, safe_svg, still_svg

PROFILE_CSS = """
.profile-surface { margin-top:40px; }
.profile-heading { position:relative; isolation:isolate; overflow:hidden; min-height:280px; padding:40px 28px; background:#0c1018; color:#f5f5f7; }
.profile-copy { position:relative; z-index:1; width:calc(100% - 220px); }
.prose .profile-heading h1 { font-size:clamp(30px,4vw,48px); line-height:1.2; margin:0 0 16px; letter-spacing:-.03em; }
.prose p.profile-meta { color:#bbc3d0; font-size:13px; margin:0; }
.profile-social { gap:24px; margin-top:24px; }
.profile-social a { color:#f5f5f7; font-size:14px; }
.profile-social span { margin-left:6px; font-variant-numeric:tabular-nums; }
.profile-background { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; z-index:-2; }
.profile-heading::after { content:""; position:absolute; inset:0; z-index:-1; background:linear-gradient(90deg,#0c1018ed,#0c101866 65%,#0c101822); pointer-events:none; }
.profile-avatar { position:absolute; right:24px; top:24px; width:210px; height:224px; object-fit:contain; }
.profile-layout { display:grid; grid-template-columns:minmax(0,1fr) 180px; gap:40px; padding-top:32px; }
.profile-main,.profile-details { min-width:0; }
.profile-main section { margin:0 0 40px; }
.profile-posts ul,.profile-details ul { padding:0; list-style:none; }
.profile-posts li { margin:0 0 24px; }
.profile-posts h3 { margin:0 0 8px; font-size:18px; }
.profile-posts p { color:var(--muted); margin:8px 0; }
.profile-bio-source,.profile-details { font-size:13px; }
.profile-ocean { position:relative; margin-top:56px; padding:0; overflow:hidden; background:transparent; }
.profile-footer { display:block; width:100%; height:200px; object-fit:cover; }
@media(max-width:640px) {
 .profile-heading { padding:28px 20px 64px; min-height:260px; }
 .profile-copy { width:calc(100% - 100px); }
 .profile-avatar { width:110px; height:150px; right:4px; top:18px; }
 .prose .profile-heading h1 { font-size:clamp(23px,6vw,32px); }
 .profile-social { gap:8px 20px; flex-direction:column; align-items:flex-start; }
 .profile-layout { grid-template-columns:1fr; gap:16px; }
}
"""

PROFILE_SCRIPT = """(() => {
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 document.querySelectorAll('.profile-heading,.profile-ocean').forEach(root=>{
   const images=[...root.querySelectorAll('[data-motion-src]')];
   images.forEach(img=>{img.dataset.motionSrc=img.getAttribute('src')});
   let paused=reduced.matches, visible=true;
   function sync(){
     const stop=paused||!visible||document.hidden||reduced.matches;
     images.forEach(img=>{const src=stop?img.dataset.stillSrc:img.dataset.motionSrc;if(img.getAttribute('src')!==src)img.setAttribute('src',src)});
   }
   document.addEventListener('visibilitychange',sync);
   reduced.addEventListener('change',()=>{paused=reduced.matches;sync()});
   if('IntersectionObserver' in window)new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;sync()}).observe(root);
   sync();
 });
})();"""
PROFILE_HASH = base64.b64encode(sha256(PROFILE_SCRIPT.encode()).digest()).decode()


def image_uri(svg):
    return 'data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode()


def artwork_html(value, kinds=('background', 'avatar')):
    artwork = value['profile'].get('artwork', {})
    images = []
    for kind in kinds:
        # Defense in depth if a caller supplies a projection without our reader.
        svg = safe_svg(artwork.get(kind, {}).get('svg', '').encode())
        svg = svg or generate_svg(value.get('id', value['name']), kind)
        motion, still = image_uri(svg), image_uri(still_svg(svg))
        alt = value['name'] + ' avatar' if kind == 'avatar' else ''
        images.append(
            f'<img class="profile-{kind}" src="{motion}" data-motion-src="" data-still-src="{still}" alt="{escape(alt, quote=True)}"'
            + (' aria-hidden="true"' if not alt else '')
            + '>'
        )
    return ''.join(images)


def profile_body(value):
    """Browser layout over the same already-authorized profile projection."""
    from msg.transports.home_page import display_time

    activity = value['profile']
    path = quote('/' + value['name'], safe='/@')
    joined = display_time(value.get('created_at', ''))
    body = (
        '<section class="profile-surface"><header class="profile-heading">'
        + artwork_html(value)
        + '<div class="profile-copy"><h1>'
        + escape(value['name'])
        + '</h1>'
    )
    body += (
        '<p class="profile-meta">'
        + escape(value.get('kind', 'registered'))
        + ' · '
        + escape(joined)
        + ' · '
        + str(activity['post_count'])
        + ' 可见帖子 / visible posts</p>'
    )
    body += '<nav class="profile-social" aria-label="Account relationships">'
    for name, label, key in [
        ('follows', '关注 / Following', 'following_count'),
        ('followers', '粉丝 / Followers', 'follower_count'),
    ]:
        body += f'<a href="{escape(path + "/" + name, quote=True)}">{label} <span>{activity.get(key, 0)}</span></a>'
    body += '</nav></div></header><div class="profile-layout"><div class="profile-main"><section class="profile-bio"><h2>简介 / Bio</h2>'
    bio = activity.get('bio', '')
    if bio:
        for paragraph in bio.split('\n\n'):
            body += '<p>' + escape(paragraph).replace('\n', '<br>') + '</p>'
    else:
        body += '<p class="muted">暂未填写简介。 / No bio yet.</p>'
    if activity.get('bio_path'):
        body += f'<a class="profile-bio-source" href="{escape(path + "/BIO.md", quote=True)}">BIO.md</a>'
    else:
        body += '<a class="profile-bio-source" href="https://github.com/TokenNotIncluded/msg/blob/main/docs/PROFILES.md">填写简介 / Add a bio</a>'
    body += '</section><section class="profile-posts"><h2>最近帖子 / Latest posts</h2><ul>'
    for item in activity['latest_posts']:
        target = quote(item['path'], safe='/@*&')
        body += (
            '<li><h3><a href="'
            + escape(target, quote=True)
            + '">'
            + escape(item['title'])
            + '</a></h3>'
        )
        body += '<time>' + escape(display_time(item['created_at'])) + '</time>'
        if item.get('excerpt'):
            body += '<p>' + escape(item['excerpt']) + '</p>'
        body += '</li>'
    body += '</ul>'
    if not activity['latest_posts']:
        body += '<p class="muted">暂无可见帖子。 / No visible posts yet.</p>'
    body += '</section></div><aside class="profile-details">'
    if value.get('groups'):
        body += '<details class="profile-groups"><summary>用户分类 / User groups</summary><ul>'
        for group in value['groups']:
            target = quote(group['path'], safe='/@&')
            body += (
                '<li><a href="'
                + escape(target, quote=True)
                + '">'
                + escape(group['name'])
                + '</a></li>'
            )
        body += '</ul></details>'
    body += '<details class="profile-resources"><summary>资源 / Resources</summary><ul>'
    for item in value.get('items', []):
        target = quote(item['path'], safe='/@*&')
        body += (
            '<li><a href="' + escape(target, quote=True) + '">' + escape(item['name']) + '</a></li>'
        )
    body += '</ul>'
    if not value.get('items'):
        body += '<p class="muted">暂无可见资源。 / No visible resources.</p>'
    return (
        body
        + '</details></aside></div></section>'
        + '<footer class="profile-ocean" aria-label="Profile ocean artwork">'
        + artwork_html(value, ('footer',))
        + '</footer>'
    )
