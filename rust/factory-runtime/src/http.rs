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
    future::IntoFuture,
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
        .route(
            "/health/live",
            get(|| async { Json(json!({"status":"ok"})) }),
        )
        .route("/health/ready", get(readiness))
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
fn query_token(uri: &axum::http::Uri) -> Option<String> {
    Query::<BTreeMap<String, String>>::try_from_uri(uri)
        .ok()?
        .0
        .remove("token")
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
        && query_token(request.uri()).is_some_and(|token| constant_eq(&token, &state.token));
    let probe = request.method() == axum::http::Method::GET
        && matches!(request.uri().path(), "/health/live" | "/health/ready");
    if !probe && !token_query && !authorized(headers, &state.token) {
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
async fn readiness(State(state): State<Server>) -> Response {
    // Reading committed state verifies SQLite access without exposing paths or runs.
    match state.control.runtime.list().await {
        Ok(_) => Json(json!({"status":"ok"})).into_response(),
        Err(_) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"status":"unavailable"})),
        )
            .into_response(),
    }
}
async fn page(State(state): State<Server>, uri: axum::http::Uri) -> Response {
    if query_token(&uri).is_some_and(|token| constant_eq(&token, &state.token)) {
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
        include_str!("../assets/web/index.html"),
    )
        .into_response()
}
async fn asset(Path(name): Path<String>) -> Response {
    if name == "geist-latin.woff2" {
        return (
            [(header::CONTENT_TYPE, "font/woff2")],
            include_bytes!("../assets/web/geist-latin.woff2").as_slice(),
        )
            .into_response();
    }
    let (kind, content) = match name.as_str() {
        "geist-LICENSE.txt" => (
            "text/plain; charset=utf-8",
            include_str!("../assets/web/geist-LICENSE.txt"),
        ),
        "app.js" => (
            "text/javascript; charset=utf-8",
            include_str!("../assets/web/app.js"),
        ),
        "app.css" => (
            "text/css; charset=utf-8",
            include_str!("../assets/web/app.css"),
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
    supervise_until(control, tokio_util::sync::CancellationToken::new(), 1).await
}
async fn supervise_until(
    control: Control,
    shutdown: tokio_util::sync::CancellationToken,
    concurrency: usize,
) -> Result<()> {
    let mut jobs = tokio::task::JoinSet::new();
    let mut active = HashSet::new();
    let mut delivery_poll = BTreeMap::new();
    loop {
        if shutdown.is_cancelled() {
            // Finish in-flight state transitions where possible; preserve unfinished
            // runs/receipts for reconciliation, rather than marking them cancelled.
            let _ = tokio::time::timeout(Duration::from_secs(20), async {
                while jobs.join_next().await.is_some() {}
            })
            .await;
            jobs.abort_all();
            return Ok(());
        }
        while let Some(result) = jobs.try_join_next() {
            let (id, outcome) = result.map_err(|e| Error::Worker(e.to_string()))?;
            active.remove(&id);
            match outcome {
                Ok(_) | Err(Error::Busy) => (),
                Err(_) => eprintln!("{}", json!({"event":"run_attention_required","run_id":id})),
            }
        }
        control.recover().await?;
        for run in control.runtime.list().await? {
            if active.len() >= concurrency {
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
        tokio::select! {
            _ = shutdown.cancelled() => (),
            _ = tokio::time::sleep(Duration::from_millis(200)) => (),
        }
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

fn planning_url(origin: &str, token: &str) -> String {
    let encoded = percent_encoding::utf8_percent_encode(token, percent_encoding::NON_ALPHANUMERIC);
    format!("{origin}/?token={encoded}")
}

pub async fn serve(control: Control, address: &str, token: &str) -> Result<()> {
    let _workspace_service_lock = control.workspace_service_lock()?;
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
    // Explicit operator assertion: Compose publishes this container listener only
    // on host loopback. A public advertised host still always requires HTTPS.
    let origin_address = if std::env::var("FACTORY_HTTP_DOCKER_LOOPBACK").as_deref() == Ok("1")
        && !public_host(&host)
    {
        std::net::SocketAddr::from(([127, 0, 0, 1], address.port()))
    } else {
        address
    };
    let origin = public_origin(origin_address, &host, configured.as_deref())?;
    let concurrency = std::env::var("FACTORY_MAX_CONCURRENT_RUNS")
        .unwrap_or_else(|_| "1".into())
        .parse::<usize>()
        .ok()
        .filter(|n| (1..=16).contains(n))
        .ok_or_else(|| {
            Error::Config("FACTORY_MAX_CONCURRENT_RUNS must be between 1 and 16".into())
        })?;
    // Recover durable command intents before accepting requests. Every resumed
    // operation then reconciles its exact worker identity before any redispatch.
    control.recover().await?;
    control.runtime.list().await?;
    let listener = tokio::net::TcpListener::bind(address).await?;
    println!(
        "{}",
        if std::env::var("FACTORY_HTTP_TOKEN").is_ok() {
            json!({"ready":true,"control_version":1,"url":origin})
        } else {
            json!({"ready":true,"control_version":1,"url":planning_url(&origin, token)})
        }
    );
    let app = router(control.clone(), token.into(), host);
    let shutdown = tokio_util::sync::CancellationToken::new();
    let http_shutdown = shutdown.clone();
    let signal_shutdown = shutdown.clone();
    let server = axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            http_shutdown.cancelled().await;
        })
        .into_future();
    let supervisor = supervise_until(control, shutdown.clone(), concurrency);
    let signal = async move {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => result?,
            _ = terminate.recv() => (),
        }
        signal_shutdown.cancel();
        Ok::<_, std::io::Error>(())
    };
    tokio::pin!(server, supervisor, signal);
    tokio::select! {
        result = &mut server => { shutdown.cancel(); result?; },
        result = &mut supervisor => { shutdown.cancel(); result?; },
        result = &mut signal => {
            result?;
            // SSE clients may stay connected indefinitely. Bound HTTP drain as
            // well as worker transition drain, below Compose's stop deadline.
            if let Ok(result) = tokio::time::timeout(Duration::from_secs(25), async {
                let (http, supervisor) = tokio::join!(&mut server, &mut supervisor);
                http?;
                supervisor
            }).await { result?; }
        },
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn startup_link_round_trips_reserved_token_characters() {
        let token = "operator/token+with=reserved&characters";
        let url: axum::http::Uri = super::planning_url("http://127.0.0.1:4317", token)
            .parse()
            .unwrap();
        assert_eq!(super::query_token(&url).as_deref(), Some(token));
    }
}
