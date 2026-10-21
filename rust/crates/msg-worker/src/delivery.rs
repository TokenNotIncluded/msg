//! Reconstruct outbound messages only after current authority and endpoint
//! generations have been checked. Queued body text never grants read access.
use crate::{
    mail::{MailMessage, SmtpSender},
    outbound, AttemptContext, EffectOutcome, EffectResult,
};
use aes_gcm::{
    aead::{Aead, Payload},
    Aes256Gcm, KeyInit, Nonce,
};
use msg_core::{unb64, Error, Json, Result};
use msg_identity::{
    authentication::{Entry, Principal},
    authorization::{AccessRequirement, AuthorizationService, Check},
    models::{ResourceRef, Timestamp},
    store::{truthy, AuthorityStore},
    worker_authority::WorkerAuthority,
};
use msg_storage::{
    postgres::PgSession,
    records::{decode, encode, EffectJob, EmailSettings, Event, ResourceState},
};
use std::collections::{BTreeMap, BTreeSet};
use zeroize::Zeroizing;

pub struct DeliveryService {
    service_url: String,
    origin: String,
    hubs: BTreeSet<String>,
    smtp: SmtpSender,
    authority: WorkerAuthority,
    authorizer: AuthorizationService,
    vault_key: Zeroizing<[u8; 32]>,
}
impl DeliveryService {
    pub fn new(
        service_url: String,
        hubs: BTreeSet<String>,
        smtp: SmtpSender,
        authority: WorkerAuthority,
        authorizer: AuthorizationService,
        vault_key: Zeroizing<[u8; 32]>,
    ) -> Result<Self> {
        let origin = service_origin(&service_url)?;
        for hub in &hubs {
            outbound::Endpoint::parse(hub)?;
        }
        Ok(Self {
            service_url,
            origin,
            hubs,
            smtp,
            authority,
            authorizer,
            vault_key,
        })
    }
    pub fn websub(&mut self, context: &mut AttemptContext<'_, '_>) -> EffectResult<EffectOutcome> {
        let (hub, topic) = context.read(|_, job, _| {
            let hub = argument(job, "hub")?.as_str()?;
            let topic = argument(job, "topic")?.as_str()?;
            require(self.hubs.contains(hub), "websub_hub_removed")?;
            require(
                topic == format!("{}/rss.xml", self.service_url),
                "invalid_websub_topic",
            )?;
            Ok((hub.to_owned(), topic.to_owned()))
        })?;
        outbound::websub(&hub, &topic)
    }
    /// Ordinary account-email jobs. Order notifications use projected_mail and
    /// the market's current order/delivery verifier instead of queued text.
    pub fn mail(&mut self, context: &mut AttemptContext<'_, '_>) -> EffectResult<EffectOutcome> {
        self.projected_mail(context, |tx, _, job, now| {
            require(
                !flag(&job.arguments, "order_notification")?,
                "order_projection_required",
            )?;
            let subject = argument(job, "recipient_subject")?.as_str()?;
            let recipient = argument(job, "recipient")?.as_str()?;
            let row = tx
                .one(
                    "SELECT substring(body,1,1048577) FROM emails WHERE subject=$1",
                    &[subject.into()],
                )?
                .ok_or(Error("email_not_set"))?;
            let email: EmailSettings = decode(&row.json(0)?)?;
            require(
                email.subject_id == subject && email.address.as_deref() == Some(recipient),
                "email_changed",
            )?;
            if flag(&job.arguments, "verification")? {
                let challenge = tx
                    .one(
                        "SELECT expires FROM email_challenges WHERE subject=$1",
                        &[subject.into()],
                    )?
                    .ok_or(Error("email_challenge_expired"))?;
                require(
                    Timestamp::parse(challenge.text(0)?)? > now,
                    "email_challenge_expired",
                )?;
            } else {
                require(
                    email.verified_at.is_some() && email.enabled_events.contains(&job.operation),
                    "notification_disabled",
                )?;
            }
            Ok(MailMessage {
                recipient: recipient.into(),
                subject: argument(job, "subject")?.as_str()?.into(),
                text: argument(job, "text")?.as_str()?.into(),
                delivery_id: job.id.clone(),
            })
        })
    }
    /// The supplied projector must reconstruct a market/order notification from
    /// current immutable order facts. It runs inside this authority snapshot.
    pub fn projected_mail(
        &mut self,
        context: &mut AttemptContext<'_, '_>,
        projection: impl FnOnce(&PgSession, &Principal, &EffectJob, Timestamp) -> Result<MailMessage>,
    ) -> EffectResult<EffectOutcome> {
        require(self.smtp.enabled(), "mail_disabled")?;
        let message = context.read(|tx, job, now| {
            let principal =
                self.authority
                    .current(&job.id, job.attempts, &mut self.authorizer, tx, now)?;
            let message = projection(tx, &principal, job, now)?;
            require(message.delivery_id == job.id, "invalid_delivery_result")?;
            Ok(message)
        })?;
        self.smtp.send(&message)
    }
    pub fn webhook(&mut self, context: &mut AttemptContext<'_, '_>) -> EffectResult<EffectOutcome> {
        let job = context.job().clone();
        let (url,secret,mut envelope)=context.read(|tx,job,now| {
            let principal=self.authority.current(&job.id,job.attempts,&mut self.authorizer,tx,now)?;
            let recipient=argument(job,"recipient_subject")?.as_str()?;
            require(!tx.subject(recipient)?.local_only,"local_only")?;
            let endpoint=tx.one("SELECT url,nonce,ciphertext,enabled,generation FROM webhook_endpoints WHERE subject=$1",&[recipient.into()])?.ok_or(Error("webhook_disabled"))?;
            let generation=integer(argument(job,"endpoint_generation")?)?;
            require(endpoint.integer(3)?==1 && endpoint.integer(4)?==generation,"webhook_disabled")?;
            let mut envelope=BTreeMap::from([
                ("event_id".into(),Json::string(&job.event_id)),
                ("delivery_id".into(),Json::string(&job.id)),
                ("subject_id".into(),Json::string(recipient)),
            ]);
            match job.operation.as_str() {
                "communication.send"=>{
                    let message=tx.one("SELECT sender,recipient,event_id FROM messages WHERE id=$1",&[argument(job,"message_id")?.as_str()?.into()])?.ok_or(Error("webhook_event_missing"))?;
                    require(message.optional_text(0)?==principal.subject() && message.optional_text(1)?==Some(recipient) && message.optional_text(2)?==Some(&job.event_id),"webhook_event_missing")?;
                    envelope.insert("type".into(),Json::string("inbox.reference"));
                }
                "communication.webhook_subscribe"=>self.domain_event(tx,job,&principal,recipient,generation,now,&mut envelope)?,
                "communication.game_webhook_subscribe"=>self.game_event(tx,job,&principal,recipient,generation,now,&mut envelope)?,
                _=>return Err(Error("invalid_webhook_event")),
            }
            let url=endpoint.text(0)?.to_owned(); outbound::Endpoint::parse(&url)?;
            let nonce=unb64(endpoint.text(1)?,12)?; let ciphertext=unb64(endpoint.text(2)?,96)?;
            require(nonce.len()==12,"invalid_webhook_secret")?;
            let aad=format!("webhook-v1\0{recipient}");
            let cipher=Aes256Gcm::new_from_slice(self.vault_key.as_ref()).map_err(|_|Error("invalid_webhook_secret"))?;
            let secret=Zeroizing::new(cipher.decrypt(Nonce::from_slice(&nonce),Payload {msg:&ciphertext,aad:aad.as_bytes()}).map_err(|_|Error("invalid_webhook_secret"))?);
            require((32..=64).contains(&secret.len()),"invalid_webhook_secret")?;
            Ok((url,secret,envelope))
        })?;
        let timestamp = context.now()?.0.timestamp().to_string();
        envelope.insert("timestamp".into(), Json::string(&timestamp));
        let body = Json::from_object(envelope).canonical()?.into_bytes();
        outbound::webhook(&url, &secret, &job.event_id, &job.id, &timestamp, &body)
    }

    #[allow(clippy::too_many_arguments)]
    fn domain_event(
        &mut self,
        tx: &PgSession,
        job: &EffectJob,
        principal: &Principal,
        recipient: &str,
        endpoint_generation: i64,
        now: Timestamp,
        envelope: &mut BTreeMap<String, Json>,
    ) -> Result<()> {
        require(
            principal.subject() == Some(recipient)
                && principal.actor() == Some(recipient)
                && principal.method() == "signature",
            "webhook_subscription_owner",
        )?;
        let rid = argument(job, "resource_id")?.as_str()?;
        let scope_id = argument(job, "scope_id")?.as_str()?;
        let category = argument(job, "category")?.as_str()?;
        let subscription = tx
            .setting(&format!("webhook_subscription:{recipient}:{scope_id}"))?
            .ok_or(Error("webhook_subscription_disabled"))?;
        let subscription = subscription.as_object()?;
        require(
            flag(subscription, "enabled")?
                && integer(field(subscription, "generation")?)?
                    == integer(argument(job, "subscription_generation")?)?
                && integer(field(subscription, "endpoint_generation")?)? == endpoint_generation
                && contains(field(subscription, "events")?, category)?
                && field(
                    field(subscription, "principal")?.as_object()?,
                    "credential_id",
                )?
                .as_str()?
                    == principal.credential_id().unwrap_or(""),
            "webhook_subscription_disabled",
        )?;
        let event = tx
            .one(
                "SELECT substring(body,1,1048577) FROM events WHERE id=$1",
                &[job.event_id.as_str().into()],
            )?
            .ok_or(Error("webhook_event_missing"))?;
        let event: Event = decode(&event.json(0)?)?;
        let expected = match event.event_type.as_str() {
            "content.post_create" | "discussion.reply" => "resource.created",
            "content.post_edit" => "resource.updated",
            _ => return Err(Error("invalid_webhook_event")),
        };
        require(
            event.id == job.event_id
                && category == expected
                && event.resources.iter().any(|r| r.id == rid),
            "invalid_webhook_event",
        )?;
        let scope = tx.resource_record(scope_id)?;
        let resource = tx.resource_record(rid)?;
        require(
            scope.owner == recipient
                && scope.state == ResourceState::Active
                && resource.state == ResourceState::Active
                && (scope.id == resource.id || resource.parent.as_deref() == Some(scope_id)),
            "webhook_scope_changed",
        )?;
        require(
            self.authorizer.has(
                principal,
                "webhook.domain",
                "communication.webhook_subscribe@1",
                scope_id,
                tx,
                now,
            )?,
            "capability_required",
        )?;
        self.authorizer.require(
            principal,
            Entry::Worker,
            now,
            &[
                AccessRequirement {
                    resource_id: scope_id.into(),
                    operation: "communication.webhook_subscribe@1".into(),
                    check: Check::Read,
                },
                AccessRequirement {
                    resource_id: rid.into(),
                    operation: "communication.webhook_subscribe@1".into(),
                    check: Check::Read,
                },
            ],
            None,
            tx,
        )?;
        envelope.insert("resource_id".into(), Json::string(rid));
        envelope.insert("type".into(), Json::string(category));
        envelope.insert(
            "event".into(),
            event_envelope(&self.origin, &event, rid, category)?,
        );
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn game_event(
        &mut self,
        tx: &PgSession,
        job: &EffectJob,
        principal: &Principal,
        recipient: &str,
        endpoint_generation: i64,
        now: Timestamp,
        envelope: &mut BTreeMap<String, Json>,
    ) -> Result<()> {
        require(
            job.arguments
                .keys()
                .map(String::as_str)
                .collect::<BTreeSet<_>>()
                == BTreeSet::from([
                    "contract_version",
                    "recipient_subject",
                    "subscription_generation",
                    "endpoint_generation",
                    "event",
                ])
                && integer(argument(job, "contract_version")?)? == 1,
            "invalid_game_event",
        )?;
        let event = argument(job, "event")?;
        bounded_game_event(event)?;
        require(
            field(event.as_object()?, "id")?.as_str()? == job.event_id,
            "invalid_game_event",
        )?;
        let subscription_generation = integer(argument(job, "subscription_generation")?)?;
        require(
            (1..=9_007_199_254_740_991).contains(&subscription_generation)
                && (1..=9_007_199_254_740_991).contains(&endpoint_generation),
            "invalid_game_subscription",
        )?;
        let owner = tx.resource_record(recipient)?;
        require(
            owner.resource_type == "user"
                && owner.owner == recipient
                && owner.state == ResourceState::Active,
            "game_webhook_owner",
        )?;
        let subscription = tx
            .setting(&format!("game_webhook:{recipient}"))?
            .ok_or(Error("game_webhook_subscription_disabled"))?;
        let subscription = subscription.as_object()?;
        require(
            field(subscription, "enabled")?.as_bool()?
                && integer(field(subscription, "generation")?)? == subscription_generation
                && integer(field(subscription, "endpoint_generation")?)? == endpoint_generation,
            "game_webhook_subscription_disabled",
        )?;
        require(
            principal.actor() == Some(recipient)
                && principal.subject() == Some(recipient)
                && principal.method() == "signature"
                && field(subscription, "principal")? == job.principal_snapshot(),
            "game_webhook_owner",
        )?;
        let category = field(event.as_object()?, "type")?.as_str()?;
        require(
            contains(field(subscription, "events")?, category)?,
            "game_event_not_subscribed",
        )?;
        self.authorizer.require_base(
            principal,
            "communication.game_webhook_subscribe@1",
            recipient,
            tx,
            now,
        )?;
        envelope.insert("type".into(), Json::string(category));
        envelope.insert("event".into(), event.clone());
        Ok(())
    }
}

fn require(value: bool, code: &'static str) -> Result<()> {
    if value {
        Ok(())
    } else {
        Err(Error(code))
    }
}
fn field<'a>(object: &'a BTreeMap<String, Json>, key: &str) -> Result<&'a Json> {
    object.get(key).ok_or(Error("invalid_job_arguments"))
}
fn argument<'a>(job: &'a EffectJob, key: &str) -> Result<&'a Json> {
    field(&job.arguments, key)
}
fn integer(value: &Json) -> Result<i64> {
    value
        .as_integer()?
        .parse()
        .map_err(|_| Error("invalid_job_arguments"))
}
fn flag(object: &BTreeMap<String, Json>, key: &str) -> Result<bool> {
    object
        .get(key)
        .map(truthy)
        .transpose()
        .map(|v| v.unwrap_or(false))
}
fn contains(value: &Json, expected: &str) -> Result<bool> {
    Ok(value
        .as_array()?
        .iter()
        .any(|v| v.as_str().ok() == Some(expected)))
}
fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 160
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'.' | b':' | b'-'))
}
fn resource_address(origin: &str, reference: &ResourceRef) -> Result<Json> {
    require(
        identifier(&reference.id) && reference.revision.as_deref().is_none_or(identifier),
        "invalid_resource_id",
    )?;
    let path = match &reference.revision {
        Some(revision) => format!("/_r/{}/rev/{revision}", reference.id),
        None => format!("/_r/{}/json", reference.id),
    };
    encode(
        &serde_json::json!({"service":origin,"ref":reference,"path":path,"url":format!("{origin}{path}")}),
    )
}
fn event_envelope(origin: &str, event: &Event, rid: &str, category: &str) -> Result<Json> {
    require(identifier(&event.id), "invalid_webhook_event")?;
    let resources = event
        .resources
        .iter()
        .filter(|r| r.id == rid)
        .map(|r| resource_address(origin, r))
        .collect::<Result<Vec<_>>>()?;
    let path = format!("/_e/{}", event.id);
    encode(
        &serde_json::json!({"version":1,"path":path,"url":format!("{origin}{path}"),"id":event.id,"source":origin,"type":category,"operation":event.event_type,"time":event.time,"request_id":event.request_id,"actor":null,"subject":null,"resources":resources}),
    )
}
fn service_origin(service: &str) -> Result<String> {
    require(
        !service.is_empty()
            && service.bytes().all(|c| c > 32 && c != 127)
            && !service.contains(['\\', '?', '#', '@']),
        "invalid_resource_address",
    )?;
    let (scheme, rest) = service
        .split_once("://")
        .ok_or(Error("invalid_resource_address"))?;
    let scheme = scheme.to_ascii_lowercase();
    require(
        matches!(scheme.as_str(), "https" | "http"),
        "invalid_resource_address",
    )?;
    let authority = rest.strip_suffix('/').unwrap_or(rest);
    require(
        !authority.is_empty() && !authority.contains('/'),
        "invalid_resource_address",
    )?;
    let (host, port) = if authority.starts_with('[') {
        let end = authority
            .find(']')
            .ok_or(Error("invalid_resource_address"))?;
        let host = &authority[..=end];
        require(
            host[1..host.len() - 1]
                .parse::<std::net::Ipv6Addr>()
                .is_ok(),
            "invalid_resource_address",
        )?;
        let suffix = &authority[end + 1..];
        (
            host,
            if suffix.is_empty() {
                None
            } else {
                Some(
                    suffix
                        .strip_prefix(':')
                        .ok_or(Error("invalid_resource_address"))?,
                )
            },
        )
    } else if let Some((host, port)) = authority.rsplit_once(':') {
        (host, Some(port))
    } else {
        (authority, None)
    };
    require(!host.is_empty(), "invalid_resource_address")?;
    let port = port
        .map(|p| {
            p.parse::<u16>()
                .ok()
                .filter(|v| *v > 0)
                .ok_or(Error("invalid_resource_address"))
        })
        .transpose()?;
    let default = if scheme == "https" { 443 } else { 80 };
    let suffix = port
        .filter(|p| *p != default)
        .map(|p| format!(":{p}"))
        .unwrap_or_default();
    Ok(format!("{scheme}://{}{suffix}", host.to_ascii_lowercase()))
}
fn bounded_game_event(event: &Json) -> Result<()> {
    let fields = event.as_object()?;
    require(
        ["id", "type", "ship_id", "time_ms"]
            .iter()
            .all(|k| fields.contains_key(*k))
            && fields.keys().all(|k| {
                matches!(
                    k.as_str(),
                    "id" | "type" | "ship_id" | "time_ms" | "region" | "score" | "collected" | "hp"
                )
            }),
        "invalid_game_event",
    )?;
    let id = field(fields, "id")?.as_str()?;
    let ship = field(fields, "ship_id")?.as_str()?;
    let kind = field(fields, "type")?.as_str()?;
    require(
        id.strip_prefix("ge_").is_some_and(|s| {
            !s.is_empty()
                && s.len() <= 156
                && s.bytes()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
        }) && ship.strip_prefix("ship_").is_some_and(|s| {
            s.len() == 24
                && s.bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        }) && matches!(
            kind,
            "game.joined" | "game.left" | "game.region" | "game.collect" | "game.hit"
        ),
        "invalid_game_event",
    )?;
    for key in ["time_ms", "score", "collected"] {
        if let Some(value) = fields.get(key) {
            require(
                (0..=9_007_199_254_740_991).contains(&integer(value)?),
                "invalid_game_event",
            )?;
        }
    }
    if let Some(value) = fields.get("region") {
        require((0..19).contains(&integer(value)?), "invalid_game_event")?;
    }
    if let Some(value) = fields.get("hp") {
        let hp = value
            .canonical()?
            .parse::<f64>()
            .map_err(|_| Error("invalid_game_event"))?;
        require(
            hp.is_finite() && (0.0..=100.0).contains(&hp),
            "invalid_game_event",
        )?;
    }
    require(event.canonical()?.len() <= 2048, "game_event_too_large")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn game_projection_rejects_extra_fields_boolean_numbers_and_foreign_content() {
        let event=Json::parse(r#"{"id":"ge_1","type":"game.hit","ship_id":"ship_0123456789abcdef01234567","time_ms":1,"hp":50.0}"#).unwrap();
        assert!(bounded_game_event(&event).is_ok());
        let mut fields = event.clone().into_object().unwrap();
        fields.insert("private_world".into(), Json::string("secret"));
        assert!(bounded_game_event(&Json::from_object(fields)).is_err());
        let mut fields = event.into_object().unwrap();
        fields.insert("time_ms".into(), Json::parse("true").unwrap());
        assert!(bounded_game_event(&Json::from_object(fields)).is_err());
    }
    #[test]
    fn public_event_envelope_has_only_authorized_references() {
        let event:Event=decode(&Json::parse(r#"{"id":"e_1","type":"content.post_edit","time":"2026-01-01T00:00:00Z","request_id":"q_1","actor":"u_secret","subject":"u_secret","resources":[{"id":"r_allowed","revision":"rev_1"},{"id":"r_secret","revision":null}],"data":{"secret":"private"}}"#).unwrap()).unwrap();
        let envelope = event_envelope(
            "https://example.com",
            &event,
            "r_allowed",
            "resource.updated",
        )
        .unwrap()
        .canonical()
        .unwrap();
        assert!(!envelope.contains("u_secret"));
        assert!(!envelope.contains("r_secret"));
        assert!(!envelope.contains("private"));
        assert!(envelope.contains("r_allowed/rev/rev_1"));
    }
}
