"""Profile artwork stays in isolated SVG image documents, never inline user HTML."""

import base64
from hashlib import sha256
from html import escape
from urllib.parse import quote

PROFILE_CSS = """
.profile-surface { margin-top:24px; }
.profile-heading { position:relative; isolation:isolate; overflow:hidden; min-height:216px; padding:32px 0; border-block:1px solid var(--line); background:var(--bg); color:var(--fg); }
.profile-copy { position:relative; z-index:1; width:calc(100% - 190px); }
.prose .profile-heading h1 { font:600 clamp(26px,3.5vw,36px)/1.25 var(--mono); margin:0 0 16px; letter-spacing:-.025em; }
.prose p.profile-meta { color:var(--muted); font:12px/1.8 var(--mono); margin:0; }
.profile-social { gap:24px; margin-top:18px; }
.profile-social a { color:var(--fg); font:13px/1.5 var(--mono); }
.profile-social span { margin-left:6px; font-variant-numeric:tabular-nums; }
.profile-background { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; z-index:-2; }
.profile-heading::after { content:""; position:absolute; inset:0; z-index:-1; background:var(--bg); opacity:.78; pointer-events:none; }
.profile-avatar { position:absolute; right:0; top:16px; width:180px; height:184px; object-fit:contain; }
.profile-layout { display:grid; grid-template-columns:minmax(0,1fr) 180px; gap:40px; padding-top:8px; }
.profile-main,.profile-details { min-width:0; }
.profile-main section { margin:0 0 40px; }
.profile-posts ul,.profile-details ul { padding:0; list-style:none; }
.profile-posts li { margin:0; padding:18px 0; border-bottom:1px solid var(--line); }
.profile-posts li:first-child { padding-top:0; }
.profile-posts h3 { margin:0 0 8px; font-size:18px; }
.profile-posts p { color:var(--muted); margin:8px 0; }
.profile-bio-source,.profile-details { font:12px/1.8 var(--mono); }
.profile-details { padding-top:40px; }
.profile-details details + details { margin-top:20px; }
.profile-details summary { min-height:44px; display:flex; align-items:center; cursor:pointer; }
.profile-ocean { position:relative; margin-top:40px; padding:0; overflow:hidden; background:transparent; border-top:1px solid var(--line); }
.profile-footer { display:block; width:100%; height:120px; object-fit:cover; opacity:.7; }
.profile-edit-link { display:inline-block; margin-top:14px; font:12px/1.8 var(--mono); }
.profile-art-editor { border-bottom:1px solid var(--line); padding:20px 0; }
.profile-art-editor[hidden],.profile-art-editor [hidden],.profile-edit-link[hidden] { display:none; }
.profile-art-editor summary { min-height:44px; cursor:pointer; font:13px/1.5 var(--mono); }
.profile-art-editor p { max-width:65ch; font-size:13px; }
.profile-art-controls { display:flex; align-items:end; flex-wrap:wrap; gap:16px; margin:20px 0; }
.profile-art-controls label { display:grid; gap:8px; min-width:0; font:12px/1.5 var(--mono); }
.profile-art-controls input,.profile-art-controls select,.profile-art-editor button { max-width:100%; min-height:44px; border:1px solid var(--line); background:var(--bg); color:var(--fg); padding:8px; font:12px/1.5 var(--mono); border-radius:0; }
.profile-art-controls input { width:280px; }
.profile-art-editor :focus-visible { outline:2px solid var(--fg); outline-offset:3px; }
.profile-art-preview { border:1px solid var(--line); }
.profile-art-preview img { display:block; width:100%; height:160px; object-fit:contain; }
.profile-art-editor pre { white-space:pre-wrap; overflow-wrap:anywhere; }
.profile-art-actions { display:flex; flex-wrap:wrap; gap:16px; align-items:center; margin-top:16px; }
.profile-art-status { min-height:2em; color:var(--muted); }
@media(max-width:640px) {
 .profile-heading { padding:24px 0; min-height:188px; }
 .profile-copy { width:calc(100% - 100px); }
 .profile-avatar { width:100px; height:148px; right:-6px; top:14px; }
 .prose .profile-heading h1 { font-size:clamp(23px,6vw,32px); }
 .profile-social { gap:8px 20px; flex-direction:column; align-items:flex-start; }
 .profile-layout { grid-template-columns:1fr; gap:0; }
 .profile-details { padding-top:0; border-top:1px solid var(--line); }
 .profile-footer { height:88px; }
 .profile-art-controls { align-items:stretch; flex-direction:column; }
 .profile-art-controls input { width:100%; }
}
"""

PROFILE_SCRIPT = r"""(() => {
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 document.querySelectorAll('.profile-heading,.profile-ocean,.board-heading').forEach(root=>{
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
 const editor=document.querySelector('.profile-art-editor');
 if(!editor)return;
 const account=document.querySelector('a.current-account');
 if(!account||account.getAttribute('href')!==editor.dataset.profilePath)return;
 editor.hidden=false;
 const editLink=document.querySelector('.profile-edit-link');
 editLink.hidden=false;
 editLink.addEventListener('click',()=>{editor.open=true});
 const kind=editor.querySelector('select'),file=editor.querySelector('input[type=file]');
 const preview=editor.querySelector('.profile-art-preview'),status=editor.querySelector('[role=status]');
 const prepared=editor.querySelector('.profile-art-prepared'),command=editor.querySelector('code');
 let downloadUrl=null,version=0;
 const namespace='http://www.w3.org/2000/svg';
 const tags=new Set(['svg','g','defs','title','desc','text','tspan','rect','circle','ellipse','path','line','polyline','polygon','style','animate','animateTransform','animateMotion','set']);
 const animated=new Set(['opacity','fill','stroke','transform','x','y','cx','cy','r','rx','ry','d','visibility','stroke-width','font-size']);
 function validate(bytes){
   if(!bytes.length||bytes.length>98304)throw Error('请选择不超过 96 KiB 的 SVG。 / Choose an SVG up to 96 KiB.');
   const text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);
   if(/<!|url\s*\(|@import|javascript:|data:|https?:|\/\//i.test(text.split(namespace).join('')))throw Error('SVG 不能包含脚本或外部资源。 / SVG must be self-contained.');
   const doc=new DOMParser().parseFromString(text,'image/svg+xml');
   if(doc.querySelector('parsererror')||doc.documentElement.localName!=='svg')throw Error('无法读取 SVG。 / Invalid SVG.');
   for(const node of doc.querySelectorAll('*')){
     if(node.namespaceURI!==namespace||!tags.has(node.localName))throw Error('SVG 包含不支持的元素。 / Unsupported SVG element.');
     for(const attr of node.attributes){
       const name=attr.localName.toLowerCase();
       if(name.startsWith('on')||name==='href'||name==='src'||(name==='attributename'&&!animated.has(attr.value.toLowerCase())))throw Error('SVG 包含不安全的属性。 / Unsafe SVG attribute.');
     }
   }
   return text;
 }
 function reset(){
   ++version;prepared.hidden=true;preview.hidden=true;preview.replaceChildren();status.textContent='';
   if(downloadUrl){URL.revokeObjectURL(downloadUrl);downloadUrl=null}
 }
 kind.addEventListener('change',()=>{reset();file.value=''});
 file.addEventListener('change',async()=>{
   reset();const selected=file.files[0],current=version;if(!selected)return;
   try{
     if(selected.size>98304)throw Error('请选择不超过 96 KiB 的 SVG。 / Choose an SVG up to 96 KiB.');
     const bytes=new Uint8Array(await selected.arrayBuffer());validate(bytes);
     if(current!==version)return;
     let binary='';bytes.forEach(byte=>{binary+=String.fromCharCode(byte)});
     const data=btoa(binary),name=kind.value==='avatar'?'AVATAR.svg':'BACKGROUND.svg';
     const response=await fetch(editor.dataset.profilePath+'/'+name+'/meta',{headers:{Accept:'application/json'},credentials:'same-origin'});
     let operation='file.create',args={parent:editor.dataset.profilePath,name,data,media_type:'image/svg+xml'},expect='';
     if(response.ok){
       const meta=await response.json();
       if(meta.type!=='file'||!/^r_[a-zA-Z0-9_]+$/.test(meta.id)||!/^v_[a-zA-Z0-9_]+$/.test(meta.revision)||!Number.isSafeInteger(meta.generation))throw Error('无法读取图片版本。 / Cannot read the current artwork revision.');
       operation='file.write';args={id:meta.id,base_revision:meta.revision,data,media_type:'image/svg+xml'};expect=' --expect '+meta.id+'='+meta.generation;
     }else if(response.status!==404){throw Error('无法读取现有图片，请重新登录后重试。 / Cannot read existing artwork; sign in again.');}
     if(current!==version)return;
     const filename='msg-profile-'+kind.value+'.json';
     downloadUrl=URL.createObjectURL(new Blob([JSON.stringify(args,null,2)+'\n'],{type:'application/json'}));
     const download=editor.querySelector('a[download]');download.href=downloadUrl;download.download=filename;
     const image=document.createElement('img');image.alt='新图片预览 / New artwork preview';image.src='data:image/svg+xml;base64,'+data;preview.replaceChildren(image);preview.hidden=false;
     const requestId='profile_art_'+crypto.randomUUID().replaceAll('-','');
     command.textContent='msg --server '+location.origin+' call '+operation+' @'+filename+' --request-id '+requestId+expect;
     prepared.hidden=false;status.textContent='预览已就绪，尚未保存。 / Preview ready; not saved yet.';
   }catch(error){if(current===version)status.textContent=error.message||'无法准备图片。 / Could not prepare artwork.';}
 });
 editor.querySelector('button').addEventListener('click',async()=>{
   try{await navigator.clipboard.writeText(command.textContent);status.textContent='命令已复制，执行成功后刷新页面。 / Command copied; refresh after it succeeds.';}
   catch{status.textContent='请选择并复制下方命令。 / Select and copy the command below.';}
 });
})();"""
PROFILE_HASH = base64.b64encode(sha256(PROFILE_SCRIPT.encode()).digest()).decode()


def artwork_html(value, kinds=('background', 'avatar')):
    path = quote('/' + value['name'], safe='/@')
    images = []
    for kind in kinds:
        url = path + '/art/' + kind + '.svg'
        alt = value['name'] + ' avatar' if kind == 'avatar' else ''
        images.append(
            f'<img class="profile-{kind}" src="{escape(url, quote=True)}" '
            f'data-motion-src="" data-still-src="{escape(url + "?still=1", quote=True)}" '
            f'alt="{escape(alt, quote=True)}" loading="lazy" decoding="async"'
            + (' aria-hidden="true"' if not alt else '')
            + '>'
        )
    return ''.join(images)


def artwork_editor_html(path):
    """Prepare existing signed file operations without enlarging browser credentials."""
    return (
        '<details id="profile-art-editor" class="profile-art-editor" hidden '
        f'data-profile-path="{escape(path, quote=True)}">'
        '<summary>修改头像和背景 / Customize artwork</summary>'
        '<p>选择 SVG 并预览，然后用自己的 MSG 签名账号保存。现有图片会保留到保存成功。'
        ' / Preview an SVG, then save with your own signed MSG account.</p>'
        '<div class="profile-art-controls"><label>图片 / Artwork<select aria-label="Artwork">'
        '<option value="avatar">头像 / Avatar</option>'
        '<option value="background">背景 / Background</option></select></label>'
        '<label>SVG 文件 · 最多 96 KiB<input type="file" accept=".svg,image/svg+xml"></label></div>'
        '<div class="profile-art-preview" hidden></div>'
        '<p class="profile-art-status" role="status" aria-live="polite"></p>'
        '<div class="profile-art-prepared" hidden><p>下载请求文件，在同一目录执行下方命令。'
        '确认 CLI 使用的是本人账号；成功后刷新页面。 / Download the request and run the command '
        'from the same directory using your own CLI account, then refresh.</p>'
        '<pre><code></code></pre><div class="profile-art-actions">'
        '<a download>下载请求 / Download request</a>'
        '<button type="button">复制命令 / Copy command</button>'
        '<a href="https://github.com/TokenNotIncluded/msg/blob/main/docs/PROFILES.md">使用说明 / Help</a>'
        '</div></div></details>'
    )


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
    body += (
        '</nav><a class="profile-edit-link" href="#profile-art-editor" hidden>'
        '修改头像和背景 / Customize artwork</a></div></header>'
        + artwork_editor_html(path)
        + '<div class="profile-layout"><div class="profile-main"><section class="profile-bio"><h2>简介 / Bio</h2>'
    )
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
