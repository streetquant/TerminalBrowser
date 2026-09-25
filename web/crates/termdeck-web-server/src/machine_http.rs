use super::*;
use serde::Deserialize;
use termdeck_web_auth::{MachineError, MachineMutation};
use zeroize::Zeroize;

pub(crate) const MACHINE_HEADER: &str = "x-termdeck-machine-key";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Mutation {
    operation: String,
    request_id: String,
    machine_id: Option<String>,
    credential: Option<String>,
    new_credential: Option<String>,
    label: Option<String>,
}

impl Drop for Mutation {
    fn drop(&mut self) {
        if let Some(key) = self.credential.as_mut() {
            key.zeroize();
        }
        if let Some(key) = self.new_credential.as_mut() {
            key.zeroize();
        }
    }
}

pub(crate) fn machine_error(error: MachineError) -> Response {
    let status = match error {
        MachineError::Invalid => StatusCode::BAD_REQUEST,
        MachineError::Unauthorized => StatusCode::FORBIDDEN,
        MachineError::Conflict => StatusCode::CONFLICT,
        MachineError::Capacity => StatusCode::TOO_MANY_REQUESTS,
        MachineError::Store(AuthError::Unauthenticated) => StatusCode::UNAUTHORIZED,
        MachineError::Store(_) => StatusCode::SERVICE_UNAVAILABLE,
    };
    json_response(
        status,
        &serde_json::json!({"ok":false,"error":error.to_string()}),
    )
}

pub(crate) fn credential(headers: &HeaderMap) -> Result<&str, MachineError> {
    if headers.get_all(MACHINE_HEADER).iter().count() != 1 {
        return Err(MachineError::Unauthorized);
    }
    let key = headers
        .get(MACHINE_HEADER)
        .and_then(|v| v.to_str().ok())
        .ok_or(MachineError::Unauthorized)?;
    termdeck_web_auth::validate_secret(termdeck_web_auth::SecretKind::Machine, key)
        .map_err(|_| MachineError::Unauthorized)?;
    Ok(key)
}

pub(crate) async fn list(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
) -> Response {
    let (Some(auth), Some(Extension(session))) = (state.auth.as_ref(), context) else {
        return forbidden_response("authentication is required");
    };
    let (login, machines) = auth.machine_stores();
    match machines.list(login, &session, unix_ms()) {
        Ok(records) => json_response(
            StatusCode::OK,
            &serde_json::json!({"schema":"termdeck.machines.v1","machines":records}),
        ),
        Err(e) => machine_error(e),
    }
}

pub(crate) async fn current(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    headers: HeaderMap,
) -> Response {
    let (Some(auth), Some(Extension(session))) = (state.auth.as_ref(), context) else {
        return forbidden_response("authentication is required");
    };
    let key = match credential(&headers) {
        Ok(v) => v,
        Err(e) => return machine_error(e),
    };
    let (login, machines) = auth.machine_stores();
    match machines.identify(login, &session, key, unix_ms()) {
        Ok(record) => json_response(
            StatusCode::OK,
            &serde_json::json!({"schema":"termdeck.machine.v1","machine":record}),
        ),
        Err(e) => machine_error(e),
    }
}

pub(crate) async fn mutate(
    State(state): State<AppState>,
    context: Option<Extension<SessionContext>>,
    request: Request,
) -> Response {
    let (Some(auth), Some(Extension(session))) = (state.auth.as_ref(), context) else {
        return forbidden_response("authentication is required");
    };
    if request
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        != Some("application/json")
    {
        return machine_error(MachineError::Invalid);
    }
    let bytes = match axum::body::to_bytes(request.into_body(), 4096).await {
        Ok(v) => v,
        Err(_) => return machine_error(MachineError::Invalid),
    };
    let body: Mutation = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(_) => return machine_error(MachineError::Invalid),
    };
    let mutation = MachineMutation {
        operation: &body.operation,
        request_id: &body.request_id,
        machine_id: body.machine_id.as_deref(),
        credential: body.credential.as_deref(),
        new_credential: body.new_credential.as_deref(),
        label: body.label.as_deref(),
    };
    let (login, machines) = auth.machine_stores();
    match machines.mutate(login, &session, mutation, unix_ms()) {
        Ok(record) => json_response(
            StatusCode::OK,
            &serde_json::json!({"schema":"termdeck.machine.v1","machine":record}),
        ),
        Err(e) => machine_error(e),
    }
}
