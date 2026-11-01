//! Authenticated Factory HTTP and the existing planning UI. SSE tails committed state.
use crate::{Error, Result, Status, control::Control};
use axum::{
    Json, Router,
    body::Body,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::{HeaderMap, StatusCode, header},
    middleware::{self, Next},
    response::{
        IntoResponse, Response,
        sse::{Event, KeepAlive, Sse},
    },
    routing::{get, post},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashSet},
    convert::Infallible,
    time::Duration,
};
use tokio_stream::wrappers::ReceiverStream;
#[derive(Clone)]
struct Server {
    control: Control,
    token: String,
    host: String,
    caller: String,
    origin: String,
}

pub fn router(control: Control, token: String, host: String) -> Router {
    let origin = std::env::var("FACTORY_HTTP_ORIGIN").unwrap_or_else(|_| format!("http://{host}"));
    router_for_origin(control, token, host, origin)
}
/// Explicit public origin for TLS reverse-proxy deployments.
pub fn router_for_origin(control: Control, token: String, host: String, origin: String) -> Router {
    let state = Server {
        control,
        token,
        host,
        caller: std::env::var("FACTORY_CALLER_ID").unwrap_or_else(|_| "local".into()),
        origin,
    };
    Router::new()
        .route("/", get(page))
        .route("/planning/{id}", get(page))
        .route("/static/{name}", get(asset))
        .route("/api/sessions", get(sessions).post(start))
        .route("/api/sessions/{id}", get(status))
        .route("/api/sessions/{id}/report", get(report))
        .route("/api/sessions/{id}/{action}", post(planning_action))
        .route("/api/control/{operation}", post(command))
        .route("/api/runs/{id}/events", get(events))
        .layer(DefaultBodyLimit::max(131072))
        .layer(middleware::from_fn_with_state(state.clone(), guard))
        .with_state(state)
}
fn constant_eq(a: &str, b: &str) -> bool {
    // Fixed-size digests keep comparison work independent of the supplied token prefix.
    let a = crate::runtime::digest(a.as_bytes());
    let b = crate::runtime::digest(b.as_bytes());
    a.bytes()
        .zip(b.bytes())
        .fold(0u8, |diff, (a, b)| diff | (a ^ b))
        == 0
}
fn authorized(headers: &HeaderMap, token: &str) -> bool {
    let bearer = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.strip_prefix("Bearer "));
    let supplied = bearer
        .or_else(|| headers.get("x-factory-token").and_then(|v| v.to_str().ok()))
        .or_else(|| {
            headers
                .get(header::COOKIE)
                .and_then(|v| v.to_str().ok())
                .and_then(|s| {
                    s.split(';')
                        .find_map(|part| part.trim().strip_prefix("factory_planning="))
                })
        })
        .unwrap_or("");
    constant_eq(supplied, token)
}
async fn guard(
    State(state): State<Server>,
    request: axum::extract::Request,
    next: Next,
) -> Response {
    let headers = request.headers();
    if headers.get(header::HOST).and_then(|h| h.to_str().ok()) != Some(&state.host) {
        return (StatusCode::MISDIRECTED_REQUEST, "unknown host").into_response();
    }
    let is_page = request.uri().path() == "/" || request.uri().path().starts_with("/planning/");
    let token_query = is_page
        && request
            .uri()
            .query()
            .and_then(|q| q.strip_prefix("token="))
            .is_some_and(|token| constant_eq(token, &state.token));
    if !token_query && !authorized(headers, &state.token) {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error":"open the link printed by the service"})),
        )
            .into_response();
    }
    if request.method() != axum::http::Method::GET {
        if headers
            .get(header::ORIGIN)
            .is_some_and(|o| o.to_str().ok() != Some(&state.origin))
        {
            return (
                StatusCode::FORBIDDEN,
                Json(json!({"error":"cross-origin request refused"})),
            )
                .into_response();
        }
        if !headers
            .get(header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .is_some_and(|s| s.split(';').next() == Some("application/json"))
        {
            return (StatusCode::UNSUPPORTED_MEDIA_TYPE, "JSON body required").into_response();
        }
    }
    let mut response = next.run(request).await;
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static("no-store"),
    );
    response.headers_mut().insert(
        "x-content-type-options",
        header::HeaderValue::from_static("nosniff"),
    );
    response
}
async fn page(State(state): State<Server>, uri: axum::http::Uri) -> Response {
    if uri.query().is_some_and(|q| q.starts_with("token=")) {
        return Response::builder()
            .status(303)
            .header(header::LOCATION, uri.path())
            .header(
                header::SET_COOKIE,
                format!(
                    "factory_planning={}; HttpOnly; SameSite=Strict; Path=/{}",
                    state.token,
                    if state.origin.starts_with("https://") {
                        "; Secure"
                    } else {
                        ""
                    }
                ),
            )
            .body(Body::empty())
            .unwrap();
    }
    (
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        include_str!("../../../src/software_factory/web_static/index.html"),
    )
        .into_response()
}
async fn asset(Path(name): Path<String>) -> Response {
    if name == "geist-latin.woff2" {
        return (
            [(header::CONTENT_TYPE, "font/woff2")],
            include_bytes!("../../../src/software_factory/web_static/geist-latin.woff2").as_slice(),
        )
            .into_response();
    }
    let (kind, content) = match name.as_str() {
        "app.js" => (
            "text/javascript; charset=utf-8",
            include_str!("../../../src/software_factory/web_static/app.js"),
        ),
        "app.css" => (
            "text/css; charset=utf-8",
            include_str!("../../../src/software_factory/web_static/app.css"),
        ),
        _ => return StatusCode::NOT_FOUND.into_response(),
    };
    ([(header::CONTENT_TYPE, kind)], content).into_response()
}
fn result(value: Result<Value>) -> Response {
    match value {
        Ok(value) => Json(value).into_response(),
        Err(error) => {
            let status = match error {
                Error::NotFound(_) => StatusCode::NOT_FOUND,
                Error::Config(_) | Error::Json(_) => StatusCode::BAD_REQUEST,
                Error::Conflict(_) | Error::Integrity(_) | Error::Busy => StatusCode::CONFLICT,
                _ => StatusCode::INTERNAL_SERVER_ERROR,
            };
            (status, Json(json!({"error":error.to_string()}))).into_response()
        }
    }
}
async fn sessions(State(s): State<Server>) -> Response {
    result(
        s.control
            .call("plan_list", json!({}))
            .await
            .map(|sessions| json!({"workspace":s.control.workspace(),"sessions":sessions})),
    )
}
async fn start(State(s): State<Server>, Json(value): Json<Value>) -> Response {
    result(s.control.call("plan_start", value).await)
}
async fn status(
    State(s): State<Server>,
    Path(id): Path<String>,
    Query(query): Query<BTreeMap<String, String>>,
) -> Response {
    let after = match query
        .get("after_event")
        .map(|v| v.parse::<u64>())
        .transpose()
    {
        Ok(after) => after.unwrap_or(0),
        Err(_) => return result(Err(Error::Config("invalid event cursor".into()))),
    };
    result(
        s.control
            .call("plan_status", json!({"session_id":id,"after_event":after}))
            .await,
    )
}
async fn planning_action(
    State(s): State<Server>,
    Path((id, action)): Path<(String, String)>,
    Json(mut value): Json<Value>,
) -> Response {
    if !["answer", "approve", "revise", "resume", "cancel"].contains(&action.as_str()) {
        return StatusCode::NOT_FOUND.into_response();
    }
    if !value.is_object() {
        return result(Err(Error::Config("a JSON object is required".into())));
    }
    value["session_id"] = json!(id);
    result(
        s.control
            .call_as(&format!("plan_{action}"), value, &s.caller)
            .await,
    )
}
async fn report(State(s): State<Server>, Path(id): Path<String>) -> Response {
    result(
        s.control
            .call("plan_report", json!({"session_id":id}))
            .await,
    )
}
async fn command(
    State(s): State<Server>,
    Path(operation): Path<String>,
    Json(value): Json<Value>,
) -> Response {
    if !value.is_object() {
        return result(Err(Error::Config("a JSON object is required".into())));
    }
    result(s.control.call_as(&operation, value, &s.caller).await)
}
async fn events(
    State(s): State<Server>,
    Path(id): Path<String>,
    headers: HeaderMap,
    Query(query): Query<BTreeMap<String, String>>,
) -> Response {
    let cursor = headers
        .get("last-event-id")
        .and_then(|v| v.to_str().ok())
        .or_else(|| query.get("after_event").map(String::as_str))
        .unwrap_or("0");
    let mut cursor = match cursor.parse::<u64>() {
        Ok(v) => v,
        Err(_) => return result(Err(Error::Config("invalid Last-Event-ID".into()))),
    };
    let run = match s.control.runtime.status(&id).await {
        Ok(r) => r,
        Err(e) => return result(Err(e)),
    };
    if cursor > run.events.len() as u64 {
        return result(Err(Error::Conflict(
            "event cursor is beyond saved history".into(),
        )));
    }
    // No broadcast race: every pass reads the authoritative committed log after
    // the previous cursor. A bounded queue disconnects slow clients; reconnect
    // from their last consumed ID replays anything they missed.
    let (sender, receiver) =
        tokio::sync::mpsc::channel::<std::result::Result<Event, Infallible>>(64);
    tokio::spawn(async move {
        loop {
            let run = match s.control.runtime.status(&id).await {
                Ok(r) => r,
                Err(_) => break,
            };
            let after = cursor;
            let batch = run
                .events
                .iter()
                .filter(|event| event.sequence > after)
                .take(32);
            for event in batch {
                let data = match serde_json::to_string(event) {
                    Ok(v) => v,
                    Err(_) => return,
                };
                if sender
                    .try_send(Ok(Event::default()
                        .id(event.sequence.to_string())
                        .data(data)))
                    .is_err()
                {
                    return;
                }
                cursor = event.sequence;
            }
            if sender.is_closed() {
                return;
            }
            if cursor < run.events.len() as u64 {
                tokio::task::yield_now().await;
            } else {
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
        }
    });
    Sse::new(ReceiverStream::new(receiver))
        .keep_alive(KeepAlive::default())
        .into_response()
}
/// A bounded background supervisor owns execution independently of HTTP/SSE connections.
pub async fn supervise(control: Control) -> Result<()> {
    let mut jobs = tokio::task::JoinSet::new();
    let mut active = HashSet::new();
    let mut delivery_poll = BTreeMap::new();
    loop {
        while let Some(result) = jobs.try_join_next() {
            let (id, outcome) = result.map_err(|e| Error::Worker(e.to_string()))?;
            active.remove(&id);
            match outcome {
                Ok(_) | Err(Error::Busy) => (),
                Err(error) => eprintln!("Factory run {id}: {error}"),
            }
        }
        control.recover().await?;
        for run in control.runtime.list().await? {
            if active.len() >= 16 {
                break;
            }
            if active.contains(&run.run_id)
                || (run.status.is_terminal() && !run.delivery_pending())
                || (run.status == Status::RecoveryNeeded
                    && !run.cancel_requested
                    && chrono::Utc::now() < run.expires_at())
            {
                continue;
            }
            if run.status == Status::WaitingForInput
                && run.waiting_for_answers()
                && !run.cancel_requested
                && chrono::Utc::now() < run.expires_at()
            {
                continue;
            }
            if run.delivery_pending() {
                let now = tokio::time::Instant::now();
                if delivery_poll
                    .get(&run.run_id)
                    .is_some_and(|next| now < *next)
                {
                    continue;
                }
                delivery_poll.insert(run.run_id.clone(), now + Duration::from_secs(5));
            }
            active.insert(run.run_id.clone());
            let runtime = control.runtime.clone();
            jobs.spawn(async move {
                let result = runtime.resume(&run.run_id).await;
                (run.run_id, result)
            });
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
}

fn public_host(host: &str) -> bool {
    let local = host
        .parse::<std::net::SocketAddr>()
        .is_ok_and(|a| a.ip().is_loopback())
        || host
            .parse::<std::net::IpAddr>()
            .is_ok_and(|a| a.is_loopback())
        || host == "localhost"
        || host
            .strip_prefix("localhost:")
            .is_some_and(|port| port.parse::<u16>().is_ok());
    !local
}
/// Validate the advertised origin as well as the backend listener address.
pub fn public_origin(
    address: std::net::SocketAddr,
    host: &str,
    configured: Option<&str>,
) -> Result<String> {
    let origin = configured
        .map(String::from)
        .unwrap_or_else(|| format!("http://{host}"));
    if origin != format!("http://{host}") && origin != format!("https://{host}") {
        return Err(Error::Config(
            "FACTORY_HTTP_ORIGIN must be the exact HTTP(S) origin for FACTORY_HTTP_HOST".into(),
        ));
    }
    if (!address.ip().is_loopback() || public_host(host)) && !origin.starts_with("https://") {
        return Err(Error::Config(
            "public service requires an HTTPS FACTORY_HTTP_ORIGIN".into(),
        ));
    }
    Ok(origin)
}

pub async fn serve(control: Control, address: &str, token: &str) -> Result<()> {
    let _service_lock = control.maintenance_lock()?;
    let host = std::env::var("FACTORY_HTTP_HOST").unwrap_or_else(|_| address.into());
    let address: std::net::SocketAddr = address
        .parse()
        .map_err(|_| Error::Config("HTTP bind must be an IP address and port".into()))?;
    if (!address.ip().is_loopback() || public_host(&host))
        && (std::env::var("FACTORY_HTTP_TOKEN").is_err()
            || std::env::var("FACTORY_HTTP_HOST").is_err()
            || std::env::var("FACTORY_CALLER_ID").is_err())
    {
        return Err(Error::Config("non-loopback service requires operator-configured FACTORY_HTTP_TOKEN, FACTORY_HTTP_HOST and FACTORY_CALLER_ID".into()));
    }
    let configured = std::env::var("FACTORY_HTTP_ORIGIN").ok();
    let origin = public_origin(address, &host, configured.as_deref())?;
    let listener = tokio::net::TcpListener::bind(address).await?;
    println!(
        "{}",
        json!({"ready":true,"control_version":1,"url":format!("{origin}/?token={token}")})
    );
    let app = router(control.clone(), token.into(), host);
    tokio::select! {
        result=axum::serve(listener,app)=>{result?;Ok(())},
        result=supervise(control)=>result,
        result=tokio::signal::ctrl_c()=>{result?;Ok(())},
    }
}
