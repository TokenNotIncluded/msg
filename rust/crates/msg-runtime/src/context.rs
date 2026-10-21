//! Per-request authority and transaction. Handlers cannot commit or mint a principal.
use crate::{catalog::Catalog, config::Settings};
use hmac::{Hmac, Mac};
use msg_core::{b64, unb64, Error, Json, Result};
use msg_identity::{
    authentication::{Entry, Principal},
    authorization::{AccessRequirement, AuthorizationService, Check},
    models::{ResourceRef, Timestamp},
};
use msg_protocol::Request;
use msg_storage::{
    content::{
        recovery::{ContentMetadata, ContentTransaction},
        GitContentStore,
    },
    postgres::PgSession,
    records::{BlobRef, Revision},
};
use rand_core::{OsRng, RngCore};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::collections::BTreeMap;

#[derive(Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HandlerOutput {
    #[serde(default)]
    pub resources: Vec<ResourceRef>,
    #[serde(default)]
    pub data: Option<BTreeMap<String, Json>>,
    #[serde(default)]
    pub output: Option<ResourceRef>,
}
pub fn output(data: serde_json::Value, resources: Vec<ResourceRef>) -> Result<HandlerOutput> {
    let data = if data.is_null() {
        None
    } else {
        Some(Json::parse(&data.to_string())?.into_object()?)
    };
    Ok(HandlerOutput {
        resources,
        data,
        output: None,
    })
}
pub fn new_id(prefix: &str) -> Result<String> {
    let mut random = [0_u8; 16];
    OsRng
        .try_fill_bytes(&mut random)
        .map_err(|_| Error("random_unavailable"))?;
    random[6] = random[6] & 0x0f | 0x40;
    random[8] = random[8] & 0x3f | 0x80;
    let hex: String = random.iter().map(|b| format!("{b:02x}")).collect();
    Ok(format!("{prefix}_{hex}"))
}

/// All content writes go through the durable metadata/content recovery unit.
pub trait ContentWrite {
    fn put_bytes(
        &mut self,
        bytes: &[u8],
        media_type: &str,
        expected: Option<&str>,
    ) -> Result<BlobRef>;
    fn pin(&mut self, blob: &BlobRef, lease: &str) -> Result<()>;
    fn unpin(&mut self, blob: &BlobRef, lease: &str) -> Result<()>;
    fn commit_revision(&mut self, topic: &str, revision: &Revision) -> Result<String>;
}
impl<M: ContentMetadata> ContentWrite for ContentTransaction<'_, M> {
    fn put_bytes(
        &mut self,
        bytes: &[u8],
        media_type: &str,
        expected: Option<&str>,
    ) -> Result<BlobRef> {
        ContentTransaction::put_bytes(self, bytes, media_type, expected)
    }
    fn pin(&mut self, blob: &BlobRef, lease: &str) -> Result<()> {
        ContentTransaction::pin(self, blob, lease)
    }
    fn unpin(&mut self, blob: &BlobRef, lease: &str) -> Result<()> {
        ContentTransaction::unpin(self, blob, lease)
    }
    fn commit_revision(&mut self, topic: &str, revision: &Revision) -> Result<String> {
        ContentTransaction::commit_revision(self, topic, revision)
    }
}

pub struct Context<'a> {
    pub tx: &'a PgSession,
    pub contents: &'a GitContentStore,
    pub catalog: &'a Catalog,
    pub principal: &'a Principal,
    pub request: &'a Request,
    pub now: Timestamp,
    pub settings: &'a Settings,
    pub authorizer: &'a mut AuthorizationService,
    pub content_write: Option<&'a mut dyn ContentWrite>,
    pub cursor_key: &'a [u8; 32],
}
impl Context<'_> {
    pub fn args(&self) -> Result<&BTreeMap<String, Json>> {
        self.request.field("arguments")?.as_object()
    }
    pub fn arg(&self, name: &str) -> Result<&Json> {
        self.args()?.get(name).ok_or(Error("missing_argument"))
    }
    pub fn text(&self, name: &str) -> Result<&str> {
        self.arg(name)?.as_str()
    }
    pub fn subject(&self) -> Result<&str> {
        self.principal
            .subject()
            .ok_or(Error("authentication_required"))
    }
    pub fn operation(&self) -> Result<String> {
        Ok(format!(
            "{}@{}",
            self.request.field("operation")?.as_str()?,
            self.request.field("contract_version")?.as_integer()?
        ))
    }
    pub fn resolve(&self, reference: &str, migrated: bool) -> Result<String> {
        if reference.starts_with('/') {
            return self.tx.resolve(reference, migrated);
        }
        if reference.is_empty()
            || reference.len() > 160
            || !reference
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_.:-".contains(&c))
        {
            return Err(Error("invalid_resource_id"));
        }
        Ok(self.tx.resource_record(reference)?.id)
    }
    pub fn check(&mut self, id: &str, check: Check) -> Result<()> {
        let operation = self.operation()?;
        let name = self
            .args()?
            .get("name")
            .and_then(|v| v.as_str().ok())
            .map(str::to_owned);
        self.authorizer.require(
            self.principal,
            Entry::Network,
            self.now,
            &[AccessRequirement {
                resource_id: id.to_owned(),
                operation,
                check,
            }],
            name.as_deref(),
            self.tx,
        )?;
        Ok(())
    }
    pub fn put_bytes(
        &mut self,
        bytes: &[u8],
        media_type: &str,
        expected: Option<&str>,
    ) -> Result<BlobRef> {
        self.content_write
            .as_deref_mut()
            .ok_or(Error("read_only_transaction"))?
            .put_bytes(bytes, media_type, expected)
    }
    pub fn pin(&mut self, blob: &BlobRef, lease: &str) -> Result<()> {
        self.content_write
            .as_deref_mut()
            .ok_or(Error("read_only_transaction"))?
            .pin(blob, lease)
    }
    pub fn unpin(&mut self, blob: &BlobRef, lease: &str) -> Result<()> {
        self.content_write
            .as_deref_mut()
            .ok_or(Error("read_only_transaction"))?
            .unpin(blob, lease)
    }
    pub fn commit_revision(&mut self, topic: &str, revision: &Revision) -> Result<String> {
        self.content_write
            .as_deref_mut()
            .ok_or(Error("read_only_transaction"))?
            .commit_revision(topic, revision)
    }
    pub fn encode_cursor(&self, payload: &Json) -> Result<String> {
        let bytes = payload.canonical()?.into_bytes();
        if bytes.len() > 8192 {
            return Err(Error("invalid_cursor"));
        }
        let mut mac = Hmac::<Sha256>::new_from_slice(self.cursor_key)
            .map_err(|_| Error("invalid_service_key"))?;
        mac.update(b"cursor-v1\0");
        mac.update(&bytes);
        Ok(format!(
            "{}.{}",
            b64(&bytes),
            b64(&mac.finalize().into_bytes())
        ))
    }
    pub fn decode_cursor(&self, cursor: &str) -> Result<Json> {
        if cursor.len() > 8192 || cursor.bytes().filter(|c| *c == b'.').count() != 1 {
            return Err(Error("invalid_cursor"));
        }
        let (encoded, signature) = cursor.split_once('.').ok_or(Error("invalid_cursor"))?;
        let bytes = unb64(encoded, 8192).map_err(|_| Error("invalid_cursor"))?;
        let signature = unb64(signature, 32).map_err(|_| Error("invalid_cursor"))?;
        let mut mac = Hmac::<Sha256>::new_from_slice(self.cursor_key)
            .map_err(|_| Error("invalid_service_key"))?;
        mac.update(b"cursor-v1\0");
        mac.update(&bytes);
        mac.verify_slice(&signature)
            .map_err(|_| Error("invalid_cursor"))?;
        let value = Json::parse(std::str::from_utf8(&bytes).map_err(|_| Error("invalid_cursor"))?)
            .map_err(|_| Error("invalid_cursor"))?;
        if value
            .as_object()?
            .get("v")
            .and_then(|v| v.as_integer().ok())
            != Some("1")
        {
            return Err(Error("invalid_cursor"));
        }
        Ok(value)
    }
}
