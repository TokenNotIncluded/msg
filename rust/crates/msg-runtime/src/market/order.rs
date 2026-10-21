//! Immutable checkout facts, signed Order resources, and guarded escrow changes.
use super::{field, hash, integer, ledger, require, text, time};
use crate::context::new_id;
use chrono::Duration;
use msg_core::{digest, unb64, Error, Json, Result};
use msg_crypto::{verify, Ed25519Signer};
use msg_identity::{
    models::{CredentialKind, ResourceRef, Timestamp},
    store::AuthorityStore,
};
use msg_protocol::Request;
use msg_storage::{
    content::{recovery::ContentTransaction, GitContentStore},
    postgres::{PgSession, Row, Value},
    records::{decode, encode, Event, Resource, ResourceState, Revision, SourceKind},
};
use serde::Serialize;

#[derive(Clone, Serialize)]
pub(super) struct Order {
    pub id: String,
    pub buyer: String,
    pub seller: String,
    pub listing_id: String,
    pub listing_revision: String,
    pub package_id: Option<String>,
    pub package_revision: Option<String>,
    pub package_digest: Option<String>,
    pub quantity: i64,
    pub unit_price_minor: i64,
    pub total_price_minor: i64,
    pub currency_id: String,
    pub escrow_subject: String,
    pub escrow_policy: String,
    pub dispute_policy: String,
    pub terms_digest: String,
    pub delivery_target: Json,
    pub payment_intent_digest: Option<String>,
    pub payment_transaction_id: Option<String>,
    pub state: String,
    pub created_at: String,
    pub funded_at: Option<String>,
    pub delivered_at: Option<String>,
    pub settled_at: Option<String>,
    pub receipt_refs: Vec<String>,
}
pub(super) fn read(tx: &PgSession, id: &str) -> Result<Order> {
    let r = tx.one("SELECT id,buyer,seller,listing_id,listing_revision,package_id,package_revision,package_digest,quantity,unit_price_minor,total_price_minor,currency_id,escrow_subject,escrow_policy,dispute_policy,terms_digest,delivery_target,payment_intent_digest,payment_transaction_id,state,created_at,funded_at,delivered_at,settled_at,receipt_refs FROM store_orders WHERE id=$1",&[id.into()])?.ok_or(Error("order_not_found"))?;
    Ok(Order {
        id: r.text(0)?.into(),
        buyer: r.text(1)?.into(),
        seller: r.text(2)?.into(),
        listing_id: r.text(3)?.into(),
        listing_revision: r.text(4)?.into(),
        package_id: r.optional_text(5)?.map(str::to_owned),
        package_revision: r.optional_text(6)?.map(str::to_owned),
        package_digest: r.optional_text(7)?.map(str::to_owned),
        quantity: r.integer(8)?,
        unit_price_minor: r.integer(9)?,
        total_price_minor: r.integer(10)?,
        currency_id: r.text(11)?.into(),
        escrow_subject: r.text(12)?.into(),
        escrow_policy: r.text(13)?.into(),
        dispute_policy: r.text(14)?.into(),
        terms_digest: r.text(15)?.into(),
        delivery_target: r.json(16)?,
        payment_intent_digest: r.optional_text(17)?.map(str::to_owned),
        payment_transaction_id: r.optional_text(18)?.map(str::to_owned),
        state: r.text(19)?.into(),
        created_at: r.text(20)?.into(),
        funded_at: r.optional_text(21)?.map(str::to_owned),
        delivered_at: r.optional_text(22)?.map(str::to_owned),
        settled_at: r.optional_text(23)?.map(str::to_owned),
        receipt_refs: decode(&r.json(24)?)?,
    })
}
fn nullable(value: &Value) -> Result<Json> {
    match value {
        Value::Null => Json::parse("null"),
        Value::Bool(v) => encode(v),
        Value::Int(v) => encode(v),
        Value::Text(v) => Ok(Json::string(v)),
        _ => Err(Error("storage_column_type")),
    }
}
fn row_array(row: &Row) -> Result<Json> {
    let values = (0..row.len())
        .map(|i| nullable(row.value(i)?))
        .collect::<Result<Vec<_>>>()?;
    encode(&values)
}
pub(super) fn get_contract(tx: &PgSession, id: &str) -> Result<Json> {
    let row = tx
        .one(
            "SELECT body,digest FROM order_contracts WHERE order_id=$1",
            &[id.into()],
        )?
        .ok_or(Error("order_contract_required"))?;
    let value = row.json(0)?;
    require(
        hash(&value)? == row.text(1)? && text(&value, "order_id")? == id,
        "order_contract_corrupt",
    )?;
    Ok(value)
}
fn entitlement_policy() -> serde_json::Value {
    serde_json::json!({"id":"deterministic-entitlement-v1","version":1,"funding_timeout_seconds":900,"delivery_timeout_seconds":900})
}

fn entitlement_intent(tx: &PgSession, locked: &Json) -> Result<String> {
    let policy = entitlement_policy();
    require(
        field(locked, "policy")?
            == &encode(&serde_json::json!({"policy":policy,"policy_digest":hash(&policy)?}))?,
        "order_contract_version",
    )?;
    require(
        field(locked, "settlement_policy")?
            == &Json::parse(r#"{"id":"deterministic-entitlement-v1","version":1}"#)?
            && text(locked, "seller")? == "u_root"
            && text(field(locked, "listing")?, "item_kind")? == "entitlement",
        "order_contract_version",
    )?;
    let signed = Request::parse(&field(locked, "redemption_request")?.canonical()?)?;
    let quote = field(locked, "offer_snapshot")?;
    require(
        signed.field("operation")?.as_str()? == "money.redeem"
            && signed.field("contract_version")?.as_integer()? == "3"
            && signed.field("subject")? == field(locked, "buyer")?
            && signed.field("request_id")? == field(locked, "redemption_request_id")?
            && signed.payload_digest()? == signed.field("payload_digest")?.as_str()?,
        "order_entitlement_intent_mismatch",
    )?;
    require(
        signed.field("arguments")?
            == &encode(
                &serde_json::json!({"offer_id":field(locked,"listing_id")?,"quantity":field(locked,"quantity")?,"currency_id":"primary","price_revision":field(quote,"price_revision")?,"listing_revision":field(locked,"listing_revision")?,"offer_snapshot_digest":hash(quote)?,"total_price_minor":field(locked,"total_price_minor")?,"settlement_policy":"deterministic-entitlement-v1"}),
            )?,
        "order_entitlement_intent_mismatch",
    )?;
    let signature = field(signed.field("proof")?, "signature")?;
    let credential = tx
        .credential(text(signature, "key_id")?)
        .map_err(|e| match e.0 {
            "credential_not_found" => Error("order_entitlement_signer_mismatch"),
            _ => e,
        })?;
    require(
        credential.subject_id == text(locked, "buyer")?
            && credential.kind == CredentialKind::SigningKey
            && credential.verifier.0 == unb64(text(locked, "redemption_signer")?, 32)?,
        "order_entitlement_signer_mismatch",
    )?;
    // This verifies a historical signed fact; current revocation controls new
    // execution elsewhere and must not rewrite an already committed receipt.
    signed.verify_signature_bytes(&credential.verifier.0)?;
    Ok(signed.field("payload_digest")?.as_str()?.into())
}
pub(super) fn delivery_snapshot(tx: &PgSession, order_id: &str) -> Result<String> {
    let row = tx.one("SELECT id,order_id,recipient_subject,kind,payload_refs,manifest,package_digest,delivery_digest,channel,prepared_at FROM store_deliveries WHERE order_id=$1",&[order_id.into()])?;
    let Some(row) = row else {
        return hash(&serde_json::Value::Null);
    };
    hash(
        &serde_json::json!({"id":row.text(0)?,"order_id":row.text(1)?,"recipient_subject":row.text(2)?,"kind":row.text(3)?,"payload_refs":row.json(4)?,"manifest":row.json(5)?,"package_digest":row.optional_text(6)?,"delivery_digest":row.text(7)?,"channel":row.text(8)?,"prepared_at":row.text(9)?}),
    )
}
pub(super) fn validate_projection(tx: &PgSession, order: &Order, locked: &Json) -> Result<()> {
    let version = integer(locked, "version")?;
    require(matches!(version, 3..=5), "order_contract_version")?;
    if version == 4 {
        require(
            field(locked, "settlement_policy")?
                == &Json::parse(r#"{"id":"explicit-buyer-acceptance","version":1}"#)?,
            "order_contract_version",
        )?;
    }
    if version == 5 {
        require(
            order.payment_intent_digest.as_deref() == Some(&entitlement_intent(tx, locked)?),
            "order_entitlement_intent_mismatch",
        )?;
    }
    let record = encode(order)?;
    for name in [
        "buyer",
        "seller",
        "listing_id",
        "listing_revision",
        "package_id",
        "package_revision",
        "package_digest",
        "quantity",
        "total_price_minor",
        "escrow_subject",
        "terms_digest",
        "created_at",
    ] {
        require(
            field(&record, name)? == field(locked, name)?,
            "order_contract_mismatch",
        )?;
    }
    let listing = field(locked, "listing")?;
    for (column, name) in [
        ("unit_price_minor", "price_minor"),
        ("currency_id", "currency_id"),
        ("escrow_policy", "escrow_policy"),
        ("dispute_policy", "dispute_policy"),
    ] {
        require(
            field(&record, column)? == field(listing, name)?,
            "order_contract_mismatch",
        )?;
    }
    if let Some(payment) = &order.payment_transaction_id {
        let row = tx.one("SELECT debit_account,credit_account,amount_minor,currency_id,reference,committed_at FROM money_ledger WHERE id=$1",&[payment.as_str().into()])?.ok_or(Error("order_payment_mismatch"))?;
        require(
            row_array(&row)?
                == encode(&serde_json::json!([
                    order.buyer,
                    order.escrow_subject,
                    order.total_price_minor,
                    order.currency_id,
                    format!("order_fund:{}", order.id),
                    order.funded_at
                ]))?,
            "order_payment_mismatch",
        )?;
    }
    let fact = tx.one(
        "SELECT body FROM order_settlements WHERE order_id=$1",
        &[order.id.as_str().into()],
    )?;
    let Some(fact) = fact else {
        return require(
            !matches!(order.state.as_str(), "settled" | "refunded"),
            "order_settlement_missing",
        );
    };
    let fact = fact.json(0)?;
    let refund = integer(&fact, "refund_minor")?;
    let release = integer(&fact, "release_minor")?;
    require(
        text(&fact, "order_id")? == order.id && refund >= 0 && release >= 0,
        "order_settlement_mismatch",
    )?;
    let receipts = field(&fact, "receipts")?.as_array()?;
    let legs: Vec<_> = [
        (
            "refund",
            order.buyer.as_str(),
            refund,
            format!("order_refund:{}", order.id),
        ),
        (
            "transfer",
            order.seller.as_str(),
            release,
            format!("order_release:{}", order.id),
        ),
    ]
    .into_iter()
    .filter(|(_, _, amount, _)| *amount != 0)
    .collect();
    require(receipts.len() == legs.len(), "order_settlement_mismatch")?;
    let mut references = vec![encode(&order.payment_transaction_id)?];
    for (receipt, (kind, recipient, amount, reference)) in receipts.iter().zip(legs) {
        let body = field(receipt, "body")?;
        require(
            text(body, "kind")? == kind
                && text(body, "from_subject")? == order.escrow_subject
                && text(body, "to_subject")? == recipient
                && integer(body, "amount_minor")? == amount
                && text(body, "currency_id")? == order.currency_id
                && text(body, "reference")? == reference,
            "order_settlement_mismatch",
        )?;
        references.push(field(body, "transaction_id")?.clone());
    }
    require(
        refund.checked_add(release) == Some(order.total_price_minor)
            && order.state == (if release == 0 { "refunded" } else { "settled" })
            && order.settled_at.as_deref() == Some(text(&fact, "at")?)
            && encode(&order.receipt_refs)? == encode(&references)?
            && delivery_snapshot(tx, &order.id)? == text(&fact, "delivery_snapshot")?,
        "order_settlement_mismatch",
    )?;
    for (name, value) in field(&fact, "order_facts")?.as_object()? {
        require(field(&record, name)? == value, "order_settlement_mismatch")?;
    }
    if version == 4 && field(&fact, "decision_id")?.is_null() && release != 0 {
        require(
            text(&fact, "reason")? == "buyer_acceptance" && refund == 0,
            "order_acceptance_policy_mismatch",
        )?;
    }
    if version == 5 {
        require(
            order.state == "settled"
                && text(&fact, "reason")? == "deterministic_entitlement"
                && refund == 0
                && order.delivered_at.is_none()
                && delivery_snapshot(tx, &order.id)? == hash(&serde_json::Value::Null)?,
            "order_entitlement_mismatch",
        )?;
        let entitlement = text(&fact, "entitlement_id")?;
        let row = tx.one("SELECT subject_id,offer_id,purchase_request_id,quantity,entitlement_kind,redeem_transaction_id,granted_at,expires_at FROM resource_entitlements WHERE id=$1",&[entitlement.into()])?.ok_or(Error("order_entitlement_mismatch"))?;
        let quote = field(locked, "offer_snapshot")?;
        let expiry = if field(quote, "duration_seconds")?.is_null() {
            None
        } else {
            Some(time(add(
                Timestamp::parse(text(&fact, "at")?)?,
                integer(quote, "duration_seconds")?,
            )?))
        };
        let first = receipts
            .first()
            .ok_or(Error("order_entitlement_mismatch"))?;
        require(
            row_array(&row)?
                == encode(&serde_json::json!([
                    order.buyer,
                    order.listing_id,
                    field(locked, "redemption_request_id")?,
                    order.quantity,
                    field(quote, "entitlement_kind")?,
                    text(field(first, "body")?, "transaction_id")?,
                    text(&fact, "at")?,
                    expiry
                ]))?,
            "order_entitlement_mismatch",
        )?;
    }
    for receipt in receipts {
        let row = tx
            .one(
                "SELECT receipt FROM money_ledger WHERE id=$1",
                &[text(field(receipt, "body")?, "transaction_id")?.into()],
            )?
            .ok_or(Error("order_settlement_mismatch"))?;
        require(row.json(0)? == *receipt, "order_settlement_mismatch")?;
    }
    Ok(())
}

fn snapshot(tx: &PgSession, order: &Order, locked: &Json) -> Result<Json> {
    let mut record = encode(order)?.into_object()?;
    record.remove("delivery_target");
    let settlement = tx
        .one(
            "SELECT body FROM order_settlements WHERE order_id=$1",
            &[order.id.as_str().into()],
        )?
        .map(|r| r.json(0))
        .transpose()?;
    let transitions = tx
        .rows(
            "SELECT body FROM order_transitions WHERE order_id=$1 ORDER BY id",
            &[order.id.as_str().into()],
        )?
        .iter()
        .map(|r| r.json(0))
        .collect::<Result<Vec<_>>>()?;
    encode(
        &serde_json::json!({"model":1,"order":Json::from_object(record),"contract":locked,"settlement":settlement,"transitions":transitions}),
    )
}
fn manifest(revision: &Revision) -> Result<Json> {
    let mut fields = encode(revision)?.into_object()?;
    fields.remove("manifest_digest");
    fields.remove("signature");
    Ok(Json::from_object(fields))
}
pub(super) fn verify_source(
    tx: &PgSession,
    contents: &GitContentStore,
    signer: &Ed25519Signer,
    order: &Order,
    locked: &Json,
) -> Result<Option<Resource>> {
    if locked
        .as_object()?
        .get("resource_model")
        .and_then(|v| v.as_integer().ok())
        != Some("1")
    {
        require(
            tx.one(
                "SELECT 1 FROM resources WHERE id=$1 AND type='order'",
                &[order.id.as_str().into()],
            )?
            .is_none(),
            "order_resource_contract_missing",
        )?;
        return Ok(None);
    }
    let resource = tx.resource_record(&order.id).map_err(|e| match e.0 {
        "not_found" | "revision_not_found" => Error("order_resource_missing"),
        _ => e,
    })?;
    require(
        resource.resource_type == "order"
            && resource.type_version == 1
            && resource.owner == order.buyer
            && resource.name == order.id
            && resource.parent.as_deref() == Some(format!("orders_{}", order.buyer).as_str())
            && resource.mode == 0o400
            && resource.state == ResourceState::Active,
        "order_resource_invalid",
    )?;
    let revision = tx
        .revision(&ResourceRef {
            id: order.id.clone(),
            revision: resource.revision.clone(),
        })
        .map_err(|e| match e.0 {
            "not_found" | "revision_not_found" => Error("order_resource_missing"),
            _ => e,
        })?;
    let manifest = manifest(&revision)?;
    let snapshot = snapshot(tx, order, locked)?.canonical()?;
    require(
        hash(&manifest)? == revision.manifest_digest
            && revision.signature.is_some()
            && revision.content.digest == digest(snapshot.as_bytes()),
        "order_resource_projection_mismatch",
    )?;
    verify(
        &signer.public_key(),
        manifest.canonical()?.as_bytes(),
        revision
            .signature
            .as_ref()
            .ok_or(Error("order_resource_projection_mismatch"))?,
        "revision",
    )?;
    require(
        contents.read_bytes(&revision.content, msg_core::MAX_BYTES as u64)? == snapshot.as_bytes(),
        "order_resource_projection_mismatch",
    )?;
    Ok(Some(resource))
}

/// Constructed only after checking the old signed source. Its private fields
/// replace Python's transaction-local mutable set of admitted order IDs.
pub(super) struct Mutation {
    pub order: Order,
    pub locked: Json,
    resource: Option<Resource>,
}
impl Mutation {
    pub(super) fn begin(
        tx: &PgSession,
        contents: &GitContentStore,
        signer: &Ed25519Signer,
        id: &str,
    ) -> Result<Self> {
        let order = read(tx, id)?;
        let locked = get_contract(tx, id)?;
        let resource = verify_source(tx, contents, signer, &order, &locked)?;
        validate_projection(tx, &order, &locked)?;
        Ok(Self {
            order,
            locked,
            resource,
        })
    }
    pub(super) fn publish(
        self,
        unit: &mut ContentTransaction<'_, &PgSession>,
        signer: &Ed25519Signer,
        now: Timestamp,
        actor: &str,
        request: &str,
    ) -> Result<()> {
        let Some(mut resource) = self.resource else {
            return Ok(());
        };
        let tx = *unit.metadata();
        let order = read(tx, &self.order.id)?;
        validate_projection(tx, &order, &self.locked)?;
        let body = snapshot(tx, &order, &self.locked)?.canonical()?;
        let current = tx.revision(&ResourceRef {
            id: order.id.clone(),
            revision: resource.revision.clone(),
        })?;
        if current.content.digest == digest(body.as_bytes()) {
            return Ok(());
        }
        let blob = unit.put_bytes(body.as_bytes(), "application/json", None)?;
        let mut revision = Revision {
            format_version: 1,
            id: new_id("v")?,
            resource_id: order.id.clone(),
            parents: resource.revision.clone().into_iter().collect(),
            content: blob,
            relations: vec![],
            actor: actor.into(),
            subject: actor.into(),
            author: actor.into(),
            created_at: now,
            manifest_digest: String::new(),
            signature: None,
            summary: None,
            change_note: None,
            source_kind: Some(SourceKind::Operation),
            source_version: Some(1),
            source_digest: Some(hash(
                &serde_json::json!({"request_id":request,"order_id":order.id}),
            )?),
            signature_source: None,
        };
        let manifest = manifest(&revision)?.canonical()?;
        revision.manifest_digest = digest(manifest.as_bytes());
        revision.signature = Some(signer.sign(manifest.as_bytes(), "revision")?);
        unit.pin(&revision.content, &revision.id)?;
        unit.commit_revision(
            resource
                .parent
                .as_deref()
                .ok_or(Error("order_resource_invalid"))?,
            &revision,
        )?;
        tx.append_revision(&revision)?;
        let generation = resource.generation;
        resource.revision = Some(revision.id);
        resource.generation = generation
            .checked_add(1)
            .ok_or(Error("invalid_generation"))?;
        resource.modified_at = now;
        resource.modified_by = actor.into();
        tx.replace_resource(&resource, generation)
    }
    pub(super) fn transition(
        &mut self,
        tx: &PgSession,
        state: &str,
        now: Timestamp,
        actor: &str,
        request: &str,
        reason: &str,
    ) -> Result<()> {
        let previous = self.order.state.as_str();
        let allowed = match previous {
            "created" => matches!(state, "funded" | "cancelled"),
            "funded" => {
                matches!(state, "delivered" | "refunded" | "disputed")
                    || (state == "settled"
                        && integer(&self.locked, "version")? == 5
                        && reason == "deterministic_entitlement")
            }
            "delivered" => matches!(state, "accepted" | "refunded" | "disputed"),
            "accepted" => state == "settled",
            "disputed" => matches!(state, "refunded" | "settled"),
            _ => false,
        };
        require(allowed, "order_transition_invalid")?;
        require(
            tx.execute(
                "UPDATE store_orders SET state=$1 WHERE id=$2 AND state=$3",
                &[state.into(), self.order.id.as_str().into(), previous.into()],
            )? == 1,
            "order_transition_conflict",
        )?;
        let fact = encode(
            &serde_json::json!({"order_id":self.order.id,"from":previous,"to":state,"at":now,"reason":reason,"policy":self.order.dispute_policy,"actor":actor,"request_id":request}),
        )?;
        tx.execute(
            "INSERT INTO order_transitions(id,order_id,body) VALUES($1,$2,$3)",
            &[
                new_id("ot")?.into(),
                self.order.id.as_str().into(),
                fact.canonical()?.into(),
            ],
        )?;
        tx.append_event(&Event {
            id: new_id("ev")?,
            event_type: format!("orders.{state}"),
            time: now,
            request_id: request.into(),
            actor: Some(actor.into()),
            subject: Some(self.order.buyer.clone()),
            resources: vec![],
            data: encode(&serde_json::json!({"order_id":self.order.id,"state":state}))?
                .into_object()?,
        })?;
        self.order.state = state.into();
        Ok(())
    }
    /// A decision must be validated by arbitration before this private call.
    pub(super) fn settle(
        &mut self,
        tx: &PgSession,
        signer: &Ed25519Signer,
        settlement: Settlement<'_>,
    ) -> Result<Json> {
        let Settlement {
            now,
            actor,
            request,
            reason,
            refund,
            decision,
        } = settlement;
        let decision = decision.map(super::arbitration::ValidatedDecision::value);
        let version = integer(&self.locked, "version")?;
        require(
            !(version == 4 && reason == "managed_instant_verified"),
            "escrow_release_forbidden",
        )?;
        require(
            tx.one(
                "SELECT 1 FROM order_escrow_decisions WHERE order_id=$1",
                &[self.order.id.as_str().into()],
            )?
            .is_none(),
            "order_already_settled",
        )?;
        let total = self.order.total_price_minor;
        require(0 <= refund && refund <= total, "settlement_amount_invalid")?;
        let decision_id = decision.map(|d| text(d, "id")).transpose()?;
        if let Some(row) = tx.one(
            "SELECT body FROM order_settlements WHERE order_id=$1",
            &[self.order.id.as_str().into()],
        )? {
            let existing = row.json(0)?;
            require(
                integer(&existing, "refund_minor")? == refund
                    && text(&existing, "reason")? == reason
                    && field(&existing, "decision_id")? == &encode(&decision_id)?,
                "order_already_settled",
            )?;
            return Ok(field(&existing, "receipts")?.clone());
        }
        // Entitlement checkout uses a separate synchronous fulfillment boundary;
        // timeout/arbitration can never manufacture its callable authorization.
        require(version != 5, "escrow_release_forbidden")?;
        if let Some(decision) = decision {
            require(
                refund == integer(decision, "refund_minor")?,
                "decision_amount_mismatch",
            )?;
        } else if refund == total {
            require(
                matches!(
                    reason,
                    "package_missing"
                        | "package_digest_mismatch"
                        | "delivery_missing"
                        | "delivery_digest_mismatch"
                        | "delivery_timeout"
                        | "buyer_cancelled"
                ) && matches!(self.order.state.as_str(), "funded" | "delivered"),
                "escrow_release_forbidden",
            )?;
        } else {
            if version == 4 {
                require(
                    reason == "buyer_acceptance" && actor == self.order.buyer,
                    "escrow_release_forbidden",
                )?;
            }
            require(
                refund == 0
                    && self.order.state == "accepted"
                    && matches!(reason, "buyer_acceptance" | "managed_instant_verified"),
                "escrow_release_forbidden",
            )?;
        }
        require(
            ledger::balance(tx, &self.order.escrow_subject)? == total,
            "escrow_balance_mismatch",
        )?;
        let mut receipts = Vec::new();
        for (suffix, recipient, amount, kind) in [
            ("refund", self.order.buyer.as_str(), refund, "refund"),
            (
                "release",
                self.order.seller.as_str(),
                total - refund,
                "transfer",
            ),
        ] {
            if amount > 0 {
                receipts.push(ledger::release(
                    tx,
                    signer,
                    ledger::Release {
                        order: &self.order,
                        recipient,
                        amount,
                        actor,
                        request: &format!("{}:{suffix}", self.order.id),
                        reference: &format!("order_{suffix}:{}", self.order.id),
                        kind,
                        now,
                    },
                )?);
            }
        }
        self.transition(
            tx,
            if refund == total {
                "refunded"
            } else {
                "settled"
            },
            now,
            actor,
            request,
            reason,
        )?;
        let mut references = self.order.receipt_refs.clone();
        for receipt in &receipts {
            references.push(text(field(receipt, "body")?, "transaction_id")?.into());
        }
        tx.execute(
            "UPDATE store_orders SET settled_at=$1,receipt_refs=$2 WHERE id=$3",
            &[
                time(now).into(),
                encode(&references)?.canonical()?.into(),
                self.order.id.as_str().into(),
            ],
        )?;
        let statement = encode(
            &serde_json::json!({"order_id":self.order.id,"refund_minor":refund,"release_minor":total-refund,"reason":reason,"decision_id":decision_id,"policy":self.order.dispute_policy,"receipts":receipts,"at":now,"order_facts":{"payment_transaction_id":self.order.payment_transaction_id,"payment_intent_digest":self.order.payment_intent_digest,"funded_at":self.order.funded_at,"delivered_at":self.order.delivered_at},"delivery_snapshot":delivery_snapshot(tx,&self.order.id)?}),
        )?;
        tx.execute(
            "INSERT INTO order_settlements(order_id,decision_id,body) VALUES($1,$2,$3)",
            &[
                self.order.id.as_str().into(),
                decision_id.into(),
                statement.canonical()?.into(),
            ],
        )?;
        if let Some(decision) = decision {
            tx.execute(
                "UPDATE arbitration_cases SET state='executed' WHERE id=$1 AND state='decided'",
                &[text(decision, "case_id")?.into()],
            )?;
        }
        self.order.receipt_refs = references;
        self.order.settled_at = Some(time(now));
        encode(&receipts)
    }
}
pub(super) struct Settlement<'a> {
    pub now: Timestamp,
    pub actor: &'a str,
    pub request: &'a str,
    pub reason: &'a str,
    pub refund: i64,
    pub decision: Option<&'a super::arbitration::ValidatedDecision>,
}
pub(super) fn add(now: Timestamp, seconds: i64) -> Result<Timestamp> {
    now.0
        .checked_add_signed(Duration::try_seconds(seconds).ok_or(Error("invalid_market_record"))?)
        .map(Timestamp)
        .ok_or(Error("invalid_datetime"))
}
pub(super) fn resolve_due(
    unit: &mut ContentTransaction<'_, &PgSession>,
    contents: &GitContentStore,
    signer: &Ed25519Signer,
    now: Timestamp,
) -> Result<bool> {
    let tx = *unit.metadata();
    let rows = tx.rows("SELECT o.id,o.buyer FROM store_orders o JOIN order_deadlines d ON d.order_id=o.id WHERE o.state IN ('created','funded') AND d.expires_at<=$1 ORDER BY d.expires_at,o.id LIMIT 100",&[time(now).into()])?;
    let mut changed = false;
    for row in rows {
        let id = row.text(0)?;
        let buyer = row.text(1)?;
        let mut mutation = Mutation::begin(tx, contents, signer, id)?;
        require(mutation.order.buyer == buyer, "order_not_found")?;
        let policy = field(field(&mutation.locked, "policy")?, "policy")?;
        let created = mutation.order.state == "created";
        let origin = if created {
            mutation.order.created_at.as_str()
        } else {
            mutation
                .order
                .funded_at
                .as_deref()
                .ok_or(Error("order_contract_mismatch"))?
        };
        let seconds = integer(
            policy,
            if created {
                "funding_timeout_seconds"
            } else {
                "delivery_timeout_seconds"
            },
        )?;
        if now < add(Timestamp::parse(origin)?, seconds)? {
            continue;
        }
        let request = format!("timeout:{id}");
        if created {
            mutation.transition(tx, "cancelled", now, buyer, &request, "funding_timeout")?;
        } else {
            let total = mutation.order.total_price_minor;
            mutation.settle(
                tx,
                signer,
                Settlement {
                    now,
                    actor: buyer,
                    request: &request,
                    reason: "delivery_timeout",
                    refund: total,
                    decision: None,
                },
            )?;
        }
        mutation.publish(unit, signer, now, buyer, &request)?;
        changed = true;
    }
    Ok(changed)
}
