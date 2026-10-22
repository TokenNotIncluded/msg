//! Optional buyer-owned address verification and live SMTP projections.
use super::{
    field, hash, integer,
    order::{self, Order},
    require, text, time,
};
use crate::{
    config::Settings,
    context::{new_id, output, Context, HandlerOutput},
};
use msg_core::{b64, Error, Json, Result};
use msg_identity::{
    authentication::Principal,
    authorization::AuthorizationService,
    models::Timestamp,
    store::{truthy, AuthorityStore},
};
use msg_storage::{
    content::GitContentStore,
    postgres::PgSession,
    records::{decode, encode, EffectJob, EmailSettings, JobDraft},
};
use msg_worker::mail::{validate_address, MailMessage};
use rand_core::{OsRng, RngCore};
use serde_json::json;
use subtle::ConstantTimeEq;

pub(super) fn enabled(settings: &Settings) -> bool {
    settings.mail.as_ref().is_some_and(|m| m.enabled)
}
fn mail_account(tx: &PgSession, subject: &str) -> Result<Option<(i64, EmailSettings)>> {
    let Some(row) = tx.one(
        "SELECT generation,body FROM emails WHERE subject=$1",
        &[subject.into()],
    )?
    else {
        return Ok(None);
    };
    let email: EmailSettings = decode(&row.json(1)?)?;
    require(email.subject_id == subject, "delivery_recipient_mismatch")?;
    Ok(Some((row.integer(0)?, email)))
}
fn endpoint(tx: &PgSession, subject: &str) -> Result<Option<(EmailSettings, String)>> {
    mail_account(tx, subject)?
        .map(|(generation, email)| {
            let id = format!(
                "em_{}",
                &hash(&json!([
                    subject,
                    email.address,
                    email.verified_at,
                    generation
                ]))?[7..39]
            );
            Ok((email, id))
        })
        .transpose()
}
pub(super) fn target_for(ctx: &Context<'_>, buyer: &str, address: Option<&str>) -> Result<Json> {
    let handle = ctx.tx.resource_record(buyer)?.name;
    let chosen = if let Some(address) = address {
        validate_address(address)?;
        let endpoint = endpoint(ctx.tx, buyer)?
            .filter(|(email, _)| {
                email.address.as_deref() == Some(address) && email.verified_at.is_some()
            })
            .map(|(_, id)| id);
        json!({"state":if enabled(ctx.settings){"pending"}else{"disabled"},"subject_id":buyer,"address_snapshot":address,"endpoint_id":endpoint})
    } else {
        json!({"state":"disabled"})
    };
    encode(&json!({"subject_id":buyer,"channel":"site","handle_snapshot":handle,"email":chosen}))
}
pub(super) fn save_target(tx: &PgSession, order: &mut Order, target: Json) -> Result<()> {
    tx.execute(
        "UPDATE store_orders SET delivery_target=$1 WHERE id=$2",
        &[target.canonical()?.into(), order.id.as_str().into()],
    )?;
    order.delivery_target = target;
    Ok(())
}
fn chosen(order: &Order) -> Result<Json> {
    Ok(order
        .delivery_target
        .as_object()?
        .get("email")
        .cloned()
        .unwrap_or(Json::parse(r#"{"state":"disabled"}"#)?))
}
fn with_email(order: &Order, email: Json) -> Result<Json> {
    let mut target = order.delivery_target.as_object()?.clone();
    target.insert("email".into(), email);
    Ok(Json::from_object(target))
}
fn save_email(tx: &PgSession, order: &mut Order, email: Json) -> Result<()> {
    let target = with_email(order, email)?;
    save_target(tx, order, target)
}
pub(super) fn validate_target(
    tx: &PgSession,
    order: &Order,
    mail: bool,
    encryption: bool,
) -> Result<Option<String>> {
    require(
        text(&order.delivery_target, "subject_id")? == order.buyer
            && text(&order.delivery_target, "channel")? == "site",
        "delivery_recipient_mismatch",
    )?;
    if encryption {
        let locked = order::get_contract(tx, &order.id)?;
        let key = field(&locked, "recipient_key")?;
        require(!key.is_null(), "delivery_recipient_mismatch")?;
        let r = tx
            .one(
                "SELECT subject,recipient,retired_at FROM encryption_subkeys WHERE key_id=$1",
                &[text(key, "key_id")?.into()],
            )?
            .ok_or(Error("delivery_recipient_mismatch"))?;
        require(
            r.text(0)? == order.buyer
                && r.text(1)? == text(key, "recipient")?
                && r.optional_text(2)?.is_none(),
            "delivery_recipient_mismatch",
        )?;
    }
    if !mail {
        return Ok(None);
    }
    let selected = chosen(order)?;
    let fields = selected.as_object()?;
    require(
        !fields
            .get("notification_disabled")
            .map(truthy)
            .transpose()?
            .unwrap_or(false),
        "delivery_recipient_mismatch",
    )?;
    let id = fields
        .get("endpoint_id")
        .filter(|v| !v.is_null())
        .map(Json::as_str)
        .transpose()?
        .unwrap_or("");
    if id.starts_with("dep_") {
        let row = tx
            .one(
                "SELECT subject,address,active FROM delivery_email_endpoints WHERE id=$1",
                &[id.into()],
            )?
            .ok_or(Error("delivery_recipient_mismatch"))?;
        require(
            row.boolean(2)?
                && row.text(0)? == order.buyer
                && row.text(0)? == text(&selected, "subject_id")?
                && row.text(1)? == text(&selected, "address_snapshot")?,
            "delivery_recipient_mismatch",
        )?;
        validate_address(row.text(1)?)?;
        return Ok(Some(row.text(1)?.into()));
    }
    let (current, live) =
        endpoint(tx, &order.buyer)?.ok_or(Error("delivery_recipient_mismatch"))?;
    require(
        text(&selected, "subject_id")? == order.buyer
            && current.verified_at.is_some()
            && id == live
            && current.address.as_deref() == Some(text(&selected, "address_snapshot")?),
        "delivery_recipient_mismatch",
    )?;
    let address = current
        .address
        .ok_or(Error("delivery_recipient_mismatch"))?;
    validate_address(&address)?;
    Ok(Some(address))
}
pub(super) fn verification_message(ctx: &Context<'_>, order: &mut Order) -> Result<()> {
    let selected = chosen(order)?;
    let chosen = selected.as_object()?;
    if !enabled(ctx.settings)
        || chosen.get("endpoint_id").is_some_and(|v| !v.is_null())
        || !chosen.contains_key("address_snapshot")
    {
        return Ok(());
    }
    let mut random = [0_u8; 32];
    OsRng
        .try_fill_bytes(&mut random)
        .map_err(|_| Error("random_unavailable"))?;
    let token = b64(&random);
    let id = new_id("evc")?;
    ctx.tx.execute("INSERT INTO delivery_email_challenges(id,order_id,subject,address,verifier,expires_at) VALUES($1,$2,$3,$4,$5,$6)",&[id.as_str().into(),order.id.as_str().into(),order.buyer.as_str().into(),text(&selected,"address_snapshot")?.into(),hash(&token)?.into(),time(order::add(ctx.now,900)?).into()])?;
    let mut fields = chosen.clone();
    fields.insert("challenge_id".into(), Json::string(&id));
    fields.insert("state".into(), Json::string("pending"));
    save_email(ctx.tx, order, Json::from_object(fields))?;
    let job=EffectJob::new(ctx.principal,JobDraft{id:new_id("job")?,event_id:id.clone(),kind:"market_email_verify".into(),dedupe_key:format!("market-verify:{id}"),operation:ctx.request.field("operation")?.as_str()?.into(),arguments:encode(&json!({"contract_version":ctx.request.field("contract_version")?,"challenge_id":id,"token":token}))?.into_object()?},ctx.now)?;
    ctx.tx.enqueue(&job)
}
pub(super) fn enqueue(ctx: &Context<'_>, order: &mut Order) -> Result<()> {
    let selected = chosen(order)?;
    let fields = selected.as_object()?;
    let id = fields
        .get("endpoint_id")
        .filter(|v| !v.is_null())
        .map(Json::as_str)
        .transpose()?;
    if !enabled(ctx.settings)
        || id.is_none()
        || fields
            .get("notification_disabled")
            .map(truthy)
            .transpose()?
            .unwrap_or(false)
    {
        return Ok(());
    }
    match validate_target(ctx.tx, order, true, false) {
        Ok(_) => {}
        Err(Error("delivery_recipient_mismatch")) => {
            let mut fields = fields.clone();
            fields.insert("state".into(), Json::string("pending"));
            fields.insert("error".into(), Json::string("delivery_recipient_mismatch"));
            return save_email(ctx.tx, order, Json::from_object(fields));
        }
        Err(error) => return Err(error),
    }
    let id = id.ok_or(Error("delivery_recipient_mismatch"))?;
    let locked = order::get_contract(ctx.tx, &order.id)?;
    let dedupe = format!("market-mail:{}:{id}", order.id);
    if ctx
        .tx
        .one(
            "SELECT 1 FROM jobs WHERE dedupe=$1",
            &[dedupe.as_str().into()],
        )?
        .is_some()
    {
        return Ok(());
    }
    // The snapshot comes only from the verified immutable checkout. It is durable
    // evidence for later worker revalidation; no authenticated Principal is minted.
    let principal = field(&locked, "buyer_principal")?;
    require(
        text(principal, "actor")? == order.buyer && text(principal, "subject")? == order.buyer,
        "delivery_recipient_mismatch",
    )?;
    let job: EffectJob = decode(&encode(
        &json!({"id":new_id("job")?,"event_id":format!("delivery:{}",order.id),"kind":"market_mail","dedupe_key":dedupe,"principal":principal,"operation":"orders.buy","arguments":{"contract_version":field(&locked,"version")?,"order_id":order.id,"endpoint_id":id},"state":"pending","attempts":0,"next_attempt_at":ctx.now,"lease_until":null}),
    )?)?;
    ctx.tx.enqueue(&job)?;
    let mut fields = fields.clone();
    fields.insert("state".into(), Json::string("queued"));
    save_email(ctx.tx, order, Json::from_object(fields))
}
pub(super) fn notification_view(tx: &PgSession, order: &Order) -> Result<Json> {
    let mut selected = chosen(order)?.into_object()?;
    if selected
        .get("notification_disabled")
        .map(truthy)
        .transpose()?
        .unwrap_or(false)
    {
        selected.insert("state".into(), Json::string("disabled"));
        return Ok(Json::from_object(selected));
    }
    let Some(endpoint) = selected
        .get("endpoint_id")
        .filter(|v| !v.is_null())
        .map(Json::as_str)
        .transpose()?
    else {
        return Ok(Json::from_object(selected));
    };
    let dedupe = format!("market-mail:{}:{endpoint}", order.id);
    let row = tx.one(
        "SELECT body FROM jobs WHERE dedupe=$1",
        &[dedupe.as_str().into()],
    )?;
    selected.remove("last_result");
    let Some(row) = row else {
        let state = if selected.get("state").map(Json::as_str).transpose()? == Some("disabled") {
            "disabled"
        } else {
            "pending"
        };
        selected.insert("state".into(), Json::string(state));
        return Ok(Json::from_object(selected));
    };
    let job: EffectJob = decode(&row.json(0)?)?;
    let endpoint = selected
        .get("endpoint_id")
        .ok_or(Error("delivery_recipient_mismatch"))?
        .as_str()?;
    require(
        job.kind == "market_mail"
            && job.dedupe_key == dedupe
            && text(job.principal_snapshot(), "actor")? == order.buyer
            && job.subject()? == order.buyer
            && job
                .arguments
                .get("order_id")
                .map(Json::as_str)
                .transpose()?
                == Some(order.id.as_str())
            && job
                .arguments
                .get("endpoint_id")
                .map(Json::as_str)
                .transpose()?
                == Some(endpoint),
        "delivery_recipient_mismatch",
    )?;
    let status = tx
        .setting(&format!("job_status:{}", job.id))?
        .unwrap_or(Json::parse("{}")?);
    let code = status
        .as_object()?
        .get("code")
        .map(Json::as_str)
        .transpose()?;
    let state = if code == Some("mail_disabled") {
        "disabled"
    } else if job.state == "done" && code == Some("sent") {
        "smtp_accepted"
    } else if matches!(job.state.as_str(), "done" | "uncertain") {
        "delivered_unknown"
    } else if job.state == "failed" {
        "failed"
    } else if code.is_some() {
        "pending"
    } else {
        "queued"
    };
    selected.insert("state".into(), Json::string(state));
    if let Some(code) = code {
        selected.insert("last_result".into(), Json::string(code));
    }
    Ok(Json::from_object(selected))
}
pub(super) fn supports(op: &str, v: u32) -> bool {
    v == 1
        && matches!(
            op,
            "orders.email_verify" | "orders.email_disable" | "orders.email_bind"
        )
}
pub(super) fn handle(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let buyer = super::signed_subject(ctx)?.to_owned();
    let mut order = super::checkout::owned(ctx, ctx.text("order_id")?, &buyer)?;
    require(order.buyer == buyer, "order_not_found")?;
    let selected = chosen(&order)?;
    let mut selected = selected.into_object()?;
    match ctx.request.field("operation")?.as_str()? {
        "orders.email_disable" => {
            if let Some(id) = selected
                .get("endpoint_id")
                .filter(|v| !v.is_null())
                .map(Json::as_str)
                .transpose()?
            {
                if id.starts_with("dep_") {
                    ctx.tx.execute("UPDATE delivery_email_endpoints SET active=FALSE WHERE id=$1 AND subject=$2",&[id.into(),buyer.as_str().into()])?;
                }
            }
            selected.insert("state".into(), Json::string("disabled"));
            selected.insert("notification_disabled".into(), encode(&true)?);
            save_email(ctx.tx, &mut order, Json::from_object(selected))?;
        }
        "orders.email_bind" => {
            order::get_contract(ctx.tx, &order.id)?;
            require(
                matches!(
                    order.state.as_str(),
                    "created" | "funded" | "delivered" | "settled"
                ),
                "order_closed",
            )?;
            require(
                order.delivered_at.is_none()
                    || (selected.get("endpoint_id").is_none_or(Json::is_null)
                        && selected
                            .get("address_snapshot")
                            .map(Json::as_str)
                            .transpose()?
                            == Some(ctx.text("email")?)),
                "delivery_target_locked",
            )?;
            let mut target = target_for(ctx, &buyer, Some(ctx.text("email")?))?.into_object()?;
            target.insert(
                "handle_snapshot".into(),
                field(&order.delivery_target, "handle_snapshot")?.clone(),
            );
            let target = Json::from_object(target);
            require(
                !field(field(&target, "email")?, "endpoint_id")?.is_null(),
                "email_not_verified",
            )?;
            save_target(ctx.tx, &mut order, target)?;
            validate_target(ctx.tx, &order, true, false)?;
            if order.delivered_at.is_some() {
                enqueue(ctx, &mut order)?;
            }
        }
        "orders.email_verify" => {
            let challenge = ctx.text("challenge_id")?;
            let row=ctx.tx.one("SELECT order_id,subject,address,verifier,expires_at,consumed FROM delivery_email_challenges WHERE id=$1",&[challenge.into()])?.ok_or(Error("email_challenge_invalid"))?;
            let digest = hash(&ctx.text("token")?)?;
            require(
                row.text(0)? == order.id
                    && row.text(1)? == buyer
                    && !row.boolean(5)?
                    && Timestamp::parse(row.text(4)?)? > ctx.now
                    && bool::from(row.text(3)?.as_bytes().ct_eq(digest.as_bytes()))
                    && selected.get("challenge_id").map(Json::as_str).transpose()?
                        == Some(challenge)
                    && selected
                        .get("address_snapshot")
                        .map(Json::as_str)
                        .transpose()?
                        == Some(row.text(2)?)
                    && selected.get("endpoint_id").is_none_or(Json::is_null)
                    && matches!(
                        order.state.as_str(),
                        "created" | "funded" | "delivered" | "settled"
                    ),
                "email_challenge_invalid",
            )?;
            let endpoint = new_id("dep")?;
            ctx.tx.execute("INSERT INTO delivery_email_endpoints(id,subject,address,verified_at) VALUES($1,$2,$3,$4)",&[endpoint.as_str().into(),buyer.as_str().into(),row.text(2)?.into(),time(ctx.now).into()])?;
            require(ctx.tx.execute("UPDATE delivery_email_challenges SET consumed=TRUE WHERE id=$1 AND consumed=FALSE",&[challenge.into()])?==1,"email_challenge_invalid")?;
            selected.remove("challenge_id");
            selected.insert("endpoint_id".into(), Json::string(endpoint));
            selected.insert("state".into(), Json::string("pending"));
            save_email(ctx.tx, &mut order, Json::from_object(selected))?;
            validate_target(ctx.tx, &order, true, false)?;
            if order.delivered_at.is_some() {
                enqueue(ctx, &mut order)?;
            }
        }
        _ => return Err(Error("unknown_operation")),
    }
    output(
        json!({"order":super::checkout::view(ctx.tx,&order,&buyer)?}),
        vec![],
    )
}

pub struct MailProjection<'a> {
    pub contents: &'a GitContentStore,
    pub settings: &'a Settings,
}
impl MailProjection<'_> {
    pub fn project(
        &self,
        tx: &PgSession,
        authorizer: &mut AuthorizationService,
        principal: &Principal,
        job: &EffectJob,
        now: Timestamp,
    ) -> Result<MailMessage> {
        require(enabled(self.settings), "mail_disabled")?;
        let args = Json::from_object(job.arguments.clone());
        if job.kind == "market_email_verify" {
            let challenge = text(&args, "challenge_id")?;
            let row=tx.one("SELECT order_id,subject,address,verifier,expires_at,consumed FROM delivery_email_challenges WHERE id=$1",&[challenge.into()])?.ok_or(Error("email_challenge_expired"))?;
            require(
                !row.boolean(5)?
                    && Timestamp::parse(row.text(4)?)? > now
                    && principal.subject() == Some(row.text(1)?)
                    && principal.actor() == principal.subject()
                    && hash(&text(&args, "token")?)? == row.text(3)?,
                "email_challenge_expired",
            )?;
            let order = order::read(tx, row.text(0)?)?;
            let selected = chosen(&order)?;
            require(
                order.buyer == row.text(1)?
                    && text(&selected, "challenge_id")? == challenge
                    && text(&selected, "address_snapshot")? == row.text(2)?
                    && selected
                        .as_object()?
                        .get("endpoint_id")
                        .is_none_or(Json::is_null)
                    && matches!(
                        order.state.as_str(),
                        "created" | "funded" | "delivered" | "settled"
                    ),
                "email_challenge_expired",
            )?;
            return Ok(MailMessage{recipient:row.text(2)?.into(),subject:"msg address verification".into(),text:encode(&json!({"challenge_id":challenge,"token":field(&args,"token")?,"purpose":"verify this address only"}))?.canonical()?,delivery_id:job.id.clone()});
        }
        if job.kind == "mail" {
            return self.legacy(tx, authorizer, principal, job, now);
        }
        require(job.kind == "market_mail", "invalid_delivery_notification")?;
        let order = order::read(tx, text(&args, "order_id")?)?;
        require(
            principal.subject() == Some(order.buyer.as_str())
                && principal.actor() == principal.subject(),
            "delivery_recipient_mismatch",
        )?;
        let locked = order::get_contract(tx, &order.id)?;
        order::verify_source(
            tx,
            self.contents,
            &self.settings.receipt_signer,
            &order,
            &locked,
        )?;
        order::validate_projection(tx, &order, &locked)?;
        let delivery =
            super::delivery::read(tx, &order.id)?.ok_or(Error("delivery_not_available"))?;
        require(
            matches!(order.state.as_str(), "delivered" | "accepted" | "settled")
                && text(&delivery, "recipient_subject")? == order.buyer,
            "delivery_not_available",
        )?;
        require(
            field(&chosen(&order)?, "endpoint_id")? == field(&args, "endpoint_id")?,
            "delivery_recipient_mismatch",
        )?;
        let address = validate_target(
            tx,
            &order,
            true,
            text(field(&locked, "listing")?, "delivery_mode")? == "sealed_manual",
        )?
        .ok_or(Error("delivery_recipient_mismatch"))?;
        Ok(MailMessage{recipient:address,subject:"msg order notification".into(),text:encode(&json!({"order_id":order.id,"handle":field(&locked,"handle_snapshot")?,"pickup":self.pickup(&order.id)?}))?.canonical()?,delivery_id:job.id.clone()})
    }
    fn pickup(&self, id: &str) -> Result<String> {
        crate::config::service_origin(&self.settings.service_url)
            .map_err(|_| Error("invalid_delivery_origin"))?;
        Ok(format!(
            "{}/_orders/{id}/_delivery",
            self.settings.service_url.trim_end_matches('/')
        ))
    }
    fn legacy(
        &self,
        tx: &PgSession,
        authorizer: &mut AuthorizationService,
        principal: &Principal,
        job: &EffectJob,
        now: Timestamp,
    ) -> Result<MailMessage> {
        let args = Json::from_object(job.arguments.clone());
        let version = integer(&args, "contract_version")?;
        require(
            (job.operation == "orders.buy" && version == 2)
                || (job.operation == "delivery.notify" && version == 1),
            "invalid_delivery_notification",
        )?;
        let buyer = text(&args, "recipient_subject")?;
        require(
            principal.actor() == Some(buyer) && principal.subject() == Some(buyer),
            "delivery_recipient_mismatch",
        )?;
        authorizer.require_base(
            principal,
            &format!("{}@{version}", job.operation),
            buyer,
            tx,
            now,
        )?;
        let order = order::read(tx, text(&args, "order_id")?)?;
        require(
            order.buyer == buyer && matches!(order.state.as_str(), "delivered" | "settled"),
            "delivery_recipient_mismatch",
        )?;
        let fact = legacy_fact(tx, &order)?;
        require(
            text(&fact, "job_id")? == job.id && field(&fact, "enabled")?.as_bool()?,
            "notification_disabled",
        )?;
        let delivery =
            super::delivery::read(tx, &order.id)?.ok_or(Error("delivery_not_available"))?;
        super::delivery::verify_managed(tx, self.contents, &order, &delivery)?;
        let address = validate_binding(tx, &order, field(&fact, "endpoint")?)?;
        require(
            text(&fact, "address")? == address,
            "delivery_recipient_mismatch",
        )?;
        Ok(MailMessage {
            recipient: address,
            subject: "msg order ready".into(),
            text: format!(
                "{}\n{}\n{}\n",
                order.id,
                text(&fact, "buyer_handle")?,
                self.pickup(&order.id)?
            ),
            delivery_id: job.id.clone(),
        })
    }
}
pub(super) fn binding(tx: &PgSession, buyer: &str, address: &str) -> Result<Json> {
    let Some((generation, email)) = mail_account(tx, buyer)?
        .filter(|(_, e)| e.address.as_deref() == Some(address) && e.verified_at.is_some())
    else {
        return Json::parse("null");
    };
    validate_address(address)?;
    encode(
        &json!({"endpoint_id":format!("email:{buyer}"),"owner":buyer,"generation":generation,"address_snapshot":address,"verified_at":email.verified_at}),
    )
}
fn validate_binding(tx: &PgSession, order: &Order, value: &Json) -> Result<String> {
    require(
        order.delivery_target == encode(&json!({"subject_id":order.buyer,"channel":"site"}))?
            && text(value, "owner")? == order.buyer,
        "delivery_recipient_mismatch",
    )?;
    let address = text(value, "address_snapshot")?;
    let current = binding(tx, &order.buyer, address)?;
    require(
        !current.is_null() && current == *value,
        "delivery_recipient_mismatch",
    )?;
    Ok(address.into())
}
fn legacy_fact(tx: &PgSession, order: &Order) -> Result<Json> {
    let fact = tx
        .setting(&format!("delivery_notification:{}", order.id))?
        .ok_or(Error("delivery_notification_unavailable"))?;
    require(
        integer(&fact, "version")? == 1
            && text(&fact, "order_id")? == order.id
            && text(&fact, "buyer")? == order.buyer,
        "delivery_notification_unavailable",
    )?;
    require(
        order.delivery_target == encode(&json!({"subject_id":order.buyer,"channel":"site"}))?,
        "delivery_recipient_mismatch",
    )?;
    Ok(fact)
}

pub(super) fn legacy_status(ctx: &Context<'_>, id: &str) -> Result<Json> {
    let Some(fact) = ctx.tx.setting(&format!("delivery_notification:{id}"))? else {
        return encode(&json!({"state":"disabled"}));
    };
    let fields = fact.as_object()?;
    let enabled_fact = field(&fact, "enabled")?.as_bool()?;
    let state = if let Some(job) = fields.get("job_id").filter(|v| !v.is_null()) {
        let row = ctx
            .tx
            .one("SELECT body FROM jobs WHERE id=$1", &[job.as_str()?.into()])?
            .ok_or(Error("job_not_found"))?;
        let job: EffectJob = decode(&row.json(0)?)?;
        if matches!(job.state.as_str(), "pending" | "running")
            && (!enabled_fact || !enabled(ctx.settings))
        {
            "disabled"
        } else {
            match job.state.as_str() {
                "pending" | "running" => "queued",
                "done" => "smtp_accepted",
                "uncertain" => "delivered_unknown",
                "failed" => "failed",
                _ => return Err(Error("invalid_job_state")),
            }
        }
    } else if enabled_fact && enabled(ctx.settings) {
        "pending"
    } else {
        "disabled"
    };
    encode(&json!({"state":state}))
}
pub(super) fn initialize_legacy(
    ctx: &Context<'_>,
    order: &Order,
    address: Option<&str>,
) -> Result<Json> {
    let Some(address) = address else {
        return encode(&json!({"state":"disabled"}));
    };
    require(
        order.delivery_target == encode(&json!({"subject_id":order.buyer,"channel":"site"}))?,
        "delivery_recipient_mismatch",
    )?;
    let fact = encode(
        &json!({"version":1,"order_id":order.id,"buyer":order.buyer,"buyer_handle":ctx.tx.resource_record(&order.buyer)?.name,"address":address,"enabled":true,"endpoint":binding(ctx.tx,&order.buyer,address)?,"job_id":null}),
    )?;
    ctx.tx
        .set_setting(&format!("delivery_notification:{}", order.id), &fact)?;
    queue_legacy(ctx, order, true)
}
pub(super) fn queue_legacy(ctx: &Context<'_>, order: &Order, enable: bool) -> Result<Json> {
    let mut fact = legacy_fact(ctx.tx, order)?.into_object()?;
    require(
        ctx.principal.actor() == Some(order.buyer.as_str())
            && ctx.principal.subject() == ctx.principal.actor(),
        "order_not_found",
    )?;
    require(
        matches!(order.state.as_str(), "delivered" | "settled"),
        "order_not_deliverable",
    )?;
    fact.insert("enabled".into(), encode(&enable)?);
    let key = format!("delivery_notification:{}", order.id);
    ctx.tx.set_setting(&key, &Json::from_object(fact.clone()))?;
    if !enable
        || !enabled(ctx.settings)
        || !fact
            .get("job_id")
            .ok_or(Error("delivery_notification_unavailable"))?
            .is_null()
    {
        return legacy_status(ctx, &order.id);
    }
    if fact
        .get("endpoint")
        .ok_or(Error("delivery_notification_unavailable"))?
        .is_null()
    {
        fact.insert(
            "endpoint".into(),
            binding(
                ctx.tx,
                &order.buyer,
                fact.get("address")
                    .ok_or(Error("delivery_notification_unavailable"))?
                    .as_str()?,
            )?,
        );
    }
    if fact
        .get("endpoint")
        .ok_or(Error("delivery_notification_unavailable"))?
        .is_null()
    {
        return encode(&json!({"state":"pending"}));
    }
    validate_binding(
        ctx.tx,
        order,
        fact.get("endpoint")
            .ok_or(Error("delivery_notification_unavailable"))?,
    )?;
    let id = new_id("job")?;
    fact.insert("job_id".into(), Json::string(&id));
    ctx.tx.set_setting(&key, &Json::from_object(fact))?;
    let job=EffectJob::new(ctx.principal,JobDraft{id,event_id:format!("e_{}",&hash(&json!([order.buyer,ctx.request.field("request_id")?]))?[7..39]),kind:"mail".into(),dedupe_key:format!("order:{}:{}:mail",order.id,order.buyer),operation:ctx.request.field("operation")?.as_str()?.into(),arguments:encode(&json!({"order_id":order.id,"recipient_subject":order.buyer,"contract_version":ctx.request.field("contract_version")?,"order_notification":true}))?.into_object()?},ctx.now)?;
    ctx.tx.enqueue(&job)?;
    encode(&json!({"state":"queued"}))
}
