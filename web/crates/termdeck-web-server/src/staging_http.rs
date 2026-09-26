use super::*;
use termdeck_web_auth::{SecretKind, digest_validated_secret};

pub(crate) async fn operate(
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
        return json_response(
            StatusCode::BAD_REQUEST,
            &serde_json::json!({"ok":false,"error":"JSON required"}),
        );
    }
    let key = match machine_http::credential(request.headers()) {
        Ok(v) => v,
        Err(e) => return machine_http::machine_error(e),
    };
    let (login, machines) = auth.machine_stores();
    let record = match machines.identify(login, &session, key, unix_ms()) {
        Ok(v) => v,
        Err(e) => return machine_http::machine_error(e),
    };
    let digest = match digest_validated_secret(SecretKind::Machine, key) {
        Ok(v) => v.iter().map(|b| format!("{b:02x}")).collect::<String>(),
        Err(_) => return forbidden_response("machine authorization failed"),
    };
    let Some(auth_dir) = login.database_path().parent() else {
        return forbidden_response("authentication unavailable");
    };
    let bytes = match axum::body::to_bytes(request.into_body(), 380 * 1024).await {
        Ok(v) => v,
        Err(_) => {
            return json_response(
                StatusCode::PAYLOAD_TOO_LARGE,
                &serde_json::json!({"ok":false,"error":"request too large"}),
            );
        }
    };
    let body: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(_) => {
            return json_response(
                StatusCode::BAD_REQUEST,
                &serde_json::json!({"ok":false,"error":"invalid JSON"}),
            );
        }
    };
    // The internal resolver is never an HTTP operation. Actor is server-owned;
    // all client fields are independently checked again by the staging store.
    if !matches!(
        body.get("operation").and_then(serde_json::Value::as_str),
        Some(
            "list"
                | "inspect"
                | "initiate"
                | "chunk"
                | "finalize"
                | "remove"
                | "attach"
                | "detach"
                | "download"
                | "cleanup"
                | "launch-options"
                | "create-window"
        )
    ) {
        return json_response(
            StatusCode::BAD_REQUEST,
            &serde_json::json!({"ok":false,"error":"invalid operation"}),
        );
    }
    let Some(service) = state.tab_actions.as_ref() else {
        return json_response(
            StatusCode::SERVICE_UNAVAILABLE,
            &serde_json::json!({"ok":false,"error":"staging bridge unavailable"}),
        );
    };
    let envelope = serde_json::json!({"actor":{
        "account_id":record.account_id,"machine_id":record.machine_id,
        "session_id":session.session_id().as_str(),"credential_hash":digest,
        "credential_generation":record.credential_generation},"request":body});
    match service.stage(auth_dir, envelope).await {
        Ok(result) => {
            let status = if result.get("ok").and_then(serde_json::Value::as_bool) == Some(true) {
                StatusCode::OK
            } else {
                match result.get("code").and_then(serde_json::Value::as_str) {
                    Some("unauthorized") => StatusCode::FORBIDDEN,
                    Some("conflict" | "stale-reference") => StatusCode::CONFLICT,
                    Some("capacity") => StatusCode::PAYLOAD_TOO_LARGE,
                    Some("invalid-request" | "unavailable-workspace") => StatusCode::BAD_REQUEST,
                    _ => StatusCode::SERVICE_UNAVAILABLE,
                }
            };
            json_response(status, &result)
        }
        Err(_) => json_response(
            StatusCode::SERVICE_UNAVAILABLE,
            &serde_json::json!({"ok":false,"error":"staging bridge unavailable"}),
        ),
    }
}
