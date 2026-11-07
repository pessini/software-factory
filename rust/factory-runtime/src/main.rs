#[tokio::main]
async fn main() {
    if let Err(error) = factory_runtime::operator::command(std::env::args().skip(1).collect()).await
    {
        eprintln!("{}", serde_json::json!({"error": error.to_string()}));
        std::process::exit(1);
    }
}
