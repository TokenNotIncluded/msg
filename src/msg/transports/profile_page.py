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
.profile-backup p { max-width:65ch; font-size:13px; }
.profile-backup dl { display:grid; grid-template-columns:max-content minmax(0,1fr); gap:10px 20px; margin:20px 0; font:12px/1.8 var(--mono); }
.profile-backup dt { color:var(--muted); }
.profile-backup dd { margin:0; min-width:0; overflow-wrap:anywhere; }
.profile-backup dd code { white-space:normal; overflow-wrap:anywhere; }
.profile-backup-actions { display:flex; align-items:center; flex-wrap:wrap; gap:16px; }
.profile-backup button { min-height:44px; border:1px solid var(--line); background:var(--bg); color:var(--fg); padding:8px; font:12px/1.5 var(--mono); border-radius:0; }
.profile-backup button:disabled { color:var(--muted); cursor:wait; }
.profile-backup :focus-visible { outline:2px solid var(--fg); outline-offset:3px; }
.profile-backup summary { min-height:44px; display:flex; align-items:center; cursor:pointer; font:12px/1.5 var(--mono); }
.profile-backup pre { white-space:pre-wrap; overflow-wrap:anywhere; }
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
 const shellQuote=value=>"'"+value.replaceAll("'","'\\''")+"'";
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
 const backup=document.querySelector('.profile-backup');
 if(backup){
   const status=backup.querySelector('.profile-backup-status'),record=backup.querySelector('.profile-backup-record');
   const retry=backup.querySelector('.profile-backup-retry'),download=backup.querySelector('.profile-backup-download');
   const copy=backup.querySelector('.profile-backup-copy'),command=backup.querySelector('.profile-backup-command');
   const commandView=backup.querySelector('.profile-backup-command-view'),hint=backup.querySelector('.profile-backup-hint');
   const account=document.querySelector('a.current-account');
   backup.querySelector('.profile-backup-owner').hidden=!account||account.getAttribute('href')!==backup.dataset.profilePath;
   const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
   const keys=(value,required,optional=[])=>object(value)&&required.every(key=>Object.hasOwn(value,key))&&Object.keys(value).every(key=>[...required,...optional].includes(key));
   function validateManifest(value){
     if(!keys(value,['schema','server','subject_id','key_id','created_at','encryption','archive_format','file'],['recovery_hint'])
       ||value.schema!=='msg.identity-backup/1'||value.server!==location.origin
       ||typeof value.subject_id!=='string'||!/^[A-Za-z0-9_.:-]{1,160}$/.test(value.subject_id)||value.subject_id!==backup.dataset.subjectId
       ||typeof value.key_id!=='string'||!/^[A-Za-z0-9_.:-]{1,160}$/.test(value.key_id)
       ||typeof value.created_at!=='string'||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value.created_at)
       ||Number(value.created_at.slice(0,4))<1||!Number.isFinite(Date.parse(value.created_at))||new Date(value.created_at).toISOString().slice(0,19)!==value.created_at.slice(0,19)
       ||!keys(value.encryption,['format'])||!['age','gpg'].includes(value.encryption.format)
       ||!['msg.account-backup/1','external'].includes(value.archive_format)
       ||!keys(value.file,['path','sha256','size'])||typeof value.file.sha256!=='string'||!/^[a-f0-9]{64}$/.test(value.file.sha256)
       ||!Number.isSafeInteger(value.file.size)||value.file.size<1||value.file.size>8388608
       ||value.file.path!==backup.dataset.profilePath+'/BACKUP-'+value.file.sha256.slice(0,16)+'.'+value.encryption.format
       ||(Object.hasOwn(value,'recovery_hint')&&(typeof value.recovery_hint!=='string'||Array.from(value.recovery_hint).length>500||/[\u0000-\u001f\u007f]/.test(value.recovery_hint))))throw Error('invalid backup metadata');
     return value;
   }
   async function manifestBytes(response){
     const reader=response.body?.getReader(),parts=[];let size=0;
     if(!reader){const bytes=new Uint8Array(await response.arrayBuffer());if(bytes.length>16384)throw Error('backup metadata too large');return bytes}
     try{while(true){const {value,done}=await reader.read();if(done)break;size+=value.length;if(size>16384)throw Error('backup metadata too large');parts.push(value)}}
     finally{await reader.cancel()}
     const bytes=new Uint8Array(size);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.length}return bytes;
   }
   let version=0;
   async function loadBackup(){
     const current=++version,controller=new AbortController(),timer=setTimeout(()=>controller.abort(),10000);
     record.hidden=true;download.removeAttribute('href');copy.hidden=true;commandView.hidden=true;command.textContent='';hint.hidden=true;
     retry.hidden=true;retry.disabled=true;backup.setAttribute('aria-busy','true');status.textContent='正在读取备份信息… / Reading backup information…';
     try{
       const response=await fetch(backup.dataset.profilePath+'/BACKUP.json/raw',{credentials:'omit',cache:'no-store',redirect:'error',signal:controller.signal,headers:{Accept:'application/json'}});
       if(current!==version)return;
       if(response.status===404){status.textContent='尚未登记备份。 / No backup registered.';return}
       if(!response.ok)throw Error('backup discovery failed');
       const value=validateManifest(JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(await manifestBytes(response))));
       if(current!==version)return;
       for(const [field,text] of Object.entries({created_at:value.created_at.replace('T',' ').replace('Z',' UTC'),format:value.encryption.format,size:value.file.size.toLocaleString()+' 字节 / bytes',sha256:value.file.sha256}))backup.querySelector('[data-backup-field='+field+']').textContent=text;
       hint.textContent=value.recovery_hint?'发布者的恢复备注 / Publisher note: '+value.recovery_hint:'';hint.hidden=!value.recovery_hint;
       download.href=value.file.path+'/raw';download.download=value.file.path.split('/').at(-1);
       const automatic=value.encryption.format==='age'&&value.archive_format==='msg.account-backup/1';
       copy.hidden=commandView.hidden=!automatic;
       backup.querySelector('.profile-backup-restore-note').textContent=automatic?'替换命令中的本机解密钥路径后执行；恢复时会检查摘要和大小。 / Replace the local decryption-key path before running; restore checks the checksum and size.':'这是外部格式备份，请下载后用自己的本机工具解密。 / Download and decrypt this backup with your local tools.';
       if(automatic)command.textContent='msg --server '+shellQuote(location.origin)+' account restore restored-account --from '+shellQuote(backup.dataset.profilePath.slice(1))+' --identity '+shellQuote('/path/to/identity.txt');
       record.hidden=false;status.textContent='已登记加密备份。 / Encrypted backup registered.';
     }catch{if(current===version){status.textContent='读取备份失败，请重试。 / Could not read backup information; retry.';retry.hidden=false}}
     finally{clearTimeout(timer);if(current===version){retry.disabled=false;backup.setAttribute('aria-busy','false')}}
   }
   retry.addEventListener('click',loadBackup);
   copy.addEventListener('click',async()=>{
     try{await navigator.clipboard.writeText(command.textContent);status.textContent='恢复命令已复制；解密和恢复在自己的终端完成。 / Restore command copied; decrypt and restore in your own terminal.'}
     catch{commandView.open=true;status.textContent='请选择并复制下方恢复命令。 / Select and copy the restore command below.'}
   });
   loadBackup();
 }
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
 const prepared=editor.querySelector('.profile-art-prepared'),command=editor.querySelector('.profile-art-command');
 const fileCommand=editor.querySelector('.profile-art-file-command');
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
     const encoded=btoa(binary),data=encoded.replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
     const name=kind.value==='avatar'?'AVATAR.svg':'BACKGROUND.svg';
     const response=await fetch(editor.dataset.profilePath+'/'+name+'/meta',{headers:{Accept:'application/json'},credentials:'same-origin'});
     let operation='file.create',args={parent:editor.dataset.profilePath,name,data,media_type:'image/svg+xml'},expect='';
     if(response.ok){
       const meta=await response.json();
       if(meta.type!=='file'||!/^r_[a-zA-Z0-9_]+$/.test(meta.id)||!/^v_[a-zA-Z0-9_]+$/.test(meta.revision)||!Number.isSafeInteger(meta.generation))throw Error('无法读取图片版本。 / Cannot read the current artwork revision.');
       operation='file.write';args={id:meta.id,base_revision:meta.revision,data,media_type:'image/svg+xml'};expect=' --expect '+meta.id+'='+meta.generation;
     }else if(response.status!==404){throw Error('无法读取现有图片，请重新登录后重试。 / Cannot read existing artwork; sign in again.');}
     if(current!==version)return;
     const filename='msg-profile-'+kind.value+'.json';
     const requestJson=JSON.stringify(args,null,2)+'\n';
     downloadUrl=URL.createObjectURL(new Blob([requestJson],{type:'application/json'}));
     const download=editor.querySelector('a[download]');download.href=downloadUrl;download.download=filename;
     const image=document.createElement('img');image.alt='新图片预览 / New artwork preview';image.src='data:image/svg+xml;base64,'+encoded;preview.replaceChildren(image);preview.hidden=false;
     const requestId='profile_art_'+crypto.randomUUID().replaceAll('-','');
     const prefix='msg --server '+shellQuote(location.origin)+' call '+operation;
     const options=' --request-id '+requestId+expect,delimiter='MSG_PROFILE_REQUEST';
     command.textContent=prefix+' -'+options+" <<'"+delimiter+"'\n"+requestJson+delimiter+'\n';
     fileCommand.textContent=prefix+' @'+filename+options;
     prepared.hidden=false;status.textContent='预览已就绪，尚未保存。 / Preview ready; not saved yet.';
   }catch(error){if(current===version)status.textContent=error.message||'无法准备图片。 / Could not prepare artwork.';}
 });
 editor.querySelector('button').addEventListener('click',async()=>{
   try{await navigator.clipboard.writeText(command.textContent);status.textContent='命令已复制，尚未保存；在终端执行成功后刷新页面。 / Command copied, not saved yet; run it in your terminal, then refresh.';}
   catch{prepared.querySelector('.profile-art-command-view').open=true;status.textContent='请选择并复制下方完整命令。 / Select and copy the full command below.';}
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
        '<div class="profile-art-prepared" hidden><p>复制保存命令，粘贴到自己的终端执行。'
        '终端须配置本人 MSG 签名账号；成功后刷新页面。 / Copy the save command and paste it '
        'into your terminal with your own MSG signing account, then refresh after it succeeds.</p>'
        '<div class="profile-art-actions">'
        '<button type="button">复制保存命令 / Copy save command</button>'
        '<a href="https://github.com/TokenNotIncluded/msg/blob/main/docs/PROFILES.md">使用说明 / Help</a>'
        '</div><details class="profile-art-command-view"><summary>查看完整命令 / Show command</summary>'
        '<pre><code class="profile-art-command"></code></pre></details>'
        '<details class="profile-art-download"><summary>下载请求文件（可选） / Download request (optional)</summary>'
        '<p>也可以下载 JSON，在下载目录执行下面这条命令。 / Alternatively, download the JSON '
        'and run this command from its directory.</p><a download>下载 JSON / Download JSON</a>'
        '<pre><code class="profile-art-file-command"></code></pre></details></div></details>'
    )


def backup_panel_html(value, path):
    """Public ciphertext discovery does not depend on an owner's browser session."""
    return (
        '<section class="profile-backup" id="identity-backup" '
        f'data-profile-path="{escape(path, quote=True)}" '
        f'data-subject-id="{escape(value.get("id", ""), quote=True)}">'
        '<h2>身份备份 / Identity backup</h2>'
        '<p>这里只登记加密后的备份，公开提供下载。解密钥由你自己保管。'
        '建议使用 age；其他已登记的加密格式也可下载。 / Only encrypted backups belong here. '
        'Downloads are public; keep the decryption key yourself. age is recommended.</p>'
        '<p class="profile-backup-status" role="status" aria-live="polite">'
        '正在读取备份信息… / Reading backup information…</p>'
        '<button class="profile-backup-retry" type="button" hidden>重新读取 / Retry</button>'
        '<div class="profile-backup-record" hidden><dl>'
        '<dt>日期 / Date</dt><dd data-backup-field="created_at"></dd>'
        '<dt>格式 / Format</dt><dd data-backup-field="format"></dd>'
        '<dt>密文大小 / Size</dt><dd data-backup-field="size"></dd>'
        '<dt>SHA-256</dt><dd><code data-backup-field="sha256"></code></dd></dl>'
        '<p class="profile-backup-hint" hidden></p><div class="profile-backup-actions">'
        '<a class="profile-backup-download">下载加密备份 / Download encrypted backup</a>'
        '<button class="profile-backup-copy" type="button" hidden>复制恢复命令 / Copy restore command</button>'
        '</div><p class="profile-backup-restore-note"></p>'
        '<details class="profile-backup-command-view" hidden><summary>查看恢复命令 / Show restore command</summary>'
        '<pre><code class="profile-backup-command"></code></pre></details></div>'
        '<p class="profile-backup-owner" hidden>'
        '<a href="https://github.com/TokenNotIncluded/msg/blob/main/docs/IDENTITY_BACKUP.md">'
        '登记或更新我的备份 / Register or update my backup</a></p>'
        '</section>'
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
    body += '</section>' + backup_panel_html(value, path)
    body += '<section class="profile-posts"><h2>最近帖子 / Latest posts</h2><ul>'
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
