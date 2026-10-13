//! Bound values for trusted, source-owned SQL. Neither rows nor parameters log secrets.
use bytes::BytesMut;
use msg_core::{Error, Json, Result, MAX_BYTES};
use postgres::types::{to_sql_checked, IsNull, ToSql, Type};
use std::{error::Error as StdError, fmt};

#[derive(Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Text(String),
    Bytes(Vec<u8>),
    TextArray(Vec<String>),
}
impl fmt::Debug for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Null => "NULL",
            Self::Bool(_) => "<bool>",
            Self::Int(_) => "<integer>",
            Self::Text(_) => "<text>",
            Self::Bytes(_) => "<bytes>",
            Self::TextArray(_) => "<text array>",
        })
    }
}
impl From<&str> for Value {
    fn from(value: &str) -> Self {
        Self::Text(value.into())
    }
}
impl From<String> for Value {
    fn from(value: String) -> Self {
        Self::Text(value)
    }
}
impl From<i64> for Value {
    fn from(value: i64) -> Self {
        Self::Int(value)
    }
}
impl From<i32> for Value {
    fn from(value: i32) -> Self {
        Self::Int(value.into())
    }
}
impl From<u32> for Value {
    fn from(value: u32) -> Self {
        Self::Int(value.into())
    }
}
impl From<bool> for Value {
    fn from(value: bool) -> Self {
        Self::Bool(value)
    }
}
impl From<Option<&str>> for Value {
    fn from(value: Option<&str>) -> Self {
        value.map(Self::from).unwrap_or(Self::Null)
    }
}
impl From<Option<String>> for Value {
    fn from(value: Option<String>) -> Self {
        value.map(Self::from).unwrap_or(Self::Null)
    }
}
impl ToSql for Value {
    fn to_sql(
        &self,
        ty: &Type,
        out: &mut BytesMut,
    ) -> std::result::Result<IsNull, Box<dyn StdError + Sync + Send>> {
        match self {
            Self::Null => Ok(IsNull::Yes),
            Self::Bool(value) if *ty == Type::BOOL => value.to_sql(ty, out),
            Self::Int(value) => match *ty {
                Type::INT2 => i16::try_from(*value)
                    .map_err(|_| "database_integer_range")?
                    .to_sql(ty, out),
                Type::INT4 => i32::try_from(*value)
                    .map_err(|_| "database_integer_range")?
                    .to_sql(ty, out),
                Type::INT8 => value.to_sql(ty, out),
                _ => Err("database_parameter_type".into()),
            },
            Self::Text(value) if <String as ToSql>::accepts(ty) => value.to_sql(ty, out),
            Self::Bytes(value) if *ty == Type::BYTEA => value.to_sql(ty, out),
            Self::TextArray(value) if <Vec<String> as ToSql>::accepts(ty) => value.to_sql(ty, out),
            _ => Err("database_parameter_type".into()),
        }
    }
    fn accepts(ty: &Type) -> bool {
        matches!(
            *ty,
            Type::BOOL | Type::INT2 | Type::INT4 | Type::INT8 | Type::BYTEA
        ) || <String as ToSql>::accepts(ty)
            || <Vec<String> as ToSql>::accepts(ty)
    }
    to_sql_checked!();
}
#[derive(Clone)]
pub struct Row(pub(crate) Vec<Value>);
impl Row {
    pub fn len(&self) -> usize {
        self.0.len()
    }
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
    pub fn value(&self, index: usize) -> Result<&Value> {
        self.0.get(index).ok_or(Error("storage_column_missing"))
    }
    pub fn text(&self, index: usize) -> Result<&str> {
        match self.value(index)? {
            Value::Text(v) => Ok(v),
            _ => Err(Error("storage_column_type")),
        }
    }
    pub fn optional_text(&self, index: usize) -> Result<Option<&str>> {
        match self.value(index)? {
            Value::Null => Ok(None),
            Value::Text(v) => Ok(Some(v)),
            _ => Err(Error("storage_column_type")),
        }
    }
    pub fn integer(&self, index: usize) -> Result<i64> {
        match self.value(index)? {
            Value::Int(v) => Ok(*v),
            _ => Err(Error("storage_column_type")),
        }
    }
    pub fn boolean(&self, index: usize) -> Result<bool> {
        match self.value(index)? {
            Value::Bool(v) => Ok(*v),
            _ => Err(Error("storage_column_type")),
        }
    }
    pub fn json(&self, index: usize) -> Result<Json> {
        let s = self.text(index)?;
        if s.len() > MAX_BYTES {
            return Err(Error("storage_record_too_large"));
        }
        Json::parse(s)
    }
    pub(crate) fn decode(raw: postgres::Row) -> Result<Self> {
        let mut values = Vec::with_capacity(raw.len());
        for (i, column) in raw.columns().iter().enumerate() {
            let value = match *column.type_() {
                Type::BOOL => raw
                    .try_get::<_, Option<bool>>(i)
                    .map(|v| v.map(Value::Bool).unwrap_or(Value::Null)),
                Type::INT2 => raw
                    .try_get::<_, Option<i16>>(i)
                    .map(|v| v.map(|v| Value::Int(v.into())).unwrap_or(Value::Null)),
                Type::INT4 => raw
                    .try_get::<_, Option<i32>>(i)
                    .map(|v| v.map(|v| Value::Int(v.into())).unwrap_or(Value::Null)),
                Type::INT8 => raw
                    .try_get::<_, Option<i64>>(i)
                    .map(|v| v.map(Value::Int).unwrap_or(Value::Null)),
                Type::BYTEA => raw
                    .try_get::<_, Option<Vec<u8>>>(i)
                    .map(|v| v.map(Value::Bytes).unwrap_or(Value::Null)),
                Type::TEXT_ARRAY | Type::VARCHAR_ARRAY => raw
                    .try_get::<_, Option<Vec<String>>>(i)
                    .map(|v| v.map(Value::TextArray).unwrap_or(Value::Null)),
                _ => raw
                    .try_get::<_, Option<String>>(i)
                    .map(|v| v.map(Value::Text).unwrap_or(Value::Null)),
            }
            .map_err(|_| Error("storage_column_type"))?;
            values.push(value);
        }
        Ok(Self(values))
    }
    pub(crate) fn bytes(&self) -> usize {
        self.0
            .iter()
            .map(|v| match v {
                Value::Text(v) => v.len(),
                Value::Bytes(v) => v.len(),
                Value::TextArray(v) => v.iter().map(String::len).sum(),
                _ => 8,
            })
            .sum()
    }
}
