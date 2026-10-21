use super::{require, Context, HandlerOutput};
use crate::context::output;
use chrono::Duration;
use msg_core::{Error, Json, Result};
use msg_identity::{models::Timestamp, store::AuthorityStore};
use msg_storage::records::encode;
use serde_json::{json, Value as J};

fn limits() -> J {
    json!({"account_hour":5,"account_day":20,"global_hour":30,"global_day":300,"cooldown_seconds":60,"svg_bytes":16384,"text_characters":2000,"text_bytes":8192,"elements":256,"animations":32,"batch_size":1})
}
fn default() -> Result<J> {
    let source: J = serde_json::from_str(include_str!("public_board.json"))
        .map_err(|_| Error("invalid_builtin_data"))?;
    Ok(
        json!({"generation":0,"svg":source["svg"],"text":source["text"],"updated_at":null,"updated_by":null,"history":[]}),
    )
}
fn load(ctx: &Context<'_>, key: &str) -> Result<Option<J>> {
    ctx.tx
        .setting(key)?
        .map(|v| serde_json::to_value(v).map_err(|_| Error("invalid_storage_record")))
        .transpose()
}
fn quota(record: Option<J>, now: Timestamp) -> Result<J> {
    let local = now.0 + Duration::hours(8);
    let hour = local.format("%Y-%m-%dT%H").to_string();
    let day = local.format("%Y-%m-%d").to_string();
    let mut record = record.unwrap_or(json!({}));
    require(record.is_object(), "invalid_public_board_quota")?;
    if record["hour"] != hour {
        record["hour"] = json!(hour);
        record["hour_count"] = json!(0);
    }
    if record["day"] != day {
        record["day"] = json!(day);
        record["day_count"] = json!(0);
    }
    Ok(record)
}
fn projection(mut record: J, own: Option<J>) -> Result<J> {
    let history = record["history"]
        .as_array()
        .ok_or(Error("invalid_public_board"))?
        .iter()
        .map(|item| {
            let mut item = item.clone();
            let obj = item.as_object_mut().ok_or(Error("invalid_public_board"))?;
            obj.remove("svg");
            obj.remove("text");
            Ok(item)
        })
        .collect::<Result<Vec<_>>>()?;
    record["history"] = json!(history);
    record["limits"] = limits();
    record["timezone"] = json!("Asia/Taipei");
    record["quota"] = json!(own);
    Ok(record)
}
pub(crate) fn get(ctx: &Context<'_>) -> Result<HandlerOutput> {
    let own = ctx
        .principal
        .subject()
        .map(|s| quota(load(ctx, &format!("public_board_quota:{s}"))?, ctx.now))
        .transpose()?;
    output(
        projection(load(ctx, "public_board_v1")?.unwrap_or(default()?), own)?,
        vec![],
    )
}
pub(super) fn update(ctx: &mut Context<'_>) -> Result<HandlerOutput> {
    let subject = ctx.subject()?.to_owned();
    let operation = ctx.operation()?;
    ctx.authorizer
        .require_base(ctx.principal, &operation, &subject, ctx.tx, ctx.now)?;
    let config = ctx
        .tx
        .setting("runtime_config")?
        .map(Json::into_object)
        .transpose()?
        .unwrap_or_default();
    require(
        config
            .get("accept_writes")
            .map(msg_identity::store::truthy)
            .transpose()?
            .unwrap_or(true),
        "writes_paused",
    )?;
    let args = ctx.args()?;
    require(
        args.contains_key("svg") || args.contains_key("text"),
        "public_board_content_required",
    )?;
    let record = load(ctx, "public_board_v1")?.unwrap_or(default()?);
    let generation = record["generation"]
        .as_i64()
        .ok_or(Error("invalid_public_board"))?;
    require(
        ctx.arg("generation")?.as_integer()? == generation.to_string(),
        "public_board_conflict",
    )?;
    let svg = args.get("svg").map(Json::as_str).transpose()?.unwrap_or(
        record["svg"]
            .as_str()
            .ok_or(Error("invalid_public_board"))?,
    );
    validate_svg(svg)?;
    let text = args.get("text").map(Json::as_str).transpose()?.unwrap_or(
        record["text"]
            .as_str()
            .ok_or(Error("invalid_public_board"))?,
    );
    require(text.len() <= 8192, "public_board_text_too_large")?;
    require(
        !text.chars().any(|c| c < ' ' && !matches!(c, '\n' | '\t')),
        "invalid_text",
    )?;
    require(
        record["svg"] != svg || record["text"] != text,
        "public_board_unchanged",
    )?;
    let key = format!("public_board_quota:{subject}");
    let mut own = quota(load(ctx, &key)?, ctx.now)?;
    let mut total = quota(load(ctx, "public_board_global_quota")?, ctx.now)?;
    let number = |data: &J, key: &str| {
        data[key]
            .as_u64()
            .ok_or(Error("invalid_public_board_quota"))
    };
    require(
        number(&own, "hour_count")? < 5 && number(&own, "day_count")? < 20,
        "public_board_rate_limited",
    )?;
    require(
        number(&total, "hour_count")? < 30 && number(&total, "day_count")? < 300,
        "public_board_global_rate_limited",
    )?;
    if let Some(last) = own["last_update"].as_str() {
        require(
            ctx.now.0 >= Timestamp::parse(last)?.0 + Duration::seconds(60),
            "public_board_cooldown",
        )?;
    }
    for budget in [&mut own, &mut total] {
        budget["hour_count"] = json!(number(budget, "hour_count")? + 1);
        budget["day_count"] = json!(number(budget, "day_count")? + 1);
        budget["last_update"] = json!(ctx.now);
    }
    ctx.tx.set_setting(&key, &encode(&own)?)?;
    ctx.tx
        .set_setting("public_board_global_quota", &encode(&total)?)?;
    let mut saved = record.clone();
    saved
        .as_object_mut()
        .ok_or(Error("invalid_public_board"))?
        .remove("history");
    let mut history = vec![saved];
    history.extend(
        record["history"]
            .as_array()
            .ok_or(Error("invalid_public_board"))?
            .iter()
            .take(19)
            .cloned(),
    );
    let next = json!({"generation":generation.checked_add(1).ok_or(Error("generation_exhausted"))?,"svg":svg,"text":text,"updated_by":subject,"updated_at":ctx.now,"history":history});
    ctx.tx.set_setting("public_board_v1", &encode(&next)?)?;
    output(projection(next, Some(own))?, vec![])
}
fn validate_svg(svg: &str) -> Result<()> {
    require(svg.len() <= 16384, "public_board_svg_too_large")?;
    require(
        !svg.contains("<!") && !svg.contains("<?"),
        "public_board_unsafe_svg",
    )?;
    let doc = roxmltree::Document::parse(svg).map_err(|_| Error("public_board_invalid_svg"))?;
    let root = doc.root_element();
    const NS: &str = "http://www.w3.org/2000/svg";
    require(
        root.tag_name().namespace() == Some(NS) && root.tag_name().name() == "svg",
        "public_board_invalid_svg",
    )?;
    require(
        root.attribute("viewBox") == Some("0 0 960 300")
            && root.attribute("width").unwrap_or("960") == "960"
            && root.attribute("height").unwrap_or("300") == "300",
        "public_board_viewbox_required",
    )?;
    let mut elements = 0;
    let mut animations = 0;
    let duration =
        regex::Regex::new(r"^[0-9]+(?:\.[0-9]+)?s$").map_err(|_| Error("invalid_pattern"))?;
    let begin =
        regex::Regex::new(r"^-?[0-9]+(?:\.[0-9]+)?s$").map_err(|_| Error("invalid_pattern"))?;
    let repeat = regex::Regex::new(r"^[1-9][0-9]{0,2}$").map_err(|_| Error("invalid_pattern"))?;
    for node in doc.descendants().filter(|n| n.is_element()) {
        elements += 1;
        require(elements <= 256, "public_board_svg_too_complex")?;
        let tag = node.tag_name().name();
        require(
            node.tag_name().namespace() == Some(NS)
                && matches!(
                    tag,
                    "svg"
                        | "g"
                        | "path"
                        | "rect"
                        | "circle"
                        | "ellipse"
                        | "line"
                        | "polyline"
                        | "polygon"
                        | "text"
                        | "tspan"
                        | "title"
                        | "desc"
                        | "animate"
                        | "animateTransform"
                ),
            "public_board_unsafe_svg",
        )?;
        for attribute in node.attributes() {
            let v = attribute.value();
            let lower = crate::unicode::casefold(v);
            require(
                attribute.namespace().is_none()
                    && matches!(
                        attribute.name(),
                        "viewBox"
                            | "width"
                            | "height"
                            | "x"
                            | "y"
                            | "x1"
                            | "y1"
                            | "x2"
                            | "y2"
                            | "cx"
                            | "cy"
                            | "r"
                            | "rx"
                            | "ry"
                            | "d"
                            | "points"
                            | "fill"
                            | "stroke"
                            | "stroke-width"
                            | "opacity"
                            | "fill-opacity"
                            | "stroke-opacity"
                            | "stroke-linecap"
                            | "stroke-linejoin"
                            | "transform"
                            | "font-size"
                            | "font-family"
                            | "text-anchor"
                            | "dominant-baseline"
                            | "attributeName"
                            | "values"
                            | "from"
                            | "to"
                            | "dur"
                            | "begin"
                            | "repeatCount"
                            | "type"
                            | "keyTimes"
                            | "calcMode"
                            | "keySplines"
                            | "additive"
                            | "accumulate"
                    )
                    && v.chars().count() <= 4096
                    && !lower.match_indices("url").any(|(i, _)| {
                        lower[i + 3..]
                            .trim_start_matches(super::py_space)
                            .starts_with('(')
                    })
                    && !v.chars().any(|c| {
                        matches!(c, '<' | '>') || (c < ' ' && !matches!(c, '\n' | '\r' | '\t'))
                    }),
                "public_board_unsafe_svg",
            )?;
        }
        if matches!(tag, "animate" | "animateTransform") {
            animations += 1;
            require(
                matches!(
                    node.attribute("attributeName"),
                    Some(
                        "x" | "y"
                            | "x1"
                            | "x2"
                            | "y1"
                            | "y2"
                            | "cx"
                            | "cy"
                            | "r"
                            | "rx"
                            | "ry"
                            | "width"
                            | "height"
                            | "opacity"
                            | "fill-opacity"
                            | "stroke-opacity"
                            | "fill"
                            | "stroke"
                            | "stroke-width"
                            | "transform"
                    )
                ),
                "public_board_unsafe_svg",
            )?;
            let d = node.attribute("dur").unwrap_or("");
            require(
                duration.is_match(d)
                    && d[..d.len().saturating_sub(1)]
                        .parse::<f64>()
                        .is_ok_and(|v| (1.0..=120.0).contains(&v)),
                "public_board_animation_duration",
            )?;
            require(
                begin.is_match(node.attribute("begin").unwrap_or("0s")),
                "public_board_unsafe_svg",
            )?;
            let count = node.attribute("repeatCount").unwrap_or("1");
            require(
                count == "indefinite" || repeat.is_match(count),
                "public_board_unsafe_svg",
            )?;
            if tag == "animateTransform" {
                require(
                    node.attribute("attributeName") == Some("transform")
                        && matches!(
                            node.attribute("type"),
                            Some("translate" | "scale" | "rotate" | "skewX" | "skewY")
                        ),
                    "public_board_unsafe_svg",
                )?;
            }
        }
    }
    require(animations <= 32, "public_board_svg_too_complex")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn untouched_board_is_the_release_owned_art() {
        let d = default().unwrap();
        assert_eq!(d["text"], "写下一句，让它继续走。");
        validate_svg(d["svg"].as_str().unwrap()).unwrap();
    }
    #[test]
    fn hour_and_day_roll_at_taipei_boundaries_and_history_does_not_expose_content() {
        let before = quota(
            None,
            Timestamp::parse("2026-10-21T15:59:59.000000Z").unwrap(),
        )
        .unwrap();
        let after = quota(
            Some(json!({"hour":before["hour"],"day":before["day"],"hour_count":5,"day_count":20})),
            Timestamp::parse("2026-10-21T16:00:00.000000Z").unwrap(),
        )
        .unwrap();
        assert_eq!(after["day"], "2026-10-22");
        assert_eq!(after["hour_count"], 0);
        assert_eq!(after["day_count"], 0);
        let mut d = default().unwrap();
        d["history"] =
            json!([{"generation":1,"svg":"private-old","text":"old","updated_by":"u_a"}]);
        assert_eq!(
            projection(d, None).unwrap()["history"],
            json!([{"generation":1,"updated_by":"u_a"}])
        );
    }
    #[test]
    fn board_svg_forbids_css_event_attributes_and_animation_triggers() {
        for payload in [
            "<style/>",
            "<path fill='url(a)'/>",
            "<path onload='x'/>",
            "<animate attributeName='href' dur='2s'/>",
            "<animate attributeName='x' dur='2s' begin='click'/>",
        ] {
            let svg = format!(
                "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 960 300'>{payload}</svg>"
            );
            assert!(validate_svg(&svg).is_err());
        }
    }
}
