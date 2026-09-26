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
        MachineError::Store(AuthError::Unauthenticated) => {
            let mut response = json_response(
                StatusCode::UNAUTHORIZED,
                &serde_json::json!({"ok":false,"error":"authentication-required"}),
            );
            response.headers_mut().insert(
                header::WWW_AUTHENTICATE,
                HeaderValue::from_static("TermDeckSession"),
            );
            return response;
        }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn login_revalidation_failure_is_distinct_from_redacted_store_failure() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap_or_else(|error| panic!("could not build machine-error test runtime: {error}"));
        runtime.block_on(async {
            for (error, status, message) in [
                (
                    MachineError::Store(AuthError::Unauthenticated),
                    StatusCode::UNAUTHORIZED,
                    "authentication-required",
                ),
                (
                    MachineError::Store(AuthError::Io(std::io::Error::other(
                        "private fixture path and credential must remain hidden",
                    ))),
                    StatusCode::SERVICE_UNAVAILABLE,
                    "machine store is unavailable",
                ),
            ] {
                let response = machine_error(error);
                assert_eq!(response.status(), status);
                assert_eq!(
                    response.headers().get(header::WWW_AUTHENTICATE),
                    (status == StatusCode::UNAUTHORIZED)
                        .then_some(&HeaderValue::from_static("TermDeckSession")),
                );
                let bytes = axum::body::to_bytes(response.into_body(), 1024)
                    .await
                    .unwrap_or_else(|error| panic!("could not read machine-error body: {error}"));
                let value: serde_json::Value = serde_json::from_slice(&bytes)
                    .unwrap_or_else(|error| panic!("invalid machine-error JSON: {error}"));
                assert_eq!(value, serde_json::json!({"ok":false,"error":message}));
            }
        });
    }
}
