"""Persistent, owner-bound byte-range transfers with explicit publication points."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib
import re
from uuid import uuid4

from msg.core.codec import canonical,decode,digest,loads,wire
from msg.core.errors import Failure,require
from msg.core.models import TransferSession,TransferChunk,ResourceRef,AccessRequirement,Page
from msg.storage.capacity import require_transfer_capacity


def validate_digest(value):
    require(isinstance(value,str) and re.fullmatch(r'sha256:[0-9a-f]{64}',value) is not None,'invalid_digest')
    return value


class TransferService:
    def __init__(self,metadata,contents,authorizer,clock, *, publish,ttl=86400,part_bytes=65536):
        self.metadata,self.contents,self.authorizer,self.clock=metadata,contents,authorizer,clock
        self.publish,self.ttl,self.part_bytes=publish,ttl,part_bytes

    async def _access(self,context,tx,resource,operation,check):
        await self.authorizer.require(context,None,(AccessRequirement(resource_id=resource,
            operation=operation+'@1',check=check),),tx)

    async def _session(self,context,tx,id,operation, *, allow_cancelled=False):
        transfer=await tx.transfer(id)
        require(transfer.subject_id==context.principal.subject,'transfer_owner_required')
        require(transfer.expires_at>self.clock(),'transfer_expired')
        require(allow_cancelled or transfer.state not in {'cancelled','expired'},'transfer_closed')
        # Each continuation rechecks the current permission and credential ceiling,
        # not just a cached decision from open().
        check='create' if transfer.direction=='upload' else 'read'
        await self._access(context,tx,transfer.target.id,operation,check)
        return transfer

    def _limits(self,tx,id):
        return loads(tx.one('SELECT limits FROM transfers WHERE id=?',(id,))[0])

    async def open(self,context,direction,target,size,digest,expires_at, *, limits=None,media_type='application/octet-stream'):
        require(context.principal.subject is not None,'authentication_required')
        require(direction in {'upload','download'},'invalid_direction')
        require(self.clock()<expires_at<=self.clock()+timedelta(seconds=self.ttl),'invalid_transfer_expiry')
        require(size is None or type(size) is int and 0<=size<2**63,'invalid_size')
        if digest is not None:
            validate_digest(digest)
        require(isinstance(media_type,str) and 0<len(media_type)<=255 and '\r' not in media_type and '\n' not in media_type,
                'invalid_media_type')
        async with self.metadata.transaction(write=True) as tx:
            require_transfer_capacity(tx, new_transfer=True)
            if direction=='download':
                require(target is not None,'target_required')
                await self._access(context,tx,target.id,'transfer.open','read')
                revision=await tx.revision(target)
                target=ResourceRef(id=target.id,revision=revision.id)
                size,digest=revision.content.size,revision.content.digest
                media_type=revision.content.media_type
            else:
                if target is None:
                    row=tx.one('SELECT id FROM resources WHERE parent=? AND name=?',(context.principal.subject,'files'))
                    require(row is not None,'upload_parent_required')
                    target=ResourceRef(id=row[0])
                require(target.revision is None,'upload_target_must_be_container')
                parent=await tx.resource(target.id)
                require(parent.type in {'topic','user','organization'},'not_a_container')
                await self._access(context,tx,target.id,'transfer.open','create')
            transfer=TransferSession(id='tr_'+uuid4().hex,subject_id=context.principal.subject,direction=direction,
                state='open',target=target,expected_size=size,expected_digest=digest,expires_at=expires_at,generation=0)
            await tx.save_transfer(transfer,None)
            options=dict(limits or {},media_type=media_type)
            options.setdefault('part_bytes',self.part_bytes)
            tx.execute('UPDATE transfers SET limits=? WHERE id=?',(canonical(options).decode(),transfer.id),write=True)
            return transfer

    async def put(self,context,transfer_id,offset,data,digest):
        validate_digest(digest)
        require(type(offset) is int and 0<=offset<2**63,'invalid_offset')
        require(isinstance(data,bytes) and data,'empty_chunk')
        require('sha256:'+hashlib.sha256(data).hexdigest()==digest,'chunk_digest_mismatch')
        async with self.metadata.transaction(write=True) as tx:
            transfer=await self._session(context,tx,transfer_id,'transfer.part_put')
            require(transfer.direction=='upload','wrong_transfer_direction')
            limits=self._limits(tx,transfer_id)
            require(len(data)<=limits['part_bytes'],'part_too_large')
            require(offset+len(data)<2**63,'invalid_offset')
            require(transfer.expected_size is None or offset+len(data)<=transfer.expected_size,'part_out_of_bounds')
            # Equality includes the range and bytes' digest, not their arrival order.
            rows=tx.rows('SELECT offset,length,body FROM chunks WHERE transfer_id=? AND offset<? AND offset+length>?',
                         (transfer_id,offset+len(data),offset))
            if rows:
                require(len(rows)==1 and rows[0][0]==offset and rows[0][1]==len(data) and
                    decode(TransferChunk,loads(rows[0][2])).content.digest==digest,'chunk_conflict')
                return decode(TransferChunk,loads(rows[0][2]))
            require(transfer.state=='open','transfer_closed')
            require_transfer_capacity(tx, len(data))
            async def pieces():
                yield data
            blob=await self.contents.put(pieces(),'application/octet-stream',expected_digest=digest)
            await self.contents.pin(blob,transfer_id+':'+str(offset))
            chunk=TransferChunk(transfer_id=transfer_id,offset=offset,content=blob)
            await tx.put_chunk(chunk)
            await tx.save_transfer(replace(transfer,generation=transfer.generation+1),transfer.generation)
            return chunk

    async def get(self,context,transfer_id,byte_range):
        require(isinstance(byte_range,tuple) and len(byte_range)==2 and
            all(type(i) is int for i in byte_range),'invalid_byte_range')
        start,end=byte_range
        async with self.metadata.transaction(write=False) as tx:
            transfer=await self._session(context,tx,transfer_id,'transfer.part_get')
            require(transfer.direction=='download','wrong_transfer_direction')
            require(0<=start<=end<=transfer.expected_size,'part_out_of_bounds')
            limits=self._limits(tx,transfer_id)
            require(end-start<=limits['part_bytes'],'part_too_large')
            revision=await tx.revision(transfer.target)
            data=b''.join([part async for part in self.contents.read(revision.content,(start,end))])
            from msg.core.models import BlobRef
            chunk=TransferChunk(transfer_id=transfer_id,offset=start,
                content=BlobRef(digest=digest(data),size=len(data),media_type=revision.content.media_type))
            return chunk,data

    async def status(self,context,transfer_id,cursor=None,limit=50):
        async with self.metadata.transaction(write=False) as tx:
            transfer=await self._session(context,tx,transfer_id,'transfer.status',allow_cancelled=True)
            if transfer.direction=='download' or transfer.state=='sealed':
                return Page(items=())
            if transfer.expected_size is None:
                # An unknown final length cannot be described as a closed missing
                # interval. Expose received ranges through the operation projection.
                return Page(items=())
            return await tx.missing_ranges(transfer_id,cursor,limit)

    async def seal(self,context,transfer_id,final_size,final_digest):
        validate_digest(final_digest)
        require(type(final_size) is int and 0<=final_size<2**63,'invalid_size')
        async with self.metadata.transaction(write=True) as tx:
            transfer=await self._session(context,tx,transfer_id,'transfer.seal')
            require(transfer.expected_size is None or final_size==transfer.expected_size,'final_size_mismatch')
            require(transfer.expected_digest is None or final_digest==transfer.expected_digest,'final_digest_mismatch')
            if transfer.state=='sealed':
                require(transfer.expected_size==final_size and transfer.expected_digest==final_digest,'seal_conflict')
                return transfer.output
            require(transfer.state=='open','transfer_closed')
            if transfer.direction=='download':
                output=transfer.target
            else:
                position=0
                # Rows are streamed from SQLite, not collected into an unbounded list.
                for start,length in tx.execute('SELECT offset,length FROM chunks WHERE transfer_id=? ORDER BY offset',(transfer_id,)):
                    require(start==position,'transfer_incomplete')
                    position=start+length
                require(position==final_size,'transfer_incomplete')
                async def pieces():
                    for (body,) in tx.execute('SELECT body FROM chunks WHERE transfer_id=? ORDER BY offset',(transfer_id,)):
                        chunk=decode(TransferChunk,loads(body))
                        async for data in self.contents.read(chunk.content):
                            yield data
                limits=self._limits(tx,transfer_id)
                blob=await self.contents.put(pieces(),limits['media_type'],expected_digest=final_digest)
                require(blob.size==final_size,'final_size_mismatch')
                await self.contents.pin(blob,transfer_id+':sealed')
                output=await self.publish(context,tx,transfer.target.id,blob)
            await tx.save_transfer(replace(transfer,state='sealed',expected_size=final_size,expected_digest=final_digest,
                output=output,generation=transfer.generation+1),transfer.generation)
            return output

    async def cancel(self,context,transfer_id):
        async with self.metadata.transaction(write=True) as tx:
            transfer=await self._session(context,tx,transfer_id,'transfer.cancel',allow_cancelled=True)
            if transfer.state=='cancelled':
                return
            require(transfer.state=='open','transfer_closed')
            await tx.save_transfer(replace(transfer,state='cancelled',generation=transfer.generation+1),transfer.generation)
            # Physical chunk cleanup is separate from this SQL transaction. Its
            # failure cannot invalidate a cancellation that has already committed.
            tx.set_setting('transfer_cleanup:'+transfer.id,{'state':'pending','after':wire(self.clock())})
