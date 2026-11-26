// A finding only of rfc-0018-string-error; the rfc-0018 set accepts it.
fn run() -> Result<(), String> {
    Err("not yet".to_string())
}
fn main() {
    let _ = run();
}
