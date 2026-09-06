//! Dependency-free, bounded RFC 8259 values used by the native protocol.
//!
//! Release catalogs remain disabled in this foundation slice. This parser is
//! deliberately private to already frame-bounded native JSON-RPC messages; it
//! rejects duplicate object keys, excessive nesting/containers, invalid UTF-8,
//! malformed escapes, non-JSON numbers, and trailing input.

use std::collections::BTreeMap;

pub const MAX_JSON_DEPTH: usize = 64;
pub const MAX_CONTAINER_ITEMS: usize = 4_096;
pub const MAX_STRING_BYTES: usize = 512 * 1024;

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Number(String),
    String(String),
    Array(Vec<Value>),
    Object(BTreeMap<String, Value>),
}

impl Value {
    pub fn as_object(&self) -> Option<&BTreeMap<String, Value>> {
        match self {
            Self::Object(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&[Value]> {
        match self {
            Self::Array(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::String(value) => Some(value),
            _ => None,
        }
    }

    pub fn as_u64(&self) -> Option<u64> {
        match self {
            Self::Number(value)
                if !value.starts_with('-')
                    && !value.contains('.')
                    && !value.contains('e')
                    && !value.contains('E') =>
            {
                value.parse().ok()
            }
            _ => None,
        }
    }

    pub fn encode(&self) -> String {
        match self {
            Self::Null => "null".to_owned(),
            Self::Bool(value) => value.to_string(),
            Self::Number(value) => value.clone(),
            Self::String(value) => quote(value),
            Self::Array(values) => {
                let values = values.iter().map(Self::encode).collect::<Vec<_>>();
                format!("[{}]", values.join(","))
            }
            Self::Object(fields) => {
                let fields = fields
                    .iter()
                    .map(|(key, value)| format!("{}:{}", quote(key), value.encode()))
                    .collect::<Vec<_>>();
                format!("{{{}}}", fields.join(","))
            }
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ParseError {
    InvalidUtf8,
    UnexpectedEnd,
    UnexpectedToken,
    InvalidEscape,
    InvalidUnicode,
    InvalidNumber,
    DuplicateKey,
    DepthExceeded,
    ContainerTooLarge,
    StringTooLarge,
    TrailingData,
}

impl ParseError {
    pub const fn reason_code(self) -> &'static str {
        match self {
            Self::InvalidUtf8 => "JSON_INVALID_UTF8",
            Self::UnexpectedEnd => "JSON_UNEXPECTED_END",
            Self::UnexpectedToken => "JSON_UNEXPECTED_TOKEN",
            Self::InvalidEscape => "JSON_INVALID_ESCAPE",
            Self::InvalidUnicode => "JSON_INVALID_UNICODE",
            Self::InvalidNumber => "JSON_INVALID_NUMBER",
            Self::DuplicateKey => "JSON_DUPLICATE_KEY",
            Self::DepthExceeded => "JSON_DEPTH_EXCEEDED",
            Self::ContainerTooLarge => "JSON_CONTAINER_TOO_LARGE",
            Self::StringTooLarge => "JSON_STRING_TOO_LARGE",
            Self::TrailingData => "JSON_TRAILING_DATA",
        }
    }
}

pub fn parse(input: &[u8]) -> Result<Value, ParseError> {
    let source = std::str::from_utf8(input).map_err(|_| ParseError::InvalidUtf8)?;
    let mut parser = Parser { source, index: 0 };
    let value = parser.parse_value(0)?;
    parser.skip_whitespace();
    if parser.index != source.len() {
        return Err(ParseError::TrailingData);
    }
    Ok(value)
}

struct Parser<'a> {
    source: &'a str,
    index: usize,
}

impl Parser<'_> {
    fn parse_value(&mut self, depth: usize) -> Result<Value, ParseError> {
        if depth > MAX_JSON_DEPTH {
            return Err(ParseError::DepthExceeded);
        }
        self.skip_whitespace();
        match self.peek_byte() {
            Some(b'n') => self.parse_literal("null", Value::Null),
            Some(b't') => self.parse_literal("true", Value::Bool(true)),
            Some(b'f') => self.parse_literal("false", Value::Bool(false)),
            Some(b'"') => self.parse_string().map(Value::String),
            Some(b'[') => self.parse_array(depth + 1),
            Some(b'{') => self.parse_object(depth + 1),
            Some(b'-' | b'0'..=b'9') => self.parse_number().map(Value::Number),
            Some(_) => Err(ParseError::UnexpectedToken),
            None => Err(ParseError::UnexpectedEnd),
        }
    }

    fn parse_literal(&mut self, literal: &str, value: Value) -> Result<Value, ParseError> {
        if self.source[self.index..].starts_with(literal) {
            self.index += literal.len();
            Ok(value)
        } else {
            Err(ParseError::UnexpectedToken)
        }
    }

    fn parse_array(&mut self, depth: usize) -> Result<Value, ParseError> {
        self.index += 1;
        self.skip_whitespace();
        let mut values = Vec::new();
        if self.consume_byte(b']') {
            return Ok(Value::Array(values));
        }
        loop {
            if values.len() >= MAX_CONTAINER_ITEMS {
                return Err(ParseError::ContainerTooLarge);
            }
            values.push(self.parse_value(depth)?);
            self.skip_whitespace();
            if self.consume_byte(b']') {
                return Ok(Value::Array(values));
            }
            if !self.consume_byte(b',') {
                return Err(ParseError::UnexpectedToken);
            }
        }
    }

    fn parse_object(&mut self, depth: usize) -> Result<Value, ParseError> {
        self.index += 1;
        self.skip_whitespace();
        let mut fields = BTreeMap::new();
        if self.consume_byte(b'}') {
            return Ok(Value::Object(fields));
        }
        loop {
            if fields.len() >= MAX_CONTAINER_ITEMS {
                return Err(ParseError::ContainerTooLarge);
            }
            self.skip_whitespace();
            if self.peek_byte() != Some(b'"') {
                return Err(ParseError::UnexpectedToken);
            }
            let key = self.parse_string()?;
            self.skip_whitespace();
            if !self.consume_byte(b':') {
                return Err(ParseError::UnexpectedToken);
            }
            let value = self.parse_value(depth)?;
            if fields.insert(key, value).is_some() {
                return Err(ParseError::DuplicateKey);
            }
            self.skip_whitespace();
            if self.consume_byte(b'}') {
                return Ok(Value::Object(fields));
            }
            if !self.consume_byte(b',') {
                return Err(ParseError::UnexpectedToken);
            }
        }
    }

    fn parse_string(&mut self) -> Result<String, ParseError> {
        if !self.consume_byte(b'"') {
            return Err(ParseError::UnexpectedToken);
        }
        let mut output = String::new();
        loop {
            let byte = self.peek_byte().ok_or(ParseError::UnexpectedEnd)?;
            match byte {
                b'"' => {
                    self.index += 1;
                    return Ok(output);
                }
                b'\\' => {
                    self.index += 1;
                    let escape = self.peek_byte().ok_or(ParseError::UnexpectedEnd)?;
                    self.index += 1;
                    match escape {
                        b'"' => output.push('"'),
                        b'\\' => output.push('\\'),
                        b'/' => output.push('/'),
                        b'b' => output.push('\u{08}'),
                        b'f' => output.push('\u{0c}'),
                        b'n' => output.push('\n'),
                        b'r' => output.push('\r'),
                        b't' => output.push('\t'),
                        b'u' => self.push_unicode_escape(&mut output)?,
                        _ => return Err(ParseError::InvalidEscape),
                    }
                }
                0x00..=0x1f => return Err(ParseError::UnexpectedToken),
                _ => {
                    let character = self.source[self.index..]
                        .chars()
                        .next()
                        .ok_or(ParseError::UnexpectedEnd)?;
                    output.push(character);
                    self.index += character.len_utf8();
                }
            }
            if output.len() > MAX_STRING_BYTES {
                return Err(ParseError::StringTooLarge);
            }
        }
    }

    fn push_unicode_escape(&mut self, output: &mut String) -> Result<(), ParseError> {
        let first = self.parse_hex_quad()?;
        let scalar = if (0xd800..=0xdbff).contains(&first) {
            if !self.source[self.index..].starts_with("\\u") {
                return Err(ParseError::InvalidUnicode);
            }
            self.index += 2;
            let second = self.parse_hex_quad()?;
            if !(0xdc00..=0xdfff).contains(&second) {
                return Err(ParseError::InvalidUnicode);
            }
            0x1_0000 + (((first - 0xd800) as u32) << 10) + (second - 0xdc00) as u32
        } else if (0xdc00..=0xdfff).contains(&first) {
            return Err(ParseError::InvalidUnicode);
        } else {
            first as u32
        };
        output.push(char::from_u32(scalar).ok_or(ParseError::InvalidUnicode)?);
        Ok(())
    }

    fn parse_hex_quad(&mut self) -> Result<u16, ParseError> {
        let end = self
            .index
            .checked_add(4)
            .ok_or(ParseError::InvalidUnicode)?;
        let value = self
            .source
            .get(self.index..end)
            .ok_or(ParseError::UnexpectedEnd)?;
        if !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(ParseError::InvalidUnicode);
        }
        self.index = end;
        u16::from_str_radix(value, 16).map_err(|_| ParseError::InvalidUnicode)
    }

    fn parse_number(&mut self) -> Result<String, ParseError> {
        let start = self.index;
        self.consume_byte(b'-');
        match self.peek_byte() {
            Some(b'0') => {
                self.index += 1;
                if self.peek_byte().is_some_and(|byte| byte.is_ascii_digit()) {
                    return Err(ParseError::InvalidNumber);
                }
            }
            Some(b'1'..=b'9') => {
                self.index += 1;
                while self.peek_byte().is_some_and(|byte| byte.is_ascii_digit()) {
                    self.index += 1;
                }
            }
            _ => return Err(ParseError::InvalidNumber),
        }
        if self.consume_byte(b'.') {
            if !self.peek_byte().is_some_and(|byte| byte.is_ascii_digit()) {
                return Err(ParseError::InvalidNumber);
            }
            while self.peek_byte().is_some_and(|byte| byte.is_ascii_digit()) {
                self.index += 1;
            }
        }
        if self
            .peek_byte()
            .is_some_and(|byte| matches!(byte, b'e' | b'E'))
        {
            self.index += 1;
            if self
                .peek_byte()
                .is_some_and(|byte| matches!(byte, b'+' | b'-'))
            {
                self.index += 1;
            }
            if !self.peek_byte().is_some_and(|byte| byte.is_ascii_digit()) {
                return Err(ParseError::InvalidNumber);
            }
            while self.peek_byte().is_some_and(|byte| byte.is_ascii_digit()) {
                self.index += 1;
            }
        }
        Ok(self.source[start..self.index].to_owned())
    }

    fn skip_whitespace(&mut self) {
        while self
            .peek_byte()
            .is_some_and(|byte| matches!(byte, b' ' | b'\n' | b'\r' | b'\t'))
        {
            self.index += 1;
        }
    }

    fn consume_byte(&mut self, expected: u8) -> bool {
        if self.peek_byte() == Some(expected) {
            self.index += 1;
            true
        } else {
            false
        }
    }

    fn peek_byte(&self) -> Option<u8> {
        self.source.as_bytes().get(self.index).copied()
    }
}

pub fn quote(value: &str) -> String {
    let mut output = String::with_capacity(value.len() + 2);
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            character if character <= '\u{1f}' => {
                output.push_str(&format!("\\u{:04x}", character as u32));
            }
            character => output.push(character),
        }
    }
    output.push('"');
    output
}

pub fn string_array<'a>(values: impl IntoIterator<Item = &'a str>) -> String {
    let values = values.into_iter().map(quote).collect::<Vec<_>>();
    format!("[{}]", values.join(","))
}

pub fn object(fields: &[(&str, String)]) -> String {
    let fields = fields
        .iter()
        .map(|(key, value)| format!("{}:{}", quote(key), value))
        .collect::<Vec<_>>();
    format!("{{{}}}", fields.join(","))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escapes_untrusted_strings() {
        assert_eq!(quote("a\"b\\c\n\u{0001}"), "\"a\\\"b\\\\c\\n\\u0001\"");
    }

    #[test]
    fn parser_round_trips_nested_json_and_unicode() {
        let input = br#"{"a":[true,null,-12.5e2],"emoji":"\ud83d\ude80"}"#;
        let value = parse(input).expect("valid JSON should parse");
        let encoded = value.encode();
        assert_eq!(parse(encoded.as_bytes()).unwrap(), value);
        assert_eq!(
            value
                .as_object()
                .and_then(|object| object.get("emoji"))
                .and_then(Value::as_str),
            Some("🚀")
        );
    }

    #[test]
    fn parser_rejects_duplicate_keys_trailing_data_and_bad_numbers() {
        assert_eq!(parse(br#"{"a":1,"a":2}"#), Err(ParseError::DuplicateKey));
        assert_eq!(parse(br#"{} true"#), Err(ParseError::TrailingData));
        assert_eq!(parse(br#"01"#), Err(ParseError::InvalidNumber));
        assert_eq!(parse(&[0xff]), Err(ParseError::InvalidUtf8));
    }
}
