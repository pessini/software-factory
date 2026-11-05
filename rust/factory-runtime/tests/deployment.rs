use axum::{body::Body, http::Request};
use factory_runtime::{Config, Runtime, control::Control};
use tower::ServiceExt;

#[test]
fn docker_loopback_service_starts_without_logging_secret_and_handles_sigterm() {
    use std::{
        io::{Read, Write},
        process::{Command, Stdio},
        time::{Duration, Instant},
    };
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let log = tempfile::NamedTempFile::new().unwrap();
    let secret = "deployment-secret-must-never-appear-in-logs";
    let mut child = Command::new(env!("CARGO_BIN_EXE_factory-runtime"))
        .args([
            data.path().to_str().unwrap(),
            workspace.path().to_str().unwrap(),
            "serve",
        ])
        .env("FACTORY_HTTP_BIND", format!("0.0.0.0:{port}"))
        .env("FACTORY_HTTP_HOST", format!("127.0.0.1:{port}"))
        .env("FACTORY_HTTP_ORIGIN", format!("http://127.0.0.1:{port}"))
        .env("FACTORY_HTTP_DOCKER_LOOPBACK", "1")
        .env("FACTORY_CALLER_ID", "operator")
        .env("FACTORY_HTTP_TOKEN", secret)
        .stdout(Stdio::from(log.reopen().unwrap()))
        .stderr(Stdio::from(log.reopen().unwrap()))
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let ready = loop {
        if child.try_wait().unwrap().is_some() || Instant::now() > deadline {
            break false;
        }
        if let Ok(mut stream) = std::net::TcpStream::connect(("127.0.0.1", port)) {
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            write!(
                stream,
                "GET /health/ready HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
            )
            .unwrap();
            let mut response = String::new();
            stream.read_to_string(&mut response).unwrap();
            if response.starts_with("HTTP/1.1 200") {
                break true;
            }
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    if !ready {
        let _ = child.kill();
    }
    assert!(ready, "{}", std::fs::read_to_string(log.path()).unwrap());
    unsafe {
        libc::kill(child.id() as i32, libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if Instant::now() > deadline {
            child.kill().unwrap();
            panic!("SIGTERM did not stop service");
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    assert!(status.success());
    assert!(
        !std::fs::read_to_string(log.path())
            .unwrap()
            .contains(secret)
    );
}

#[tokio::test]
async fn health_probes_need_no_secret_and_reject_unknown_hosts() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let runtime = Runtime::open(Config::new(data.path(), workspace.path()).unwrap())
        .await
        .unwrap();
    let control = Control::open(runtime).await.unwrap();
    let app = factory_runtime::http::router(control, "secret".into(), "127.0.0.1:4317".into());
    for path in ["/health/live", "/health/ready"] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(path)
                    .header("host", "127.0.0.1:4317")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), 200, "{path}");
        let body = axum::body::to_bytes(response.into_body(), 1024)
            .await
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&body).unwrap(),
            serde_json::json!({"status":"ok"})
        );
    }
    let response = app
        .oneshot(
            Request::builder()
                .uri("/health/ready")
                .header("host", "evil.example")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 421);
}
