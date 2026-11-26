// A finding of the shared rfc-0018 set: std::env::args() panics on a non-Unicode argument.
fn main() {
    let args: Vec<String> = std::env::args().collect();
    println!("{}", args.len());
}
