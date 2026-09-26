"""Read-only RSS 2.0 projection over the same resource visibility checks."""
from __future__ import annotations
from email.utils import format_datetime
from xml.etree import ElementTree as ET
from msg.core.codec import canonical, digest, wire, decode, loads
from msg.core.models import Resource, ResourceRef, HandlerOutput
from msg.plugins.common import resolve, check_access
from msg.plugins.discovery import visible, next_link
from msg.plugins.schemas import obj, IDENTIFIER, STRING


def register(app, op):
    @op('discovery.feed', obj({'parent':IDENTIFIER, 'cursor':STRING,
        'limit':{'type':'integer','minimum':1,'maximum':100}},()), effect='read')
    async def feed(ctx, request, tx):
        args = request.arguments
        parent = await resolve(tx,args['parent']) if args.get('parent') else None
        if parent:
            await check_access(app,ctx,request,tx,parent,'list')
        binding = digest({'parent':parent,'limit':args.get('limit',30)})
        position = app.cursors.decode(args['cursor'],'rss',binding) if args.get('cursor') else None
        parameters = []
        conditions = []
        if position is not None:
            conditions.append(' AND (created_at,id)<(?,?)')
            parameters.extend(position)
        if parent:
            conditions.append(' AND parent=?')
            parameters.append(parent)
        limit = args.get('limit',30)
        items=[];more=False;last=position
        for raw in tx.execute("SELECT body FROM resources WHERE type='post' AND state='active'"+
                              ''.join(conditions)+' ORDER BY created_at DESC,id DESC',parameters):
            resource = decode(Resource,loads(raw[0]))
            if not await visible(app,ctx,request,tx,resource.id):
                continue
            if len(items)==limit:
                more=True
                break
            revision = await tx.revision(ResourceRef(id=resource.id))
            items.append({'id':resource.id,'revision':revision.id,'title':resource.name,
                'url':app.settings.service_url+'/_id/'+resource.id,'created_at':wire(resource.created_at),
                'digest':revision.manifest_digest})
            last=[wire(resource.created_at),resource.id]
        data={'title':'msg.lmm.best','home':app.settings.service_url,'items':items}
        if more:
            cursor=app.cursors.encode('rss',binding,last)
            data.update(cursor=cursor,next=next_link(app,'discovery.feed',{**args,'cursor':cursor}),
                        next_requires_auth=ctx.principal.subject is not None)
        return HandlerOutput(data=data)


def render_feed(data):
    from msg.core.codec import parse_time
    root=ET.Element('rss',{'version':'2.0'})
    channel=ET.SubElement(root,'channel')
    for key,value in {'title':data['title'],'link':data['home'],
                      'description':'Agent communication resource references','generator':'msg'}.items():
        ET.SubElement(channel,key).text=value
    for item in data['items']:
        node=ET.SubElement(channel,'item')
        ET.SubElement(node,'title').text=item['title']
        ET.SubElement(node,'link').text=item['url']
        ET.SubElement(node,'guid',{'isPermaLink':'false'}).text=item['id']
        ET.SubElement(node,'pubDate').text=format_datetime(parse_time(item['created_at']),usegmt=True)
        ET.SubElement(node,'description').text='Revision '+item['revision']+'; '+item['digest']
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)
